"""
Append-only token usage logger for agent turns (Postgres claw.token_usage).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import db


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
    """Insert a usage record. Returns a display line."""
    total = int(input_tokens) + int(output_tokens)
    tools = ",".join(tools_used) if tools_used else "-"
    line = (
        f"{_ts()} | model={model_id} | input={int(input_tokens)} | "
        f"output={int(output_tokens)} | total={total} | "
        f"tools={tools} | rounds={int(rounds)} | "
        f"user={user_id or '-'} | "
        f"duration_sec={duration_sec:.3f}"
    )
    db.execute(
        """
        INSERT INTO token_usage (
            model, input_tokens, output_tokens, total_tokens,
            tools, rounds, user_id, duration_sec
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            model_id,
            int(input_tokens),
            int(output_tokens),
            total,
            tools,
            int(rounds),
            user_id or None,
            float(duration_sec),
        ),
    )
    return line


_DEFAULT_PRICE = (1.0, 3.0)
_PRICE_HINTS: list[tuple[str, tuple[float, float]]] = [
    (":free", (0.0, 0.0)),
    ("llama-3.3-70b", (0.59, 0.79)),
    ("gpt-4o-mini", (0.15, 0.60)),
    ("haiku", (1.0, 5.0)),
    ("qwen", (0.20, 0.60)),
    ("nemotron", (0.0, 0.0)),
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
    """Summarize recent turns from Postgres, with an estimated USD cost."""
    limit = max(1, min(int(limit), 500))
    if user_id:
        rows_raw = db.fetch_all(
            """
            SELECT ts, model, input_tokens, output_tokens, total_tokens,
                   tools, rounds, user_id, duration_sec
            FROM token_usage
            WHERE user_id = %s
            ORDER BY ts DESC
            LIMIT %s
            """,
            (user_id, limit),
        )
        all_raw = db.fetch_all(
            """
            SELECT model, input_tokens, output_tokens, duration_sec
            FROM token_usage
            WHERE user_id = %s
            """,
            (user_id,),
        )
    else:
        rows_raw = db.fetch_all(
            """
            SELECT ts, model, input_tokens, output_tokens, total_tokens,
                   tools, rounds, user_id, duration_sec
            FROM token_usage
            ORDER BY ts DESC
            LIMIT %s
            """,
            (limit,),
        )
        all_raw = db.fetch_all(
            "SELECT model, input_tokens, output_tokens, duration_sec FROM token_usage"
        )

    recent: list[dict[str, Any]] = []
    for row in rows_raw:
        model = str(row["model"])
        inp = int(row["input_tokens"] or 0)
        out = int(row["output_tokens"] or 0)
        tools = str(row.get("tools") or "-")
        ts = row["ts"]
        recent.append(
            {
                "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "model": model,
                "input_tokens": inp,
                "output_tokens": out,
                "total_tokens": int(row["total_tokens"] or inp + out),
                "tools": [] if tools == "-" else tools.split(","),
                "rounds": int(row["rounds"] or 1),
                "duration_sec": float(row["duration_sec"] or 0),
                "user": str(row.get("user_id") or ""),
                "estimated_cost_usd": round(_cost(model, inp, out), 6),
            }
        )

    input_tokens = sum(int(row["input_tokens"] or 0) for row in all_raw)
    output_tokens = sum(int(row["output_tokens"] or 0) for row in all_raw)
    durations = [float(row["duration_sec"] or 0) for row in all_raw]
    cost = sum(
        _cost(str(row["model"]), int(row["input_tokens"] or 0), int(row["output_tokens"] or 0))
        for row in all_raw
    )
    return {
        "turns": len(all_raw),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated_cost_usd": round(cost, 6),
        "avg_duration_sec": round(sum(durations) / len(durations), 3) if durations else 0.0,
        "recent": recent,
        "note": "Estimates use public list prices and are not an invoice.",
    }
