"""Active Telegram user for the current thread or request."""

from __future__ import annotations

from contextvars import ContextVar, Token

_current: ContextVar[str | None] = ContextVar("knics_user_id", default=None)
_reply_chat: ContextVar[str | None] = ContextVar("knics_reply_chat", default=None)


def current_user_id() -> str | None:
    return _current.get()


def current_reply_chat() -> str | None:
    return _reply_chat.get()


def use_reply_chat(chat_id: str | None) -> Token:
    return _reply_chat.set(chat_id or None)


def reset_reply_chat(token: Token) -> None:
    _reply_chat.reset(token)


def use_user(user_id: str | None) -> Token:
    return _current.set(user_id or None)


def reset_user(token: Token) -> None:
    _current.reset(token)
