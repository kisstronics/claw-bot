"""Back-compat entry. The reminder agent lives in agents.reminder."""

from agents.reminder import *  # noqa: F403
from agents.reminder import run_reminders

if __name__ == "__main__":
    import json

    print(json.dumps(run_reminders(dry_run=True), indent=2))
