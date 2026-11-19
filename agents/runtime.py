"""
Minimal agent with a tool-calling loop.
Model providers live in models/ (Bedrock, Groq) — no agent frameworks.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agents.news_agent import run_news_digest
from agents.reminder import add_task, list_reminders, remove_task, run_reminders
from channels.gmail import read_last_emails, send_email
from channels.telegram import bot_handler
from config import SYSTEM_PROMPT_PATH
import memory_store
from tools.basic import clock_prompt_block, now_tool, resolve_date_tool
from user_scope import current_user_id
from models import (
    AWS_REGION,
    MODEL_ID,
    PROVIDER_NAME,
    get_provider,
    make_client,  # re-exported for callers / back-compat
)
import token_tracker

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAX_TOOL_ROUNDS = 20
COMMAND_TIMEOUT_SEC = 60

_SYSTEM_PROMPT_FILE = SYSTEM_PROMPT_PATH

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant with filesystem memory. "
    "Use the memory tool to remember/recall facts. "
    "Telegram is the default notify channel."
)


def load_system_prompt() -> str:
    if _SYSTEM_PROMPT_FILE.exists():
        text = _SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
        if text:
            return text
    return DEFAULT_SYSTEM_PROMPT


def build_system_prompt() -> str:
    """Base prompt + system clock + live core memory from disk."""
    base = load_system_prompt()
    core = memory_store.load_core_memory()
    return (
        f"{base}\n\n"
        f"{clock_prompt_block()}\n\n"
        "## Persistent memory (from memory/MEMORY.md)\n"
        f"{core}\n"
    )


memory_store.ensure_layout()

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

TOOL_CONFIG: dict[str, Any] = {
    "tools": [
        {
            "toolSpec": {
                "name": "now",
                "description": (
                    "Current local date and time from the system clock. "
                    "Use this for today. Do not invent the date."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "resolve_date",
                "description": (
                    "Turn a relative date into a calendar date using the system clock. "
                    "Examples: today, tomorrow, yesterday, tonight, now, "
                    "in 2 days, in 3 hours, this friday, next monday, tomorrow at 9:30am."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "Relative date phrase to resolve.",
                            },
                        },
                        "required": ["text"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "run_command",
                "description": (
                    "Execute a shell/terminal command on the local machine and "
                    "return stdout, stderr, and exit code."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "The shell command to execute.",
                            },
                        },
                        "required": ["command"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "read_gmail",
                "description": (
                    "Read newest Gmail inbox messages. Fetched messages are marked "
                    "as read in Gmail. Optionally limit to unread only."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "description": "How many recent emails to fetch (1-50). Default 10.",
                            },
                            "unread_only": {
                                "type": "boolean",
                                "description": "If true, only fetch unread messages. Default false.",
                            },
                        },
                        "required": [],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "send_gmail",
                "description": (
                    "Send a plain-text email through the configured Gmail account. "
                    "Use this whenever the user asks to send an email. "
                    "Requires to, subject, and body."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "to": {
                                "type": "string",
                                "description": "Recipient email address.",
                            },
                            "subject": {
                                "type": "string",
                                "description": "Email subject line.",
                            },
                            "body": {
                                "type": "string",
                                "description": "Plain-text email body.",
                            },
                        },
                        "required": ["to", "subject", "body"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "telegram_bot_handler",
                "description": (
                    "Telegram BotHandler — default user communication channel. "
                    "Actions: send (notify user), check_messages (recent inbound "
                    "inbox), status (bot/inbox info). Prefer action=send to notify "
                    "about email summaries, alerts, or job results."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "description": "send | check_messages | status",
                            },
                            "message": {
                                "type": "string",
                                "description": "Text to send (required for action=send).",
                            },
                            "chat_id": {
                                "type": "string",
                                "description": (
                                    "Optional. Omit to reply in this chat."
                                ),
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max messages for check_messages (default 10).",
                            },
                            "clear": {
                                "type": "boolean",
                                "description": "Clear inbox after check_messages.",
                            },
                        },
                        "required": ["action"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "memory",
                "description": (
                    "Filesystem memory. Actions: remember (save fact/note), "
                    "recall (read core or a note by key), list (note keys), "
                    "forget (delete note or clear core), search (grep memory files), "
                    "journal (append event), read_journal. "
                    "Use remember for durable facts the user wants kept; "
                    "use key for named notes (e.g. preferences, projects)."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "description": (
                                    "remember | recall | list | forget | search | "
                                    "journal | read_journal"
                                ),
                            },
                            "content": {
                                "type": "string",
                                "description": "Text to save (remember/journal).",
                            },
                            "key": {
                                "type": "string",
                                "description": (
                                    "Note name for remember/recall/forget. "
                                    "Omit to use core MEMORY.md."
                                ),
                            },
                            "query": {
                                "type": "string",
                                "description": "Search string for action=search.",
                            },
                            "append": {
                                "type": "boolean",
                                "description": "Append on remember (default true).",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max results for search/read_journal.",
                            },
                        },
                        "required": ["action"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "news_digest",
                "description": (
                    "Run the news agent: fetch top world headlines, email up to 5 "
                    "new one-liners (skips stories already emailed), and remember "
                    "sent ids on disk. Optional dry_run returns body without sending."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "dry_run": {
                                "type": "boolean",
                                "description": "If true, do not send email.",
                            },
                            "to": {
                                "type": "string",
                                "description": "Optional recipient override.",
                            },
                        },
                        "required": [],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "reminder",
                "description": (
                    "Anniversaries for today and scheduled task reminders. "
                    "An hourly job already notifies on Telegram once per occurrence. "
                    "Actions: add (text + when), list, remove (id), run (send due now)."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "description": "add | list | remove | run",
                            },
                            "text": {
                                "type": "string",
                                "description": "What to remind (required for add).",
                            },
                            "when": {
                                "type": "string",
                                "description": (
                                    "Date or time from the system clock: today, tomorrow, "
                                    "next monday, YYYY-MM-DD, YYYY-MM-DDTHH:MM, "
                                    "'30 July', or a clock time such as 10:55."
                                ),
                            },
                            "recurring": {
                                "type": "string",
                                "description": "yearly, daily, or omit for a one-shot task.",
                            },
                            "id": {
                                "type": "string",
                                "description": "Task id for action=remove.",
                            },
                            "dry_run": {
                                "type": "boolean",
                                "description": "For action=run, list due items without sending.",
                            },
                        },
                        "required": ["action"],
                    }
                },
            }
        },
    ]
}


def run_command(command: str) -> dict[str, Any]:
    command = (command or "").strip()
    if not command:
        return {"stdout": "", "stderr": "command is required", "exit_code": -1}
    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SEC,
            stdin=subprocess.DEVNULL,
        )
        return {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "exit_code": completed.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Command timed out after {COMMAND_TIMEOUT_SEC}s",
            "exit_code": -1,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "stdout": "",
            "stderr": str(exc),
            "exit_code": -1,
        }


def send_gmail(inp: dict[str, Any]) -> dict[str, Any]:
    """Send one email. Used when a Telegram message asks to send mail."""
    to_addr = str(inp.get("to") or inp.get("recipient") or "").strip()
    subject = str(inp.get("subject") or "").strip()
    body = str(inp.get("body") or inp.get("message") or inp.get("text") or "").strip()
    if "@" not in to_addr:
        return {"ok": False, "error": "to is required and must be an email address."}
    if not subject:
        return {"ok": False, "error": "subject is required."}
    if not body:
        return {"ok": False, "error": "body is required."}
    return send_email(subject=subject, body=body, to_addr=to_addr)


def read_gmail(limit: int = 10, unread_only: bool = False) -> dict[str, Any]:
    try:
        messages = read_last_emails(
            limit=limit,
            unread_only=unread_only,
            mark_as_read=True,
        )
        return {
            "count": len(messages),
            "marked_as_read": True,
            "messages": messages,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "count": 0, "messages": []}


def telegram_bot_handler_tool(inp: dict[str, Any]) -> dict[str, Any]:
    return bot_handler.handle(
        str(inp.get("action", "")),
        message=inp.get("message"),
        text=inp.get("text"),
        chat_id=inp.get("chat_id"),
        limit=inp.get("limit", 10),
        clear=inp.get("clear", False),
    )


def memory_tool(inp: dict[str, Any]) -> dict[str, Any]:
    return memory_store.handle(
        str(inp.get("action", "")),
        content=inp.get("content"),
        event=inp.get("event"),
        key=inp.get("key"),
        query=inp.get("query"),
        append=inp.get("append", True),
        limit=inp.get("limit", 10),
    )


def news_digest_tool(inp: dict[str, Any]) -> dict[str, Any]:
    return run_news_digest(
        dry_run=bool(inp.get("dry_run", False)),
        to_addr=inp.get("to"),
    )


_REMINDER_ACTIONS = {"add", "list", "remove", "delete", "run", "check"}
_REMINDER_RESERVED = {"text", "message", "when", "time", "at", "recurring", "id", "dry_run"}


def _reminder_value(inp: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).lower(): value for key, value in inp.items()}
    for name in names:
        value = lowered.get(name)
        if value not in (None, ""):
            return value
    return None


def _reminder_action(inp: dict[str, Any]) -> str:
    action = str(_reminder_value(inp, "action") or "").strip().lower()
    if action:
        return action
    for key, value in inp.items():
        if str(key).lower() in _REMINDER_RESERVED:
            continue
        candidate = str(value or "").strip().lower()
        if candidate in _REMINDER_ACTIONS:
            return candidate
    if _reminder_value(inp, "text", "message") and _reminder_value(inp, "when", "time", "at"):
        return "add"
    if _reminder_value(inp, "id"):
        return "remove"
    return ""


def reminder_tool(inp: dict[str, Any]) -> dict[str, Any]:
    action = _reminder_action(inp)
    if action in {"run", "check"}:
        return run_reminders(dry_run=bool(inp.get("dry_run", False)))
    if action == "add":
        return add_task(
            str(_reminder_value(inp, "text", "message") or ""),
            str(_reminder_value(inp, "when", "time", "at") or ""),
            recurring=_reminder_value(inp, "recurring"),
        )
    if action == "list":
        return list_reminders()
    if action in {"remove", "delete"}:
        return remove_task(str(_reminder_value(inp, "id") or ""))
    return {
        "ok": False,
        "error": "Unknown action. Use add, list, remove, or run.",
    }


TOOL_HANDLERS = {
    "now": now_tool,
    "resolve_date": resolve_date_tool,
    "run_command": lambda inp: run_command(str(inp.get("command") or "")),
    "read_gmail": lambda inp: read_gmail(
        int(inp.get("limit", 10)),
        bool(inp.get("unread_only", False)),
    ),
    "send_gmail": send_gmail,
    "telegram_bot_handler": telegram_bot_handler_tool,
    "memory": memory_tool,
    "news_digest": news_digest_tool,
    "reminder": reminder_tool,
    "send_telegram": lambda inp: bot_handler.send(
        inp["message"],
        chat_id=inp.get("chat_id"),
    ),
}


def dispatch_tool(name: str, tool_input: dict[str, Any]) -> str:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        result: Any = {"error": f"Unknown tool: {name}"}
    else:
        print(f"  → tool `{name}` input={json.dumps(tool_input)}")
        try:
            result = handler(tool_input)
        except Exception as exc:  # noqa: BLE001
            result = {"error": str(exc)}
        print(f"  ← tool `{name}` result={_preview(result)}")
    return json.dumps(result)


def _preview(obj: Any, limit: int = 400) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def extract_text(content_blocks: list[dict]) -> str:
    parts = [b["text"] for b in content_blocks if "text" in b]
    return "\n".join(parts).strip()


def run_agent_turn(provider, messages: list[dict], user_text: str) -> str:
    """Append a user message and loop until the model stops requesting tools."""
    if provider is None or not hasattr(provider, "converse"):
        provider = get_provider()

    messages.append({"role": "user", "content": [{"text": user_text}]})

    started = time.perf_counter()
    input_tokens = 0
    output_tokens = 0
    tools_used: list[str] = []
    rounds = 0
    reply = "Stopped: reached max tool-calling rounds."
    model_id = getattr(provider, "model_id", MODEL_ID)

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = provider.converse(
                messages,
                system=build_system_prompt(),
                tool_config=TOOL_CONFIG,
            )
            rounds += 1
            inp, out = token_tracker.extract_usage(response)
            input_tokens += inp
            output_tokens += out

            output = response["output"]["message"]
            stop_reason = response["stopReason"]
            messages.append(output)

            if stop_reason != "tool_use":
                reply = extract_text(output["content"]) or "(no text response)"
                break

            tool_results: list[dict] = []
            for block in output["content"]:
                if "toolUse" not in block:
                    continue
                tool_use = block["toolUse"]
                tools_used.append(tool_use["name"])
                result_json = dispatch_tool(tool_use["name"], tool_use["input"])
                tool_results.append(
                    {
                        "toolResult": {
                            "toolUseId": tool_use["toolUseId"],
                            "content": [{"json": json.loads(result_json)}],
                        }
                    }
                )

            messages.append({"role": "user", "content": tool_results})
    finally:
        duration = time.perf_counter() - started
        try:
            line = token_tracker.log_turn(
                model_id=f"{getattr(provider, 'provider_name', PROVIDER_NAME)}:{model_id}",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                tools_used=tools_used,
                duration_sec=duration,
                rounds=rounds,
                user_id=current_user_id() or "",
            )
            print(f"  ⚙ {line}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚙ token log failed: {exc}", file=sys.stderr)

    return reply


def run_agent_once(user_text: str, messages: list[dict] | None = None) -> str:
    history = messages if messages is not None else []
    return run_agent_turn(get_provider(), history, user_text)


def chat_loop() -> None:
    provider = get_provider()
    print(
        f"Agent  provider={provider.provider_name}  "
        f"model={provider.model_id}  region={AWS_REGION}"
    )
    print("Type a message (or 'quit' to exit).\n")

    messages: list[dict] = []

    while True:
        try:
            user_text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            break

        if not user_text:
            continue
        if user_text.lower() in {"quit", "exit", "q"}:
            print("bye")
            break

        try:
            reply = run_agent_turn(provider, messages, user_text)
        except Exception as exc:  # noqa: BLE001
            print(f"error: {exc}", file=sys.stderr)
            if messages and messages[-1]["role"] == "user":
                messages.pop()
            continue

        print(f"agent> {reply}\n")


if __name__ == "__main__":
    chat_loop()
