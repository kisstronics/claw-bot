"""
Reminder agent: anniversaries for today, and tasks scheduled to remind.

Runs hourly. Sources:
  - claw.memory_core / claw.memory_notes
    (anniversary, birthday, or any line marked remindable, with a date)
  - claw.reminders for explicit tasks

Notifies once per occurrence on Telegram. Recurring items fire again on the
next due day; one-shot tasks fire once.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from channels.telegram import bot_handler
from memory_store import ensure_layout, journal, memory_texts_for_events
from tools.basic import resolve_relative
from user_scope import current_user_id

import db

MAX_SENT = 500


def _owner() -> str:
    return current_user_id() or db.SYSTEM_USER_ID

_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

_HINT_RE = re.compile(r"remind|anniversary|birthday|birth\s*day", re.I)
_ISO_RE = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})(?:[T\s](\d{1,2}):(\d{2}))?\b"
)
_DMY_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"([A-Za-z]{3,9})"
    r"(?:\s+(\d{4}))?"
    r"(?:\s+at\s+(\d{1,2}):(\d{2}))?\b",
    re.I,
)
_MDY_RE = re.compile(
    r"\b([A-Za-z]{3,9})\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(\d{4}))?"
    r"(?:\s+at\s+(\d{1,2}):(\d{2}))?\b",
    re.I,
)
_TIME_RE = re.compile(r"\bat\s+(\d{1,2}):(\d{2})\b", re.I)


def _valid_md(month: int, day: int) -> bool:
    try:
        date(2024, month, day)
    except ValueError:
        return False
    return True


def _clock(hour: int | None, minute: int | None) -> tuple[int | None, int | None]:
    if hour is None:
        return None, None
    if not (0 <= hour <= 23 and 0 <= (minute or 0) <= 59):
        return None, None
    return hour, minute or 0


def _parts(
    month: int,
    day: int,
    year: int | None,
    hour: int | None,
    minute: int | None,
) -> dict[str, Any] | None:
    if not _valid_md(month, day):
        return None
    hour, minute = _clock(hour, minute)
    return {
        "month": month,
        "day": day,
        "year": year,
        "hour": hour,
        "minute": minute,
    }


def _match_iso(text: str) -> dict[str, Any] | None:
    match = _ISO_RE.search(text)
    if not match:
        return None
    year, month, day, hour, minute = match.groups()
    return _parts(
        int(month),
        int(day),
        int(year),
        int(hour) if hour else None,
        int(minute) if minute else None,
    )


def _match_dmy(text: str) -> dict[str, Any] | None:
    match = _DMY_RE.search(text)
    if not match:
        return None
    day, month_name, year, hour, minute = match.groups()
    month = _MONTHS.get(month_name.lower())
    if not month:
        return None
    return _parts(
        month,
        int(day),
        int(year) if year else None,
        int(hour) if hour else None,
        int(minute) if minute else None,
    )


def _match_mdy(text: str) -> dict[str, Any] | None:
    match = _MDY_RE.search(text)
    if not match:
        return None
    month_name, day, year, hour, minute = match.groups()
    month = _MONTHS.get(month_name.lower())
    if not month:
        return None
    return _parts(
        month,
        int(day),
        int(year) if year else None,
        int(hour) if hour else None,
        int(minute) if minute else None,
    )


_CLOCK_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", re.I)


def _parse_clock(text: str) -> tuple[int, int] | None:
    """Clock time only: 10:55, 10:55 AM, or 11pm. A bare number is not a time."""
    match = _CLOCK_RE.fullmatch(text or "")
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    ampm = (match.group(3) or "").lower()
    if match.group(2) is None and not ampm:
        return None
    if ampm:
        if not 1 <= hour <= 12 or not 0 <= minute <= 59:
            return None
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    elif hour > 23 or minute > 59:
        return None
    return hour, minute


def _due_from_clock(hour: int, minute: int, now: datetime) -> datetime:
    """Today at that time, or tomorrow if it was more than 15 minutes ago."""
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if (now - due).total_seconds() > 15 * 60:
        due += timedelta(days=1)
    return due


def parse_when(text: str) -> dict[str, Any] | None:
    """First date in `text`. Accepts ISO, '30 July', or 'July 30', optional time."""
    text = text or ""
    for matcher in (_match_iso, _match_dmy, _match_mdy):
        found = matcher(text)
        if found:
            if found["hour"] is None:
                clock = _TIME_RE.search(text)
                if clock:
                    hour, minute = _clock(int(clock.group(1)), int(clock.group(2)))
                    found["hour"] = hour
                    found["minute"] = minute
            return found
    return None


def _recurring_from_text(text: str) -> str | None:
    lower = text.lower()
    if re.search(r"\b(daily|every day)\b", lower):
        return "daily"
    if re.search(r"\b(yearly|every year|recurring)\b", lower) or re.search(
        r"anniversary|birthday", lower
    ):
        return "yearly"
    return None


def _display_text(line: str) -> str:
    text = re.sub(r"\s+", " ", line).strip()
    text = re.sub(r"\s*Remindable event\.?\s*$", "", text, flags=re.I).strip()
    return text[:300]


def _event_id(source: str, label: str, parts: dict[str, Any], recurring: str | None) -> str:
    raw = "|".join(
        [
            source,
            label.lower(),
            f"{parts['month']:02d}-{parts['day']:02d}",
            str(parts.get("year") or ""),
            "" if parts.get("hour") is None else f"{parts['hour']:02d}:{parts['minute']:02d}",
            recurring or "",
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _build_event(
    label: str,
    parts: dict[str, Any],
    recurring: str | None,
    source: str,
    *,
    task_id: str | None = None,
) -> dict[str, Any]:
    label = _display_text(label)
    return {
        "id": task_id or _event_id(source, label, parts, recurring),
        "text": label,
        "month": parts["month"],
        "day": parts["day"],
        "year": parts.get("year"),
        "hour": parts.get("hour"),
        "minute": parts.get("minute"),
        "recurring": recurring,
        "source": source,
    }


def event_from_line(line: str, source: str = "memory") -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if not _HINT_RE.search(stripped):
        return None
    parts = parse_when(stripped)
    if not parts:
        return None
    return _build_event(stripped, parts, _recurring_from_text(stripped), source)


def events_from_text(text: str, source: str = "memory") -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        event = event_from_line(line, source)
        if event is None or event["id"] in seen:
            continue
        seen.add(event["id"])
        found.append(event)
    return found


def load_tasks() -> list[dict[str, Any]]:
    ensure_layout()
    rows = db.fetch_all(
        """
        SELECT id, text, when_at, recurring
        FROM reminders
        WHERE user_id = %s
        ORDER BY when_at, id
        """,
        (_owner(),),
    )
    return [
        {
            "id": row["id"],
            "text": row["text"],
            "when": row["when_at"],
            "recurring": row["recurring"],
        }
        for row in rows
    ]


def _save_tasks(tasks: list[dict[str, Any]]) -> None:
    ensure_layout()
    user_id = _owner()
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reminders WHERE user_id = %s", (user_id,))
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                task_id = str(task.get("id") or "").strip()
                text = str(task.get("text") or "").strip()
                when = str(task.get("when") or "").strip()
                if not task_id or not text or not when:
                    continue
                recurring = task.get("recurring") or None
                if recurring not in {None, "yearly", "daily"}:
                    recurring = None
                cur.execute(
                    """
                    INSERT INTO reminders (id, user_id, text, when_at, recurring)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (task_id, user_id, text, when, recurring),
                )
        conn.commit()


