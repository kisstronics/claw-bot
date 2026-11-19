#!/usr/bin/env python3
"""Promote a user to admin by email or user_id.

Usage:
  .venv/bin/python scripts/set_admin.py chvkishore.20@gmail.com
  .venv/bin/python scripts/set_admin.py g_111142601352535069448
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import accounts
import db


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: set_admin.py <email-or-user-id> [role]", file=sys.stderr)
        return 2
    target = sys.argv[1]
    role = sys.argv[2] if len(sys.argv) > 2 else "admin"
    db.init_db()
    user = accounts.set_user_role(target, role)
    print(f"Updated {user.get('email') or user.get('user_id')} → role={user.get('role')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
