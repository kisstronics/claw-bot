"""
Telegram channel: outbound sends and inbound webhook updates.

Env:
  TELEGRAM_BOT_TOKEN      — BotFather token
  TELEGRAM_WEBHOOK_URL    — public https URL of POST /telegram/webhook
  TELEGRAM_WEBHOOK_SECRET — optional secret Telegram echoes back
  TELEGRAM_SSL_VERIFY     — true/false. Set false behind corp SSL inspection.
  TELEGRAM_CA_BUNDLE      — optional path to a corporate CA PEM
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import ssl
import threading
import time
from typing import Any, Callable

import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CA_BUNDLE = os.environ.get("TELEGRAM_CA_BUNDLE", "").strip()
_BOT_PUBLIC: dict[str, str] | None = None


def _ssl_verify_enabled() -> bool:
    raw = os.environ.get("TELEGRAM_SSL_VERIFY", "false").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _ssl_verify_setting() -> bool | str:
    """httpx/urllib verify value: path, True, or False."""
    if CA_BUNDLE:
        return CA_BUNDLE
    return _ssl_verify_enabled()


def _urllib_ssl_context() -> ssl.SSLContext | None:
    setting = _ssl_verify_setting()
    if isinstance(setting, str):
        return ssl.create_default_context(cafile=setting)
    if setting:
        return None  # default system verify
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def bot_public_identity() -> dict[str, str]:
    """Username and numeric id for the Login Widget. Never includes the token."""
    global _BOT_PUBLIC
    if _BOT_PUBLIC and _BOT_PUBLIC.get("username"):
        return dict(_BOT_PUBLIC)
    result = _api_call("getMe")
    if not result.get("ok"):
        return {"username": "", "id": "", "error": result.get("error") or "Could not reach Telegram."}
    user = result.get("result") if isinstance(result.get("result"), dict) else {}
    username = str(user.get("username") or "").strip()
    if not username:
        return {"username": "", "id": "", "error": "This bot has no username."}
    _BOT_PUBLIC = {"username": username, "id": str(user.get("id") or "")}
    return dict(_BOT_PUBLIC)


MessageHandlerFn = Callable[[str, str, str], str]


def _api_call(method: str, **fields: Any) -> dict[str, Any]:
    if not TOKEN:
        return {"ok": False, "error": "Set TELEGRAM_BOT_TOKEN environment variable."}

    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    try:
        data = None
        cleaned: dict[str, Any] = {}
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, bool):
                cleaned[key] = "true" if value else "false"
            else:
                cleaned[key] = value
        if cleaned:
            data = urllib.parse.urlencode(cleaned).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        ctx = _urllib_ssl_context()
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        return {"ok": False, "error": f"HTTP {exc.code}: {detail[:300]}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


class TelegramBotHandler:
    """
    Simple Telegram bot handler used as an agent tool.

    Actions:
      - send: notify a chat
      - check_messages: return recent inbound messages from the inbox buffer
      - status: bot config / inbox stats
    """

    def __init__(self, inbox_limit: int = 100) -> None:
        self._inbox: list[dict[str, Any]] = []
        self._inbox_limit = inbox_limit
        self._lock = threading.Lock()

    def send(
        self,
        message: str,
        chat_id: str | None = None,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from accounts import chat_for_send

        target, error = chat_for_send(chat_id)
        if error:
            return {"ok": False, "error": error}
        result = _api_call(
            "sendMessage",
            chat_id=target,
            text=str(message)[:4000],
            reply_markup=json.dumps(reply_markup) if reply_markup else None,
        )
        if result.get("ok"):
            return {"ok": True, "chat_id": target, "message_id": (
                (result.get("result") or {}).get("message_id")
            )}
        return {
            "ok": False,
            "error": result.get("error") or result.get("description") or str(result),
            "chat_id": target,
        }

    def record_inbound(
        self,
        chat_id: str,
        text: str,
        *,
        user_id: str | None = None,
        user: str | None = None,
        username: str | None = None,
    ) -> None:
        item = {
            "chat_id": str(chat_id),
            "user_id": str(user_id or ""),
            "text": text,
            "user": user,
            "username": username,
            "ts": time.time(),
        }
        with self._lock:
            self._inbox.append(item)
            if len(self._inbox) > self._inbox_limit:
                self._inbox = self._inbox[-self._inbox_limit :]

    def check_messages(
        self,
        limit: int = 10,
        *,
        clear: bool = False,
    ) -> dict[str, Any]:
        from user_scope import current_user_id

        limit = max(1, min(int(limit), 50))
        uid = current_user_id() or ""
        with self._lock:
            mine = [item for item in self._inbox if item.get("user_id") == uid] if uid else []
            messages = list(mine[-limit:])
            if clear and uid:
                self._inbox = [item for item in self._inbox if item.get("user_id") != uid]
        return {"ok": True, "count": len(messages), "messages": messages}

    def status(self) -> dict[str, Any]:
        from user_scope import current_user_id

        uid = current_user_id() or ""
        with self._lock:
            inbox_count = sum(1 for item in self._inbox if item.get("user_id") == uid)
        return {
            "ok": True,
            "token_configured": bool(TOKEN),
            "mode": "webhook",
            "webhook_configured": bool(os.environ.get("TELEGRAM_WEBHOOK_URL", "").strip()),
            "inbox_count": inbox_count,
        }

    def handle(self, action: str, **kwargs: Any) -> dict[str, Any]:
        """Dispatch tool actions: send | check_messages | status."""
        action = (action or "").strip().lower()
        if action in {"send", "notify", "send_message"}:
            message = kwargs.get("message") or kwargs.get("text")
            if not message:
                return {"ok": False, "error": "message is required for send"}
            return self.send(str(message), chat_id=kwargs.get("chat_id"))
        if action in {"check_messages", "check", "read", "inbox"}:
            return self.check_messages(
                limit=int(kwargs.get("limit", 10)),
                clear=bool(kwargs.get("clear", False)),
            )
        if action in {"status", "info"}:
            return self.status()
        return {
            "ok": False,
            "error": f"Unknown action '{action}'. Use send, check_messages, or status.",
        }


# Shared singleton used by the agent tool and webhook updates.
bot_handler = TelegramBotHandler()


def send_message(text: str, chat_id: str | None = None) -> dict[str, Any]:
    return bot_handler.send(text, chat_id=chat_id)


SHARE_KEYBOARD = {
    "keyboard": [[{"text": "Share phone number", "request_contact": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}
REMOVE_KEYBOARD = {"remove_keyboard": True}


def webhook_secret() -> str:
    return os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()


def webhook_secret_ok(header_value: str | None) -> bool:
    secret = webhook_secret()
    if not secret:
        return True
    got = header_value or ""
    if len(got) != len(secret):
        return False
    return hmac.compare_digest(secret, got)


def configure_webhook() -> dict[str, Any]:
    url = os.environ.get("TELEGRAM_WEBHOOK_URL", "").strip()
    if not TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN is not set; webhook was not registered.")
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN is not set"}
    if not url:
        logger.warning(
            "TELEGRAM_WEBHOOK_URL is not set. Telegram updates will not arrive "
            "until a public HTTPS URL is configured."
        )
        return {"ok": False, "error": "TELEGRAM_WEBHOOK_URL is not set"}
    result = _api_call(
        "setWebhook",
        url=url,
        secret_token=webhook_secret() or None,
        drop_pending_updates=False,
        allowed_updates=json.dumps(["message"]),
    )
    if result.get("ok"):
        logger.info("Telegram webhook registered")
    else:
        logger.error(
            "Telegram setWebhook failed: %s",
            result.get("error") or result.get("description"),
        )
    return result


def dispatch_update(
    update: dict[str, Any],
    on_text: MessageHandlerFn,
    on_user: Callable[[str], None] | None = None,
) -> None:
    """Handle one Telegram update. Replies in the chat that sent it."""
    message = update.get("message") or {}
    if not isinstance(message, dict):
        return
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    user_id = str(sender.get("id") or "")
    chat_id = str(chat.get("id") or "")
    if not user_id or not chat_id:
        return

    from accounts import ensure_user, link_phone
    from user_scope import reset_reply_chat, reset_user, use_reply_chat, use_user

    private = chat.get("type") == "private"
    name = " ".join(
        part for part in (sender.get("first_name"), sender.get("last_name")) if part
    ).strip()
    user_token = use_user(user_id)
    reply_token = use_reply_chat(chat_id)
    try:
        ensure_user(
            user_id,
            chat_id,
            name=name,
            username=str(sender.get("username") or ""),
            private=private,
        )
        if on_user:
            on_user(user_id)

        contact = message.get("contact") if isinstance(message.get("contact"), dict) else None
        text = str(message.get("text") or "").strip()
        print(
            f"[telegram] message keys={','.join(sorted(message))} contact={bool(contact)} "
            f"text_digits={sum(ch.isdigit() for ch in text)}",
            flush=True,
        )
        if contact:
            contact_user = str(contact.get("user_id") or "")
            if contact_user and contact_user != user_id:
                bot_handler.send(
                    "Share your own Telegram number with the button.",
                    chat_id=chat_id,
                    reply_markup=SHARE_KEYBOARD,
                )
                return
            try:
                link_phone(user_id, str(contact.get("phone_number") or ""))
            except (ValueError, LookupError) as exc:
                bot_handler.send(str(exc), chat_id=chat_id)
                return
            print("[telegram] contact phone linked", flush=True)
            bot_handler.send(
                "Phone number saved. Sign in on the website with Log in with Telegram.",
                chat_id=chat_id,
                reply_markup=REMOVE_KEYBOARD,
            )
            return

        if not text:
            return
        from accounts import phone_from_text

        typed_phone = phone_from_text(text)
        if typed_phone and private:
            try:
                link_phone(user_id, typed_phone)
            except (ValueError, LookupError) as exc:
                bot_handler.send(str(exc), chat_id=chat_id)
                return
            print(f"[telegram] phone linked digits={len(typed_phone)}", flush=True)
            bot_handler.send(
                "Phone number saved. Sign in on the website with Log in with Telegram.",
                chat_id=chat_id,
                reply_markup=REMOVE_KEYBOARD,
            )
            return
        command = text.split()[0].split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            bot_handler.send(
                "Knics is ready.\n\n"
                "On the website, tap Log in with Telegram and confirm in this app. "
                "You can also type here to talk to your agent.",
                chat_id=chat_id,
            )
            return

        bot_handler.record_inbound(
            chat_id,
            text,
            user_id=user_id,
            user=name or None,
            username=sender.get("username"),
        )
        try:
            reply = on_text(user_id, chat_id, text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent error from Telegram")
            reply = f"Error: {exc}"
        bot_handler.send((reply or "(no reply)")[:4000], chat_id=chat_id)
    finally:
        reset_reply_chat(reply_token)
        reset_user(user_token)


