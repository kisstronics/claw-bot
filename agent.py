"""Back-compat entry. The agent loop lives in agents.runtime."""

from agents.runtime import *  # noqa: F403
from agents.runtime import chat_loop

if __name__ == "__main__":
    chat_loop()