def tasks_as_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for task in load_tasks():
        text = str(task.get("text") or "").strip()
        when = str(task.get("when") or "").strip()
        if not text or not when:
            continue
        parts = parse_when(when)
        if not parts:
            continue
        recurring = task.get("recurring") or None
        if recurring not in {None, "yearly", "daily"}:
            recurring = None
        task_id = str(task.get("id") or "").strip() or None
        events.append(
            _build_event(text, parts, recurring, "task", task_id=task_id)
        )
    return events


def load_events() -> list[dict[str, Any]]:
    """Memory anniversaries/reminders plus scheduled tasks. Deduped by id."""
    ensure_layout()
    events: list[dict[str, Any]] = []
    seen: set[str] = set()

    for source, text in memory_texts_for_events():
        for event in events_from_text(text, source):
            if event["id"] in seen:
                continue
            seen.add(event["id"])
            events.append(event)

    for event in tasks_as_events():
        if event["id"] in seen:
            continue
        seen.add(event["id"])
        events.append(event)
    return events


def _is_due(event: dict[str, Any], now: datetime) -> bool:
    month = int(event["month"])
    day = int(event["day"])
    year = event.get("year")
    recurring = event.get("recurring")
    today = now.date()

    if recurring == "daily":
        date_ok = True
    elif recurring == "yearly":
        date_ok = today.month == month and today.day == day
    elif year:
        date_ok = today.year == int(year) and today.month == month and today.day == day
    else:
        date_ok = today.month == month and today.day == day

    if not date_ok:
        return False

    hour = event.get("hour")
    if hour is None:
        return True
    due_minutes = int(hour) * 60 + int(event.get("minute") or 0)
    now_minutes = now.hour * 60 + now.minute
    return now_minutes >= due_minutes


