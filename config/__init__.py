"""Project paths and the editable runtime settings file."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = CONFIG_DIR / "settings.json"
SCHEDULES_PATH = CONFIG_DIR / "schedules.json"
SYSTEM_PROMPT_PATH = CONFIG_DIR / "system_prompt.md"


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(data: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
