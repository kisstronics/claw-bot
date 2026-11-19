"""
Simple filesystem memory for the agent.

With a signed-in Telegram user, files live under data/users/<id>/memory.
Otherwise they stay in the legacy ./memory directory.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path(__file__).with_name("memory")))
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
NOTES_DIR = MEMORY_DIR / "notes"
JOURNAL_FILE = MEMORY_DIR / "journal.jsonl"

_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")


def active_memory_dir() -> Path:
    from user_scope import current_user_id

    user_id = current_user_id()
    if not user_id:
        return MEMORY_DIR
    from accounts import memory_dir

    return memory_dir(user_id)


def _core() -> Path:
    return active_memory_dir() / "MEMORY.md"


def _notes() -> Path:
    return active_memory_dir() / "notes"


def _journal_path() -> Path:
    return active_memory_dir() / "journal.jsonl"


def ensure_layout() -> None:
    notes = _notes()
    notes.mkdir(parents=True, exist_ok=True)
    core = _core()
    if not core.exists():
        core.write_text(
            "# Agent memory\n\nDurable facts and preferences go here.\n",
            encoding="utf-8",
        )


def _safe_key(key: str) -> str:
    key = (key or "").strip()
    if not _KEY_RE.match(key):
        raise ValueError(
            "Invalid key. Use 1-64 chars: letters, digits, underscore, hyphen."
        )
    return key


def _note_path(key: str) -> Path:
    return _notes() / f"{_safe_key(key)}.md"


def load_core_memory(max_chars: int = 8000) -> str:
    ensure_layout()
    text = _core().read_text(encoding="utf-8").strip()
    if len(text) > max_chars:
        return text[:max_chars] + "\n…[truncated]"
    return text


def remember(content: str, *, key: str | None = None, append: bool = True) -> dict[str, Any]:
    """
    Save memory.
    - key omitted → write/append to MEMORY.md (core durable memory)
    - key set → write/append notes/<key>.md
    """
    ensure_layout()
    content = (content or "").strip()
    if not content:
        return {"ok": False, "error": "content is required"}

    if key:
        path = _note_path(key)
        if append and path.exists():
            existing = path.read_text(encoding="utf-8").rstrip()
            path.write_text(existing + "\n\n" + content + "\n", encoding="utf-8")
        else:
            path.write_text(content.rstrip() + "\n", encoding="utf-8")
        return {"ok": True, "where": "note", "key": key, "path": str(path)}

    core = _core()
    if append and core.exists():
        existing = core.read_text(encoding="utf-8").rstrip()
        core.write_text(existing + "\n\n" + content + "\n", encoding="utf-8")
    else:
        core.write_text(content.rstrip() + "\n", encoding="utf-8")
    return {"ok": True, "where": "core", "path": str(core)}


def recall(key: str | None = None) -> dict[str, Any]:
    ensure_layout()
    if key:
        path = _note_path(key)
        if not path.exists():
            return {"ok": False, "error": f"No note named '{key}'"}
        return {
            "ok": True,
            "where": "note",
            "key": key,
            "content": path.read_text(encoding="utf-8"),
        }
    return {
        "ok": True,
        "where": "core",
        "content": _core().read_text(encoding="utf-8"),
    }


def list_notes() -> dict[str, Any]:
    ensure_layout()
    keys = sorted(p.stem for p in _notes().glob("*.md"))
    return {"ok": True, "count": len(keys), "keys": keys}


def forget(key: str | None = None) -> dict[str, Any]:
    ensure_layout()
    if key:
        path = _note_path(key)
        if not path.exists():
            return {"ok": False, "error": f"No note named '{key}'"}
        path.unlink()
        return {"ok": True, "deleted": key}
    _core().write_text(
        "# Agent memory\n\nDurable facts and preferences go here.\n",
        encoding="utf-8",
    )
    return {"ok": True, "deleted": "core"}


def search(query: str, limit: int = 10) -> dict[str, Any]:
    ensure_layout()
    query = (query or "").strip().lower()
    if not query:
        return {"ok": False, "error": "query is required"}

    hits: list[dict[str, Any]] = []
    core = _core()
    files = [core, *sorted(_notes().glob("*.md"))]
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            if query in line.lower():
                hits.append(
                    {
                        "file": path.name if path == core else f"notes/{path.name}",
                        "line": i,
                        "text": line.strip()[:300],
                    }
                )
                if len(hits) >= limit:
                    return {"ok": True, "count": len(hits), "hits": hits}
    return {"ok": True, "count": len(hits), "hits": hits}


def journal(event: str, *, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_layout()
    event = (event or "").strip()
    if not event:
        return {"ok": False, "error": "event is required"}
    entry = {
        "ts": time.time(),
        "event": event,
        "meta": meta or {},
    }
    with _journal_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"ok": True, "written": True}


def read_journal(limit: int = 20) -> dict[str, Any]:
    ensure_layout()
    limit = max(1, min(int(limit), 100))
    path = _journal_path()
    if not path.exists():
        return {"ok": True, "count": 0, "entries": []}
    lines = path.read_text(encoding="utf-8").splitlines()
    entries: list[Any] = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            entries.append({"raw": line})
    return {"ok": True, "count": len(entries), "entries": entries}


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
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    return {
        "ok": False,
        "error": (
            f"Unknown action '{action}'. "
            "Use remember, recall, list, forget, search, journal, read_journal."
        ),
    }
