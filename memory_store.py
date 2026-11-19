"""
Postgres-backed memory for the agent (claw schema).

Signed-in users use their own rows; otherwise the `_system` user is used.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import db
from user_scope import current_user_id

_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")
_DEFAULT_CORE = "# Agent memory\n\nDurable facts and preferences go here.\n"


def _owner() -> str:
    return current_user_id() or db.SYSTEM_USER_ID


def ensure_layout() -> None:
    user_id = _owner()
    db.execute(
        """
        INSERT INTO memory_core (user_id, content)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id, _DEFAULT_CORE),
    )


def _safe_key(key: str) -> str:
    key = (key or "").strip()
    if not _KEY_RE.match(key):
        raise ValueError(
            "Invalid key. Use 1-64 chars: letters, digits, underscore, hyphen."
        )
    return key


def load_core_memory(max_chars: int = 8000) -> str:
    ensure_layout()
    row = db.fetch_one(
        "SELECT content FROM memory_core WHERE user_id = %s",
        (_owner(),),
    )
    text = str((row or {}).get("content") or "").strip()
    if len(text) > max_chars:
        return text[:max_chars] + "\n…[truncated]"
    return text


def load_memory_for_prompt(max_chars: int = 8000) -> str:
    """Core memory plus all notes — injected into the system prompt."""
    ensure_layout()
    user_id = _owner()
    parts: list[str] = []
    core = load_core_memory(max_chars=max_chars)
    if core:
        parts.append(core)
    notes = db.fetch_all(
        "SELECT key, content FROM memory_notes WHERE user_id = %s ORDER BY key",
        (user_id,),
    )
    for row in notes:
        key = str(row.get("key") or "")
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        parts.append(f"### Note: {key}\n{content}")
    text = "\n\n".join(parts).strip()
    if len(text) > max_chars:
        return text[:max_chars] + "\n…[truncated]"
    return text or "# Agent memory\n\n(no facts stored yet)\n"


def remember(content: str, *, key: str | None = None, append: bool = True) -> dict[str, Any]:
    ensure_layout()
    content = (content or "").strip()
    if not content:
        return {"ok": False, "error": "content is required"}
    user_id = _owner()

    if key:
        key = _safe_key(key)
        row = db.fetch_one(
            "SELECT content FROM memory_notes WHERE user_id = %s AND key = %s",
            (user_id, key),
        )
        if append and row and row.get("content"):
            existing = str(row["content"]).rstrip()
            text = existing + "\n\n" + content + "\n"
        else:
            text = content.rstrip() + "\n"
        db.execute(
            """
            INSERT INTO memory_notes (user_id, key, content, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (user_id, key) DO UPDATE
            SET content = EXCLUDED.content, updated_at = now()
            """,
            (user_id, key, text),
        )
        return {"ok": True, "where": "note", "key": key, "path": f"db://notes/{key}"}

    row = db.fetch_one(
        "SELECT content FROM memory_core WHERE user_id = %s",
        (user_id,),
    )
    if append and row and row.get("content"):
        existing = str(row["content"]).rstrip()
        text = existing + "\n\n" + content + "\n"
    else:
        text = content.rstrip() + "\n"
    db.execute(
        """
        INSERT INTO memory_core (user_id, content, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (user_id) DO UPDATE
        SET content = EXCLUDED.content, updated_at = now()
        """,
        (user_id, text),
    )
    return {"ok": True, "where": "core", "path": "db://memory_core"}


def recall(key: str | None = None) -> dict[str, Any]:
    ensure_layout()
    user_id = _owner()
    if key:
        key = _safe_key(key)
        row = db.fetch_one(
            "SELECT content FROM memory_notes WHERE user_id = %s AND key = %s",
            (user_id, key),
        )
        if not row:
            return {"ok": False, "error": f"No note named '{key}'"}
        return {
            "ok": True,
            "where": "note",
            "key": key,
            "content": str(row.get("content") or ""),
        }
    row = db.fetch_one(
        "SELECT content FROM memory_core WHERE user_id = %s",
        (user_id,),
    )
    return {
        "ok": True,
        "where": "core",
        "content": str((row or {}).get("content") or ""),
    }


def list_notes() -> dict[str, Any]:
    ensure_layout()
    rows = db.fetch_all(
        "SELECT key FROM memory_notes WHERE user_id = %s ORDER BY key",
        (_owner(),),
    )
    keys = [str(row["key"]) for row in rows]
    return {"ok": True, "count": len(keys), "keys": keys}


def forget(key: str | None = None) -> dict[str, Any]:
    ensure_layout()
    user_id = _owner()
    if key:
        key = _safe_key(key)
        row = db.fetch_one(
            "SELECT 1 FROM memory_notes WHERE user_id = %s AND key = %s",
            (user_id, key),
        )
        if not row:
            return {"ok": False, "error": f"No note named '{key}'"}
        db.execute(
            "DELETE FROM memory_notes WHERE user_id = %s AND key = %s",
            (user_id, key),
        )
        return {"ok": True, "deleted": key}
    db.execute(
        """
        INSERT INTO memory_core (user_id, content, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (user_id) DO UPDATE
        SET content = EXCLUDED.content, updated_at = now()
        """,
        (user_id, _DEFAULT_CORE),
    )
    return {"ok": True, "deleted": "core"}


