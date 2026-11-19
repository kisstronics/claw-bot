"""
News agent: fetch top world headlines, email one-liners for new items only.

Dedupes via claw.news_sent so the same story is not emailed twice.
Always aims for up to 5 new items per run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

load_dotenv()

from channels.gmail import send_email
from memory_store import ensure_layout
from user_scope import current_user_id

import db

# BBC World + Google News World (no API key)
RSS_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
]

TOP_N = 5
MAX_SENT_IDS = 500
USER_AGENT = "python-claw-news-agent/1.0"


def _owner() -> str:
    return current_user_id() or db.SYSTEM_USER_ID


def _news_recipient(explicit: str | None) -> str:
    if explicit and "@" in str(explicit):
        return str(explicit).strip()
    from user_scope import current_user_id

    if current_user_id():
        from accounts import news_recipient

        return news_recipient()
    return os.environ.get("NEWS_EMAIL_TO", "").strip()


def _story_id(title: str, link: str) -> str:
    raw = f"{title.strip().lower()}|{link.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return " ".join(text.split()).strip()


def _load_sent() -> dict[str, Any]:
    ensure_layout()
    rows = db.fetch_all(
        """
        SELECT story_id, title, link, extract(epoch FROM sent_at) AS sent_at
        FROM news_sent
        WHERE user_id = %s
        ORDER BY sent_at
        """,
        (_owner(),),
    )
    ids = [str(row["story_id"]) for row in rows]
    items = {
        str(row["story_id"]): {
            "title": row.get("title") or "",
            "link": row.get("link") or "",
            "sent_at": float(row["sent_at"] or 0),
        }
        for row in rows
    }
    return {"ids": ids, "items": items}


def _save_sent(data: dict[str, Any]) -> None:
    ensure_layout()
    user_id = _owner()
    ids = list(data.get("ids") or [])
    items = dict(data.get("items") or {})
    if len(ids) > MAX_SENT_IDS:
        ids = ids[-MAX_SENT_IDS:]
        items = {sid: items[sid] for sid in ids if sid in items}
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM news_sent WHERE user_id = %s", (user_id,))
            for sid in ids:
                item = items.get(sid) or {}
                sent_at = float(item.get("sent_at") or time.time())
                cur.execute(
                    """
                    INSERT INTO news_sent (user_id, story_id, title, link, sent_at)
                    VALUES (%s, %s, %s, %s, to_timestamp(%s))
                    """,
                    (
                        user_id,
                        sid,
                        str(item.get("title") or ""),
                        str(item.get("link") or ""),
                        sent_at,
                    ),
                )
        conn.commit()


def _fetch_feed(url: str, timeout: int = 20) -> list[dict[str, Any]]:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (URLError, TimeoutError, OSError) as exc:
        print(f"[news_agent] feed error {url}: {exc}")
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        print(f"[news_agent] parse error {url}: {exc}")
        return []

    items: list[dict[str, Any]] = []
    # RSS 2.0: channel/item ; Atom: entry
    nodes = root.findall(".//item") or root.findall(
        ".//{http://www.w3.org/2005/Atom}entry"
    )
    for node in nodes:
        title = _clean_text(
            (node.findtext("title") or node.findtext("{http://www.w3.org/2005/Atom}title") or "")
        )
        link = (
            node.findtext("link")
            or ""
        ).strip()
        if not link:
            link_el = node.find("{http://www.w3.org/2005/Atom}link")
            if link_el is not None:
                link = (link_el.get("href") or "").strip()

        desc = _clean_text(
            node.findtext("description")
            or node.findtext("{http://www.w3.org/2005/Atom}summary")
            or ""
        )
        pub = node.findtext("pubDate") or node.findtext(
            "{http://www.w3.org/2005/Atom}updated"
        )
        ts = 0.0
        if pub:
            try:
                ts = parsedate_to_datetime(pub).timestamp()
            except (TypeError, ValueError, IndexError):
                ts = 0.0

        if not title:
            continue
        one_liner = title if not desc or desc == title else f"{title} — {desc[:120]}"
        # Keep one-liner short
        one_liner = one_liner[:220]
        items.append(
            {
                "id": _story_id(title, link or title),
                "title": title,
                "link": link,
                "one_liner": one_liner,
                "published_ts": ts,
                "source": url,
            }
        )
    return items


def fetch_top_world_news(limit: int = 30) -> list[dict[str, Any]]:
    """Merge feeds, dedupe by id, prefer fresher / earlier-in-feed order."""
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for feed in RSS_FEEDS:
        for item in _fetch_feed(feed):
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            merged.append(item)

    # Stable: items already roughly ranked by each feed; bump by recency lightly
    merged.sort(key=lambda x: x.get("published_ts") or 0, reverse=True)
    return merged[: max(1, limit)]


def pick_new_top_n(n: int = TOP_N) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sent = _load_sent()
    sent_ids = set(sent.get("ids") or [])
    fresh: list[dict[str, Any]] = []
    for item in fetch_top_world_news(limit=40):
        if item["id"] in sent_ids:
            continue
        fresh.append(item)
        if len(fresh) >= n:
            break
    return fresh, sent


def mark_sent(items: list[dict[str, Any]], sent: dict[str, Any]) -> None:
    ids = list(sent.get("ids") or [])
    store = dict(sent.get("items") or {})
    now = time.time()
    for item in items:
        sid = item["id"]
        if sid not in ids:
            ids.append(sid)
        store[sid] = {
            "title": item.get("title"),
            "link": item.get("link"),
            "sent_at": now,
        }
    _save_sent({"ids": ids, "items": store})


def build_email_body(items: list[dict[str, Any]]) -> str:
    lines = [
        "Top world news (new since last digest)",
        "=" * 40,
        "",
    ]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {item['one_liner']}")
        if item.get("link"):
            lines.append(f"   {item['link']}")
        lines.append("")
    lines.append("— news agent")
    return "\n".join(lines)


def run_news_digest(
    *,
    n: int = TOP_N,
    to_addr: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Fetch top world news, email up to `n` unseen one-liners, remember sent ids.
    """
    n = max(1, min(int(n), 10))
    items, sent = pick_new_top_n(n)

    if not items:
        return {
            "ok": True,
            "emailed": False,
            "reason": "no_new_stories",
            "count": 0,
            "items": [],
        }

    subject = f"World news digest — {len(items)} new headline(s)"
    body = build_email_body(items)
    recipient = _news_recipient(to_addr) or None

    if dry_run:
        return {
            "ok": True,
            "emailed": False,
            "dry_run": True,
            "count": len(items),
            "subject": subject,
            "body": body,
            "items": items,
        }

    mail_result = send_email(subject=subject, body=body, to_addr=recipient)
    if not mail_result.get("ok"):
        return {
            "ok": False,
            "emailed": False,
            "error": mail_result.get("error"),
            "count": len(items),
            "items": items,
        }

    mark_sent(items, sent)
    # Also jot a short journal line in memory
    try:
        from memory_store import journal

        journal(
            f"news_digest emailed {len(items)} stories",
            meta={"ids": [i["id"] for i in items]},
        )
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": True,
        "emailed": True,
        "count": len(items),
        "to": mail_result.get("to"),
        "subject": subject,
        "items": [
            {"id": i["id"], "one_liner": i["one_liner"], "link": i.get("link")}
            for i in items
        ],
    }


if __name__ == "__main__":
    print(json.dumps(run_news_digest(), indent=2))
