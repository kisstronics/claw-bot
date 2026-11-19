"""
Small factual tools. Dates always come from the system clock.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

_WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

_RELATIVE_RE = re.compile(
    r"^(today|tomorrow|yesterday|tonight|now)(?:\s+(?:at\s+)?(.+))?$",
    re.I,
)
_IN_RE = re.compile(
    r"^in\s+(\d+)\s+(minutes?|hours?|days?|weeks?)$",
    re.I,
)
_WEEKDAY_RE = re.compile(
    r"^(this|next)\s+([a-z]+)(?:\s+(?:at\s+)?(.+))?$",
    re.I,
)
_CLOCK_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.I)


def _local_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now().astimezone()
    if now.tzinfo is None:
        return now
    return now.astimezone()


def _clock(text: str) -> tuple[int, int] | None:
    match = _CLOCK_RE.fullmatch((text or "").strip())
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


def _dated(
    moment: datetime,
    *,
    hour: int | None = None,
    minute: int | None = None,
) -> dict[str, Any]:
    day = moment.date()
    result: dict[str, Any] = {
        "ok": True,
        "date": day.isoformat(),
        "year": day.year,
        "month": day.month,
        "day": day.day,
        "weekday": day.strftime("%A"),
        "hour": hour,
        "minute": 0 if hour is not None and minute is None else minute,
    }
    if hour is None:
        result["time"] = None
        result["iso"] = day.isoformat()
    else:
        result["time"] = f"{hour:02d}:{int(result['minute'] or 0):02d}"
        result["iso"] = f"{day.isoformat()}T{result['time']}"
    return result


def system_now(now: datetime | None = None) -> dict[str, Any]:
    """Current local date and time from the system clock."""
    current = _local_now(now)
    today = current.date()
    return {
        "ok": True,
        "timezone": current.tzname() or "local",
        "iso": current.isoformat(timespec="seconds"),
        "date": today.isoformat(),
        "time": current.strftime("%H:%M:%S"),
        "weekday": current.strftime("%A"),
        "today": today.isoformat(),
        "tomorrow": (today + timedelta(days=1)).isoformat(),
        "yesterday": (today - timedelta(days=1)).isoformat(),
    }


def resolve_relative(text: str, now: datetime | None = None) -> dict[str, Any] | None:
    """
    Resolve a relative phrase against the system clock.
    Returns None when `text` is not a relative date.
    """
    raw = " ".join((text or "").split())
    if not raw:
        return None
    current = _local_now(now)

    relative = _RELATIVE_RE.fullmatch(raw)
    if relative:
        word = relative.group(1).lower()
        clock_text = relative.group(2)
        if word == "tomorrow":
            moment = current + timedelta(days=1)
        elif word == "yesterday":
            moment = current - timedelta(days=1)
        else:
            moment = current
        hour = minute = None
        if clock_text:
            clock = _clock(clock_text)
            if clock is None:
                return {
                    "ok": False,
                    "error": f"Could not read a time from '{clock_text}'.",
                }
            hour, minute = clock
        elif word == "now":
            hour, minute = current.hour, current.minute
        return _dated(moment, hour=hour, minute=minute)

    ahead = _IN_RE.fullmatch(raw)
    if ahead:
        count = int(ahead.group(1))
        unit = ahead.group(2).lower()
        if unit.startswith("minute"):
            moment = current + timedelta(minutes=count)
            return _dated(moment, hour=moment.hour, minute=moment.minute)
        if unit.startswith("hour"):
            moment = current + timedelta(hours=count)
            return _dated(moment, hour=moment.hour, minute=moment.minute)
        if unit.startswith("day"):
            return _dated(current + timedelta(days=count))
        return _dated(current + timedelta(weeks=count))

    named = _WEEKDAY_RE.fullmatch(raw)
    if named:
        which = named.group(1).lower()
        weekday = _WEEKDAYS.get(named.group(2).lower())
        if weekday is None:
            return None
        clock_text = named.group(3)
        hour = minute = None
        if clock_text:
            clock = _clock(clock_text)
            if clock is None:
                return {
                    "ok": False,
                    "error": f"Could not read a time from '{clock_text}'.",
                }
            hour, minute = clock
        delta = (weekday - current.weekday()) % 7
        if which == "next" and delta == 0:
            delta = 7
        return _dated(current + timedelta(days=delta), hour=hour, minute=minute)

    return None


def now_tool(_inp: dict[str, Any] | None = None) -> dict[str, Any]:
    return system_now()


def resolve_date_tool(inp: dict[str, Any]) -> dict[str, Any]:
    text = str(inp.get("text") or inp.get("when") or inp.get("date") or "").strip()
    if not text:
        return {
            "ok": False,
            "error": "text is required. Examples: today, tomorrow, in 2 days, next monday.",
        }
    found = resolve_relative(text)
    if found is None:
        return {
            "ok": False,
            "error": (
                "Not a relative date. Use today, tomorrow, yesterday, "
                "in 2 days, in 3 hours, or next monday."
            ),
        }
    return found


def clock_prompt_block(now: datetime | None = None) -> str:
    clock = system_now(now)
    return (
        "## System clock\n"
        f"Local time: {clock['weekday']} {clock['date']} {clock['time']} ({clock['timezone']}).\n"
        f"Today is {clock['today']}. Yesterday is {clock['yesterday']}. Tomorrow is {clock['tomorrow']}.\n"
        "These dates come from the system clock. Do not guess or replace them."
    )
