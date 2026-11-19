"""ASGI entrypoint for Vercel, plus a local start helper."""

from __future__ import annotations

import os
import sys

if sys.version_info < (3, 12):
    raise SystemExit(
        f"Python 3.12+ is required (found {sys.version.split()[0]}). "
        "Install 3.12+ and recreate your venv."
    )

from api import app

__all__ = ["app", "run"]


def run() -> None:
    """Start the API with uvicorn (used by `pip install -e .` → `start`)."""
    import uvicorn

    port = int(os.environ.get("PORT", "8500"))
    uvicorn.run(
        "api:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=port,
        reload=os.environ.get("RELOAD", "").lower() in {"1", "true", "yes"},
    )


if __name__ == "__main__":
    run()