def due_events(events: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now()
    return [event for event in events if _is_due(event, now)]


def _sent_key(event: dict[str, Any], today: date) -> str:
    if event.get("recurring") in {"yearly", "daily"}:
        return f"{event['id']}|{today.isoformat()}"
    return str(event["id"])


def _load_sent() -> dict[str, Any]:
    ensure_layout()
    rows = db.fetch_all(
        "SELECT event_key, sent_on FROM reminder_sent WHERE user_id = %s",
        (_owner(),),
    )
    sent = {
        str(row["event_key"]): row["sent_on"].isoformat()
        if hasattr(row["sent_on"], "isoformat")
        else str(row["sent_on"])
        for row in rows
    }
    return {"sent": sent}


def _mark_sent(keys: list[str], today: date) -> None:
    ensure_layout()
    user_id = _owner()
    with db.connect() as conn:
        with conn.cursor() as cur:
            for key in keys:
                cur.execute(
                    """
                    INSERT INTO reminder_sent (user_id, event_key, sent_on)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, event_key) DO UPDATE
                    SET sent_on = EXCLUDED.sent_on
                    """,
                    (user_id, key, today),
                )
            cur.execute(
                """
                DELETE FROM reminder_sent rs
                WHERE rs.user_id = %s
                  AND rs.ctid IN (
                    SELECT ctid FROM reminder_sent
                    WHERE user_id = %s
                    ORDER BY sent_on ASC
                    OFFSET %s
                  )
                """,
                (user_id, user_id, MAX_SENT),
            )
        conn.commit()


def _format_message(items: list[dict[str, Any]], today: date) -> str:
    from channels.telegram import BRAND_NAME, escape_html, format_html_card

    body_lines: list[str] = []
    for item in items:
        suffix = ""
        if item.get("hour") is not None:
            suffix = f" ({int(item['hour']):02d}:{int(item.get('minute') or 0):02d})"
        body_lines.append(f"• {escape_html(str(item['text']))}{escape_html(suffix)}")

    return format_html_card(
        badge=BRAND_NAME,
        title="Reminders",
        subtitle=today.strftime("%d %b %Y"),
        lines=body_lines,
    )


def _public_item(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event["id"],
        "text": event["text"],
        "month": event["month"],
        "day": event["day"],
        "year": event.get("year"),
        "time": (
            None
            if event.get("hour") is None
            else f"{int(event['hour']):02d}:{int(event.get('minute') or 0):02d}"
        ),
        "recurring": event.get("recurring"),
        "source": event.get("source"),
    }


def run_reminders(*, dry_run: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """
    Notify for anniversaries due today and tasks whose schedule is due.
    Already-sent items are skipped until the next occurrence.
    """
    now = now or datetime.now()
    today = now.date()
    sent = _load_sent().get("sent") or {}
    pending: list[dict[str, Any]] = []
    for event in due_events(load_events(), now):
        key = _sent_key(event, today)
        if sent.get(key):
            continue
        event = dict(event)
        event["_sent_key"] = key
        pending.append(event)

    if not pending:
        return {
            "ok": True,
            "notified": False,
            "reason": "nothing_due",
            "count": 0,
            "items": [],
            "date": today.isoformat(),
        }

    message = _format_message(pending, today)
    items = [_public_item(event) for event in pending]

    if dry_run:
        return {
            "ok": True,
            "notified": False,
            "dry_run": True,
            "count": len(pending),
            "date": today.isoformat(),
            "message": message,
            "items": items,
        }

    from accounts import wants_telegram_notify

    if not wants_telegram_notify():
        return {
            "ok": True,
            "notified": False,
            "reason": "telegram_notifications_disabled",
            "count": len(pending),
            "date": today.isoformat(),
            "message": message,
            "items": items,
        }

    result = bot_handler.send_html(message)
    if not result.get("ok"):
        return {
            "ok": False,
            "notified": False,
            "error": result.get("error"),
            "count": len(pending),
            "date": today.isoformat(),
            "items": items,
        }

    _mark_sent([event["_sent_key"] for event in pending], today)
    try:
        journal(
            f"reminder notified {len(pending)} item(s)",
            meta={"ids": [event["id"] for event in pending], "date": today.isoformat()},
        )
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": True,
        "notified": True,
        "channel": "telegram",
        "count": len(pending),
        "date": today.isoformat(),
        "message": message,
        "items": items,
    }


def list_reminders() -> dict[str, Any]:
    events = load_events()
    return {
        "ok": True,
        "count": len(events),
        "items": [_public_item(event) for event in events],
    }


def add_task(
    text: str,
    when: str,
    recurring: str | None = None,
) -> dict[str, Any]:
    """Schedule a task reminder. `when` is a date or datetime the hourly job checks."""
    text = (text or "").strip()
    when = (when or "").strip()
    if not text:
        return {"ok": False, "error": "text is required"}
    if not when:
        return {"ok": False, "error": "when is required (YYYY-MM-DD, YYYY-MM-DDTHH:MM, or '30 July')"}

    recurring_norm = (recurring or "").strip().lower() or None
    if recurring_norm in {"", "none", "once"}:
        recurring_norm = None
    if recurring_norm not in {None, "yearly", "daily"}:
        return {"ok": False, "error": "recurring must be yearly, daily, or omitted"}

    relative = resolve_relative(when)
    if relative is not None:
        if not relative.get("ok"):
            return relative
        parts = {
            "month": relative["month"],
            "day": relative["day"],
            "year": relative["year"],
            "hour": relative.get("hour"),
            "minute": relative.get("minute"),
        }
    else:
        parts = parse_when(when)
    if parts is None:
        clock = _parse_clock(when)
        if clock is not None:
            hour, minute = clock
            due = _due_from_clock(hour, minute, datetime.now())
            parts = {
                "month": due.month,
                "day": due.day,
                "year": due.year,
                "hour": hour,
                "minute": minute,
            }
    if not parts:
        return {
            "ok": False,
            "error": (
                "Could not read a date from `when`. "
                "Use YYYY-MM-DD, YYYY-MM-DDTHH:MM, '30 July', or a time like 10:55."
            ),
        }

    now = datetime.now()
    year = parts.get("year")
    if year is None:
        try:
            candidate = date(now.year, parts["month"], parts["day"])
        except ValueError:
            candidate = None
        if recurring_norm in {"yearly", "daily"}:
            year = now.year if candidate else now.year + 1
        elif candidate is None or candidate < now.date():
            year = now.year + 1
        else:
            year = now.year
    parts = dict(parts)
    parts["year"] = int(year)

    if parts["hour"] is None:
        when_stored = f"{parts['year']:04d}-{parts['month']:02d}-{parts['day']:02d}"
    else:
        when_stored = (
            f"{parts['year']:04d}-{parts['month']:02d}-{parts['day']:02d}"
            f"T{int(parts['hour']):02d}:{int(parts['minute'] or 0):02d}"
        )

    task_id = hashlib.sha256(
        f"{text}|{when_stored}|{time.time_ns()}".encode("utf-8")
    ).hexdigest()[:16]
    event = _build_event(text, parts, recurring_norm, "task", task_id=task_id)
    tasks = load_tasks()
    tasks.append(
        {
            "id": event["id"],
            "text": text[:300],
            "when": when_stored,
            "recurring": recurring_norm,
        }
    )
    _save_tasks(tasks)
    return {"ok": True, "task": _public_item(event) | {"when": when_stored}}


def remove_task(task_id: str) -> dict[str, Any]:
    task_id = (task_id or "").strip()
    if not task_id:
        return {"ok": False, "error": "id is required"}
    tasks = load_tasks()
    kept = [task for task in tasks if str(task.get("id") or "") != task_id]
    if len(kept) == len(tasks):
        return {
            "ok": False,
            "error": (
                f"No scheduled task with id '{task_id}'. "
                "Anniversaries stored in memory are removed with the memory tool."
            ),
        }
    _save_tasks(kept)
    return {"ok": True, "deleted": task_id}


if __name__ == "__main__":
    print(json.dumps(run_reminders(dry_run=True), indent=2))
