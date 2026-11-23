"""
Telegram channel: outbound sends and inbound webhook updates.

Config (claw.app_config / env):
  TELEGRAM_ENABLED        — true/false (default true when token is set)
  TELEGRAM_BOT_TOKEN      — BotFather token
  TELEGRAM_WEBHOOK_URL    — public https URL of POST /telegram/webhook
  TELEGRAM_WEBHOOK_SECRET — optional secret Telegram echoes back
  TELEGRAM_SSL_VERIFY     — true/false. Set false behind corp SSL inspection.
  TELEGRAM_CA_BUNDLE      — optional path to a corporate CA PEM
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import os
import re
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

_BOT_PUBLIC: dict[str, str] | None = None


def _config(key: str, default: str = "") -> str:
    try:
        import db

        return str(db.get_config(key, default) or "").strip()
    except Exception:  # noqa: BLE001
        return os.environ.get(key, default).strip()


def bot_token() -> str:
    return _config("TELEGRAM_BOT_TOKEN")


# Back-compat for callers that still import TOKEN.
TOKEN = ""  # refreshed via bot_token(); do not use for live reads


def clear_bot_public_cache() -> None:
    global _BOT_PUBLIC
    _BOT_PUBLIC = None


def is_enabled() -> bool:
    raw = _config("TELEGRAM_ENABLED", "").lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    # Default: enabled when a token is configured.
    return bool(bot_token())


def webhook_url() -> str:
    return _config("TELEGRAM_WEBHOOK_URL")


def webhook_secret() -> str:
    return _config("TELEGRAM_WEBHOOK_SECRET")


def ca_bundle() -> str:
    return _config("TELEGRAM_CA_BUNDLE")


def _ssl_verify_enabled() -> bool:
    raw = _config("TELEGRAM_SSL_VERIFY", "false").lower()
    return raw not in {"0", "false", "no", "off"}


def _ssl_verify_setting() -> bool | str:
    """httpx/urllib verify value: path, True, or False."""
    bundle = ca_bundle()
    if bundle:
        return bundle
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


def escape_html(value: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html.escape(str(value or ""), quote=False)


def format_html_card(
    *,
    badge: str = "",
    title: str = "",
    subtitle: str = "",
    lines: list[str] | None = None,
    footer: str = "",
) -> str:
    """
    Build a compact HTML card for Telegram.
    Supports b/i/u/s/code/pre/a — keep markup minimal.
    Line items may already include safe HTML (caller responsibility).
    """
    parts: list[str] = []
    if badge:
        parts.append(f"<b>{escape_html(badge)}</b>")
    if title:
        parts.append(f"<b>{escape_html(title)}</b>")
    if subtitle:
        parts.append(f"<i>{escape_html(subtitle)}</i>")
    if lines:
        parts.append("")
        parts.extend(lines)
    if footer:
        parts.append("")
        parts.append(f"<i>{escape_html(footer)}</i>")
    return "\n".join(parts)[:4000]


def format_plain_to_html(text: str) -> str:
    """
    Convert plain / lightly-markdown agent text into Telegram HTML.
    Escapes raw HTML, then applies a small set of safe markdown markers.
    """
    raw = str(text or "")
    if not raw.strip():
        return ""

    # Protect fenced code blocks first.
    fences: list[str] = []

    def _stash_fence(match: re.Match[str]) -> str:
        fences.append(match.group(1))
        return f"\x00FENCE{len(fences) - 1}\x00"

    protected = re.sub(
        r"```(?:\w+)?\n?([\s\S]*?)```",
        _stash_fence,
        raw,
    )

    # Escape remaining text.
    escaped = escape_html(protected)

    # Inline code
    escaped = re.sub(
        r"`([^`\n]+)`",
        lambda m: f"<code>{m.group(1)}</code>",
        escaped,
    )
    # Bold **...**
    escaped = re.sub(
        r"\*\*([^*]+)\*\*",
        lambda m: f"<b>{m.group(1)}</b>",
        escaped,
    )
    # Italic *...* (avoid matching bold leftovers)
    escaped = re.sub(
        r"(?<!\*)\*([^*\n]+)\*(?!\*)",
        lambda m: f"<i>{m.group(1)}</i>",
        escaped,
    )

    # Restore fenced blocks as <pre>
    for index, body in enumerate(fences):
        escaped = escaped.replace(
            f"\x00FENCE{index}\x00",
            f"<pre>{escape_html(body.rstrip())}</pre>",
        )

    return escaped[:4000]


def bot_public_identity() -> dict[str, str]:
    """Username and numeric id for the Login Widget. Never includes the token."""
    global _BOT_PUBLIC
    if _BOT_PUBLIC and _BOT_PUBLIC.get("username"):
        return dict(_BOT_PUBLIC)
    result = _api_call("getMe")
    if not result.get("ok"):
        return {
            "username": "",
            "id": "",
            "error": result.get("error") or "Could not reach Telegram.",
        }
    user = result.get("result") if isinstance(result.get("result"), dict) else {}
    username = str(user.get("username") or "").strip()
    if not username:
        return {"username": "", "id": "", "error": "This bot has no username."}
    _BOT_PUBLIC = {"username": username, "id": str(user.get("id") or "")}
    return dict(_BOT_PUBLIC)


def public_status() -> dict[str, Any]:
    identity = bot_public_identity() if bot_token() else {"username": "", "id": ""}
    return {
        "enabled": is_enabled() and bool(bot_token()),
        "botUsername": identity.get("username") or "",
        "hasBotToken": bool(bot_token()),
        "webhookUrl": webhook_url(),
        "hasWebhookSecret": bool(webhook_secret()),
    }


def get_admin_settings() -> dict[str, Any]:
    return public_status()


def update_settings(
    *,
    enabled: bool | None = None,
    bot_token_value: str | None = None,
    webhook_url_value: str | None = None,
    webhook_secret_value: str | None = None,
) -> dict[str, Any]:
    """Persist Telegram settings into claw.app_config and refresh webhook."""
    import db

    if enabled is not None:
        db.set_config("TELEGRAM_ENABLED", "true" if enabled else "false")
    if bot_token_value is not None and str(bot_token_value).strip():
        db.set_config("TELEGRAM_BOT_TOKEN", str(bot_token_value).strip())
        clear_bot_public_cache()
    if webhook_url_value is not None:
        db.set_config("TELEGRAM_WEBHOOK_URL", str(webhook_url_value).strip())
    if webhook_secret_value is not None and str(webhook_secret_value).strip():
        db.set_config("TELEGRAM_WEBHOOK_SECRET", str(webhook_secret_value).strip())

    # Refresh cached username when we have a token.
    if bot_token():
        bot_public_identity()
        username = (_BOT_PUBLIC or {}).get("username") or ""
        if username:
            db.set_config("TELEGRAM_BOT_USERNAME", username)

    if is_enabled() and bot_token() and webhook_url():
        configure_webhook()

    return get_admin_settings()


MessageHandlerFn = Callable[[str, str, str], str]


def _api_call(method: str, **fields: Any) -> dict[str, Any]:
    token = bot_token()
    if not token:
        return {"ok": False, "error": "Set TELEGRAM_BOT_TOKEN in Settings → Telegram."}
    if not is_enabled() and method != "getMe":
        return {"ok": False, "error": "Telegram is disabled in Settings → Telegram."}

    url = f"https://api.telegram.org/bot{token}/{method}"
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
        parse_mode: str | None = "HTML",
        html: bool = False,
    ) -> dict[str, Any]:
        from accounts import chat_for_send

        target, error = chat_for_send(chat_id)
        if error:
            return {"ok": False, "error": error}

        text = str(message or "")[:4000]
        mode = parse_mode
        if mode == "HTML" and not html:
            text = format_plain_to_html(text)
        elif mode == "none":
            mode = None

        result = _api_call(
            "sendMessage",
            chat_id=target,
            text=text,
            parse_mode=mode,
            disable_web_page_preview=True,
            reply_markup=json.dumps(reply_markup) if reply_markup else None,
        )
        if result.get("ok"):
            return {
                "ok": True,
                "chat_id": target,
                "message_id": ((result.get("result") or {}).get("message_id")),
            }
        return {
            "ok": False,
            "error": result.get("error") or result.get("description") or str(result),
            "chat_id": target,
        }

    def send_html(
        self,
        html_body: str,
        chat_id: str | None = None,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.send(
            html_body,
            chat_id=chat_id,
            reply_markup=reply_markup,
            parse_mode="HTML",
            html=True,
        )

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
            mine = (
                [item for item in self._inbox if item.get("user_id") == uid] if uid else []
            )
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
            "enabled": is_enabled(),
            "token_configured": bool(bot_token()),
            "mode": "webhook",
            "webhook_configured": bool(webhook_url()),
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


def webhook_secret_ok(header_value: str | None) -> bool:
    secret = webhook_secret()
    if not secret:
        return True
    got = header_value or ""
    if len(got) != len(secret):
        return False
    return hmac.compare_digest(secret, got)


def configure_webhook() -> dict[str, Any]:
    url = webhook_url()
    if not bot_token():
        logger.warning("TELEGRAM_BOT_TOKEN is not set; webhook was not registered.")
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN is not set"}
    if not is_enabled():
        logger.info("Telegram disabled; skipping webhook registration.")
        return {"ok": False, "error": "Telegram is disabled"}
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
        logger.info("Telegram webhook registered → %s", url)
    else:
        logger.error(
            "Telegram setWebhook failed: %s",
            result.get("error") or result.get("description"),
        )
    return result


def send_to_linked_phone(phone: str, html_body: str) -> dict[str, Any]:
    """Send HTML to a user that has both phone and chat_id linked."""
    from accounts import find_by_phone, normalize_phone

    try:
        digits = normalize_phone(phone)
    except ValueError:
        return {"ok": False, "error": "Invalid phone number"}

    user = find_by_phone(digits)
    if not user or not user.get("chat_id") or not user.get("phone"):
        return {
            "ok": False,
            "error": "No user found with both that phone and a linked Telegram chat_id",
        }
    sent = bot_handler.send_html(html_body, chat_id=str(user["chat_id"]))
    return {
        **sent,
        "user_id": str(user.get("user_id") or ""),
        "chat_id": str(user.get("chat_id") or ""),
        "phone": digits,
    }


def dispatch_update(
    update: dict[str, Any],
    on_text: MessageHandlerFn,
    on_user: Callable[[str], None] | None = None,
) -> None:
    """Handle one Telegram update. Replies in the chat that sent it."""
    if not is_enabled():
        return

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
                bot_handler.send_html(
                    format_html_card(
                        badge="Knics",
                        title="Share your own number",
                        lines=["Use the button to share <b>your</b> Telegram phone."],
                    ),
                    chat_id=chat_id,
                    reply_markup=SHARE_KEYBOARD,
                )
                return
            try:
                link_phone(user_id, str(contact.get("phone_number") or ""))
            except (ValueError, LookupError) as exc:
                bot_handler.send_html(
                    format_html_card(
                        badge="Knics",
                        title="Could not save phone",
                        lines=[escape_html(str(exc))],
                    ),
                    chat_id=chat_id,
                )
                return
            print("[telegram] contact phone linked", flush=True)
            bot_handler.send_html(
                format_html_card(
                    badge="Knics",
                    title="Phone number saved",
                    lines=["Sign in on the website with Google to open your console."],
                ),
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
                bot_handler.send_html(
                    format_html_card(
                        badge="Knics",
                        title="Could not save phone",
                        lines=[escape_html(str(exc))],
                    ),
                    chat_id=chat_id,
                )
                return
            print(f"[telegram] phone linked digits={len(typed_phone)}", flush=True)
            bot_handler.send_html(
                format_html_card(
                    badge="Knics",
                    title="Phone number saved",
                    lines=["Sign in on the website with Google to open your console."],
                ),
                chat_id=chat_id,
                reply_markup=REMOVE_KEYBOARD,
            )
            return
        command = text.split()[0].split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            bot_handler.send_html(
                format_html_card(
                    badge="Knics",
                    title="Ready when you are",
                    lines=[
                        "Sign in with Google on the website to open the console.",
                        "Or type here anytime to talk to your agent.",
                    ],
                    footer="Share your phone with the button if you want login codes.",
                ),
                chat_id=chat_id,
                reply_markup=SHARE_KEYBOARD,
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
        body = (reply or "(no reply)").strip()
        bot_handler.send(body, chat_id=chat_id)
    finally:
        reset_reply_chat(reply_token)
        reset_user(user_token)
