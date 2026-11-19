"""
Append-only token usage logger for Bedrock agent turns.

Writes one line per turn to token-usage.log:
  timestamp | model | input_tokens | output_tokens | total_tokens |
  tools | rounds | duration_sec
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
from pathlib import Path
from typing import Any

LOG_PATH = Path(
    os.environ.get(
        "TOKEN_USAGE_LOG",
        Path(__file__).with_name("token-usage.log"),
    )
)


def _ts() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def extract_usage(response: dict[str, Any]) -> tuple[int, int]:
    """Return (input_tokens, output_tokens) from a provider converse response."""
    usage = response.get("usage") or {}
    inp = int(usage.get("inputTokens") or usage.get("input_tokens") or 0)
    out = int(usage.get("outputTokens") or usage.get("output_tokens") or 0)
    return inp, out


def log_turn(
    *,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    tools_used: list[str],
    duration_sec: float,
    rounds: int = 1,
    user_id: str = "",
) -> str:
    """
    Append a one-liner usage record. Returns the written line.
    """
    total = int(input_tokens) + int(output_tokens)
    tools = ",".join(tools_used) if tools_used else "-"
    line = (
        f"{_ts()} | model={model_id} | input={int(input_tokens)} | "
        f"output={int(output_tokens)} | total={total} | "
        f"tools={tools} | rounds={int(rounds)} | "
        f"user={user_id or '-'} | "
        f"duration_sec={duration_sec:.3f}"
    )
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line


_LINE_RE = re.compile(
    r"^(?P<ts>.+?) \| model=(?P<model>.+?) \| input=(?P<input>\d+) \| "
    r"output=(?P<output>\d+) \| total=(?P<total>\d+) \| tools=(?P<tools>.*?) \| "
    r"rounds=(?P<rounds>\d+) \| (?:user=(?P<user>.*?) \| )?"
    r"duration_sec=(?P<duration>[0-9.]+)\s*$"
)

# Rough USD per 1M tokens. Free-tier model ids ending in :free are treated as $0.
_DEFAULT_PRICE = (1.0, 3.0)
_PRICE_HINTS: list[tuple[str, tuple[float, float]]] = [
    (":free", (0.0, 0.0)),
    ("llama-3.3-70b", (0.59, 0.79)),
    ("gpt-4o-mini", (0.15, 0.60)),
    ("haiku", (1.0, 5.0)),
    ("qwen", (0.20, 0.60)),
]


def _price_per_million(model_id: str) -> tuple[float, float]:
    lowered = model_id.lower()
    for hint, price in _PRICE_HINTS:
        if hint in lowered:
            return price
    return _DEFAULT_PRICE


def _cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    input_rate, output_rate = _price_per_million(model_id)
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


def read_usage(limit: int = 100, user_id: str | None = None) -> dict[str, Any]:
    """Summarize recent turns from the usage log, with an estimated USD cost."""
    limit = max(1, min(int(limit), 500))
    if not LOG_PATH.exists():
        return {
            "turns": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
            "avg_duration_sec": 0.0,
            "recent": [],
            "note": "Estimates use public list prices and are not an invoice.",
        }
    rows: list[dict[str, Any]] = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        match = _LINE_RE.match(line.strip())
        if not match:
            continue
        model = match.group("model")
        inp = int(match.group("input"))
        out = int(match.group("output"))
        rows.append(
            {
                "ts": match.group("ts"),
                "model": model,
                "input_tokens": inp,
                "output_tokens": out,
                "total_tokens": int(match.group("total")),
                "tools": [] if match.group("tools") == "-" else match.group("tools").split(","),
                "rounds": int(match.group("rounds")),
                "duration_sec": float(match.group("duration")),
                "user": match.group("user") or "",
                "estimated_cost_usd": round(_cost(model, inp, out), 6),
            }
        )
    if user_id:
        rows = [row for row in rows if row.get("user") == user_id]
    recent = rows[-limit:]
    recent.reverse()
    input_tokens = sum(row["input_tokens"] for row in rows)
    output_tokens = sum(row["output_tokens"] for row in rows)
    durations = [row["duration_sec"] for row in rows]
    cost = sum(_cost(row["model"], row["input_tokens"], row["output_tokens"]) for row in rows)
    return {
        "turns": len(rows),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated_cost_usd": round(cost, 6),
        "avg_duration_sec": round(sum(durations) / len(durations), 3) if durations else 0.0,
        "recent": recent,
        "note": "Estimates use public list prices and are not an invoice.",
    }