def search(query: str, limit: int = 10) -> dict[str, Any]:
    ensure_layout()
    raw = (query or "").strip().lower()
    if not raw:
        return {"ok": False, "error": "query is required"}
    tokens = [tok for tok in re.split(r"\W+", raw) if len(tok) >= 2]
    if not tokens:
        tokens = [raw]

    user_id = _owner()
    hits: list[dict[str, Any]] = []
    core = db.fetch_one(
        "SELECT content FROM memory_core WHERE user_id = %s",
        (user_id,),
    )
    files: list[tuple[str, str]] = [
        ("MEMORY.md", str((core or {}).get("content") or "")),
    ]
    for row in db.fetch_all(
        "SELECT key, content FROM memory_notes WHERE user_id = %s ORDER BY key",
        (user_id,),
    ):
        files.append((f"notes/{row['key']}.md", str(row.get("content") or "")))

    for name, text in files:
        lowered = text.lower()
        # Document matches when every token appears somewhere in it.
        doc_match = all(tok in lowered for tok in tokens)
        lines = text.splitlines()
        matched_line = False
        for i, line in enumerate(lines, 1):
            line_l = line.lower().strip()
            if not line_l:
                continue
            if all(tok in line_l for tok in tokens) or (
                doc_match and any(tok in line_l for tok in tokens)
            ):
                hits.append(
                    {
                        "file": name,
                        "line": i,
                        "text": line.strip()[:300],
                    }
                )
                matched_line = True
                if len(hits) >= limit:
                    return {"ok": True, "count": len(hits), "hits": hits}
        # Note/key matched the query but no single line did — return a snippet.
        if doc_match and not matched_line:
            snippet = " ".join(line.strip() for line in lines if line.strip())[:300]
            if snippet:
                hits.append({"file": name, "line": 1, "text": snippet})
                if len(hits) >= limit:
                    return {"ok": True, "count": len(hits), "hits": hits}
    return {"ok": True, "count": len(hits), "hits": hits}


def journal(event: str, *, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_layout()
    event = (event or "").strip()
    if not event:
        return {"ok": False, "error": "event is required"}
    db.execute(
        """
        INSERT INTO journal (user_id, ts, event, meta)
        VALUES (%s, to_timestamp(%s), %s, %s::jsonb)
        """,
        (_owner(), time.time(), event, json.dumps(meta or {})),
    )
    return {"ok": True, "written": True}


def read_journal(limit: int = 20) -> dict[str, Any]:
    ensure_layout()
    limit = max(1, min(int(limit), 100))
    rows = db.fetch_all(
        """
        SELECT extract(epoch FROM ts) AS ts, event, meta
        FROM journal
        WHERE user_id = %s
        ORDER BY ts DESC
        LIMIT %s
        """,
        (_owner(), limit),
    )
    entries = [
        {
            "ts": float(row["ts"] or 0),
            "event": row["event"],
            "meta": row["meta"] if isinstance(row["meta"], dict) else {},
        }
        for row in reversed(rows)
    ]
    return {"ok": True, "count": len(entries), "entries": entries}


def memory_texts_for_events() -> list[tuple[str, str]]:
    """Core + notes as (source, text) for reminder parsing."""
    ensure_layout()
    user_id = _owner()
    texts: list[tuple[str, str]] = []
    core = db.fetch_one(
        "SELECT content FROM memory_core WHERE user_id = %s",
        (user_id,),
    )
    if core and core.get("content"):
        texts.append(("memory", str(core["content"])))
    for row in db.fetch_all(
        "SELECT key, content FROM memory_notes WHERE user_id = %s ORDER BY key",
        (user_id,),
    ):
        texts.append((f"notes/{row['key']}.md", str(row.get("content") or "")))
    return texts


def handle(action: str, **kwargs: Any) -> dict[str, Any]:
    action = (action or "").strip().lower()
    try:
        if action in {"remember", "write", "save"}:
            return remember(
                str(kwargs.get("content") or ""),
                key=kwargs.get("key"),
                append=bool(kwargs.get("append", True)),
            )
        if action in {"recall", "read", "get"}:
            return recall(kwargs.get("key"))
        if action in {"list", "list_notes"}:
            return list_notes()
        if action in {"forget", "delete"}:
            return forget(kwargs.get("key"))
        if action in {"search", "find"}:
            return search(str(kwargs.get("query") or ""), int(kwargs.get("limit", 10)))
        if action in {"journal", "log"}:
            return journal(str(kwargs.get("content") or kwargs.get("event") or ""))
        if action in {"read_journal", "journal_read"}:
            return read_journal(int(kwargs.get("limit", 20)))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    return {
        "ok": False,
        "error": (
            f"Unknown action '{action}'. "
            "Use remember, recall, list, forget, search, journal, read_journal."
        ),
    }
