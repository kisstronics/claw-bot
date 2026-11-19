"""Project paths and editable runtime settings (Postgres claw.global_settings)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = CONFIG_DIR / "settings.json"  # legacy path; unused when DB is up
SCHEDULES_PATH = CONFIG_DIR / "schedules.json"
SYSTEM_PROMPT_PATH = CONFIG_DIR / "system_prompt.md"


def load_settings() -> dict[str, Any]:
    try:
        import db

        row = db.fetch_one(
            "SELECT provider, model_id FROM global_settings WHERE id = 1"
        )
    except Exception:  # noqa: BLE001 — DB may not be ready during import
        return {}
    if not row:
        return {}
    data = {
        "provider": str(row.get("provider") or ""),
        "model_id": str(row.get("model_id") or ""),
    }
    return {k: v for k, v in data.items() if v}


def save_settings(data: dict) -> None:
    import db

    provider = str((data or {}).get("provider") or "")
    model_id = str((data or {}).get("model_id") or "")
    db.execute(
        """
        INSERT INTO global_settings (id, provider, model_id)
        VALUES (1, %s, %s)
        ON CONFLICT (id) DO UPDATE
        SET provider = EXCLUDED.provider, model_id = EXCLUDED.model_id
        """,
        (provider, model_id),
    )
