"""Per-user profiles, Google Sign-In, Telegram links, email, and schedules (Postgres)."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any

from user_scope import current_user_id

import db

_LOCK = threading.Lock()
_OTP: dict[str, dict[str, Any]] = {}

_USER_ID_RE = re.compile(r"^(?:[0-9]{1,20}|g_[a-zA-Z0-9_-]{1,200}|_system)$")
_OTP_TTL = 300
_OTP_ATTEMPTS = 5
_OTP_COOLDOWN = 30
_SESSION_TTL = 14 * 24 * 3600
_TELEGRAM_AUTH_MAX_AGE = 24 * 3600
_TELEGRAM_AUTH_FIELDS = (
    "auth_date",
    "first_name",
    "id",
    "last_name",
    "photo_url",
    "username",
)

_DEFAULT_JOBS = [
    {
        "name": "reminders",
        "enabled": True,
        "every_seconds": 3600,
        "handler": "reminders",
        "initial_delay_seconds": 45,
    },
    {
        "name": "news_digest",
        "enabled": False,
        "every_seconds": 3600,
        "handler": "news_digest",
        "initial_delay_seconds": 30,
    },
]

_USER_COLS = (
    "user_id",
    "chat_id",
    "phone",
    "email",
    "picture",
    "name",
    "username",
    "auth_provider",
    "google_sub",
    "provider",
    "model_id",
)


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) < 8 or len(digits) > 15:
        raise ValueError("Enter the phone number with country code.")
    return digits


def phone_from_text(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped or re.search(r"[A-Za-z]", stripped):
        return ""
    try:
        return normalize_phone(stripped)
    except ValueError:
        return ""


def _safe_user_id(user_id: str) -> str:
    user_id = str(user_id or "").strip()
    if not _USER_ID_RE.match(user_id):
        raise ValueError("Invalid user id.")
    return user_id


def _google_user_id(sub: str) -> str:
    sub = str(sub or "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,200}", sub):
        digest = hashlib.sha256(sub.encode("utf-8")).hexdigest()[:40]
        return f"g_{digest}"
    return f"g_{sub}"


def memory_dir(user_id: str) -> str:
    """Compatibility shim; memory now lives in Postgres."""
    return f"db://claw/memory/{_safe_user_id(user_id)}"


def list_user_ids() -> list[str]:
    rows = db.fetch_all(
        "SELECT user_id FROM users WHERE user_id <> %s ORDER BY user_id",
        (db.SYSTEM_USER_ID,),
    )
    return [str(row["user_id"]) for row in rows]


def get_user(user_id: str) -> dict[str, Any] | None:
    try:
        user_id = _safe_user_id(user_id)
    except ValueError:
        return None
    row = db.fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
    return dict(row) if row else None


def normalize_role(role: str | None) -> str:
    role = str(role or "user").strip().lower()
    return role if role in {"user", "admin"} else "user"


def is_admin(user: dict[str, Any] | None) -> bool:
    return normalize_role((user or {}).get("role")) == "admin"


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    phone = str(user.get("phone") or "")
    masked = phone
    if len(phone) > 4:
        masked = "•" * (len(phone) - 4) + phone[-4:]
    role = normalize_role(user.get("role"))
    return {
        "user_id": user.get("user_id"),
        "name": user.get("name") or "",
        "username": user.get("username") or "",
        "email": user.get("email") or "",
        "picture": user.get("picture") or "",
        "phone": masked,
        "phone_linked": bool(phone),
        "role": role,
        "is_admin": role == "admin",
        "auth_provider": user.get("auth_provider")
        or ("google" if str(user.get("user_id") or "").startswith("g_") else "telegram"),
    }


def _provision(user_id: str) -> None:
    existing = load_user_schedules(user_id)
    if not existing:
        save_user_schedules(user_id, [dict(job) for job in _DEFAULT_JOBS])
    db.execute(
        """
        INSERT INTO memory_core (user_id, content)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id, "# Agent memory\n\nDurable facts and preferences go here.\n"),
    )


def ensure_user(
    user_id: str,
    chat_id: str,
    *,
    name: str = "",
    username: str = "",
    private: bool = True,
) -> dict[str, Any]:
    user_id = _safe_user_id(user_id)
    chat_id = str(chat_id or "").strip()
    current = get_user(user_id)
    created = current is None
    if created:
        db.execute(
            """
            INSERT INTO users (
                user_id, chat_id, phone, email, picture, name, username,
                auth_provider, google_sub, role, provider, model_id
            ) VALUES (%s, %s, '', '', '', %s, %s, 'telegram', '', 'user', '', '')
            """,
            (user_id, chat_id, name or "", username or ""),
        )
        _provision(user_id)
        return get_user(user_id) or {}

    updates: list[str] = []
    params: list[Any] = []
    if private or not current.get("chat_id"):
        updates.append("chat_id = %s")
        params.append(chat_id)
    if name:
        updates.append("name = %s")
        params.append(name)
    if username:
        updates.append("username = %s")
        params.append(username)
    if updates:
        updates.append("updated_at = now()")
        params.append(user_id)
        db.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE user_id = %s",
            tuple(params),
        )
    return get_user(user_id) or current


def ensure_google_user(
    *,
    sub: str,
    email: str = "",
    name: str = "",
    picture: str = "",
) -> dict[str, Any]:
    user_id = _google_user_id(sub)
    email = str(email or "").strip().lower()
    name = str(name or "").strip()
    picture = str(picture or "").strip()
    current = get_user(user_id)
    created = current is None

    if email:
        owner = db.fetch_one(
            "SELECT user_id FROM users WHERE email = %s AND user_id <> %s",
            (email, user_id),
        )
        if owner:
            raise ValueError("That Google email is already linked to another account.")

    if created:
        db.execute(
            """
            INSERT INTO users (
                user_id, chat_id, phone, email, picture, name, username,
                auth_provider, google_sub, role, provider, model_id
            ) VALUES (%s, '', '', %s, %s, %s, %s, 'google', %s, 'user', '', '')
            """,
            (
                user_id,
                email,
                picture,
                name,
                email.split("@", 1)[0] if email else "",
                str(sub),
            ),
        )
        _provision(user_id)
    else:
        db.execute(
            """
            UPDATE users SET
                auth_provider = 'google',
                google_sub = %s,
                email = CASE WHEN %s <> '' THEN %s ELSE email END,
                picture = CASE WHEN %s <> '' THEN %s ELSE picture END,
                name = CASE WHEN %s <> '' THEN %s ELSE name END,
                username = CASE
                    WHEN username = '' AND %s <> '' THEN split_part(%s, '@', 1)
                    ELSE username
                END,
                updated_at = now()
            WHERE user_id = %s
            """,
            (
                str(sub),
                email,
                email,
                picture,
                picture,
                name,
                name,
                email,
                email,
                user_id,
            ),
        )
    return get_user(user_id) or {}


def link_phone(user_id: str, phone: str) -> dict[str, Any]:
    user_id = _safe_user_id(user_id)
    phone = normalize_phone(phone)
    owner = db.fetch_one(
        "SELECT user_id FROM users WHERE phone = %s AND user_id <> %s",
        (phone, user_id),
    )
    if owner:
        raise ValueError("That phone number is already linked to another account.")
    user = get_user(user_id)
    if not user:
        raise LookupError("Telegram user is not registered yet.")
    db.execute(
        "UPDATE users SET phone = %s, updated_at = now() WHERE user_id = %s",
        (phone, user_id),
    )
    return get_user(user_id) or user


def _same_phone(left: str, right: str) -> bool:
    if left == right:
        return True
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    if len(short) < 8 or len(long) - len(short) > 3:
        return False
    return long.endswith(short)


def find_by_phone(phone: str) -> dict[str, Any] | None:
    phone = normalize_phone(phone)
    row = db.fetch_one("SELECT * FROM users WHERE phone = %s", (phone,))
    if row:
        return dict(row)
    for user in db.fetch_all("SELECT * FROM users WHERE phone <> ''"):
        if _same_phone(str(user.get("phone") or ""), phone):
            return dict(user)
    return None


def request_otp(phone: str) -> None:
    phone = normalize_phone(phone)
    user = find_by_phone(phone)
    if not user or not user.get("chat_id"):
        raise LookupError(
            "This number is not linked yet. Open the bot, send /start, and share your phone number."
        )
    now = time.time()
    with _LOCK:
        previous = _OTP.get(phone)
        if previous and now - float(previous.get("sent_at") or 0) < _OTP_COOLDOWN:
            raise RuntimeError("Wait a moment before requesting another code.")
        code = f"{secrets.randbelow(1_000_000):06d}"
        _OTP[phone] = {
            "code": code,
            "exp": now + _OTP_TTL,
            "attempts": 0,
            "sent_at": now,
        }
    from channels.telegram import bot_handler

    result = bot_handler.send(
        f"Your Knics login code is {code}. It expires in 5 minutes.",
        chat_id=str(user["chat_id"]),
    )
    if not result.get("ok"):
        with _LOCK:
            _OTP.pop(phone, None)
        raise RuntimeError(result.get("error") or "Could not send the code on Telegram.")


def google_client_id() -> str:
    return db.get_config("GOOGLE_CLIENT_ID")


def login_with_google(credential: str) -> dict[str, Any]:
    client_id = google_client_id()
    if not client_id:
        raise RuntimeError("Google login is not configured. Set GOOGLE_CLIENT_ID.")

    token = str(credential or "").strip()
    if not token:
        raise ValueError("Google credential is missing.")

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
    except ImportError as exc:
        raise RuntimeError(
            "Install google-auth to enable Google Sign-In (pip install google-auth)."
        ) from exc

    try:
        info = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            client_id,
            clock_skew_in_seconds=60,
        )
    except Exception as exc:  # noqa: BLE001
        raise PermissionError(
            f"Google login could not be verified ({exc.__class__.__name__}). "
            "Check GOOGLE_CLIENT_ID matches the OAuth Web client and this origin "
            "is listed under Authorized JavaScript origins."
        ) from exc

    issuer = str(info.get("iss") or "")
    if issuer not in {"accounts.google.com", "https://accounts.google.com"}:
        raise PermissionError("Google login could not be verified.")
    if info.get("email_verified") is False:
        raise PermissionError("Verify your Google email address, then try again.")

    sub = str(info.get("sub") or "").strip()
    if not sub:
        raise PermissionError("Google login could not be verified.")

    user = ensure_google_user(
        sub=sub,
        email=str(info.get("email") or ""),
        name=str(info.get("name") or ""),
        picture=str(info.get("picture") or ""),
    )
    session = create_session(str(user["user_id"]))
    return {"token": session, "user": public_user(user)}


def login_with_telegram(payload: dict[str, Any]) -> dict[str, Any]:
    from channels.telegram import TOKEN

    if not TOKEN:
        raise RuntimeError("Telegram login is not configured.")
    if not isinstance(payload, dict):
        raise ValueError("Telegram login data is missing.")

    received = str(payload.get("hash") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", received):
        raise PermissionError("Telegram login could not be verified.")

    try:
        user_id = str(int(payload.get("id")))
        auth_date = int(payload.get("auth_date"))
    except (TypeError, ValueError) as exc:
        raise PermissionError("Telegram login could not be verified.") from exc

    now = time.time()
    if auth_date > now + 30 or now - auth_date > _TELEGRAM_AUTH_MAX_AGE:
        raise PermissionError("That Telegram login expired. Try the button again.")

    lines: list[str] = []
    for key in _TELEGRAM_AUTH_FIELDS:
        if key not in payload or payload[key] is None:
            continue
        raw = payload[key]
        if key in {"id", "auth_date"}:
            try:
                value = str(int(raw))
            except (TypeError, ValueError) as exc:
                raise PermissionError("Telegram login could not be verified.") from exc
        else:
            value = str(raw)
        lines.append(f"{key}={value}")
    data_check = "\n".join(sorted(lines))
    secret = hashlib.sha256(TOKEN.encode("utf-8")).digest()
    digest = hmac.new(secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, received):
        raise PermissionError("Telegram login could not be verified.")

    _safe_user_id(user_id)
    name = " ".join(
        part
        for part in (
            str(payload.get("first_name") or "").strip(),
            str(payload.get("last_name") or "").strip(),
        )
        if part
    )
    user = ensure_user(
        user_id,
        user_id,
        name=name,
        username=str(payload.get("username") or "").strip().lstrip("@"),
        private=True,
    )
    token = create_session(user_id)
    return {"token": token, "user": public_user(user)}


def verify_otp(phone: str, code: str) -> dict[str, Any]:
    phone = normalize_phone(phone)
    got = re.sub(r"\D", "", code or "")
    with _LOCK:
        entry = _OTP.get(phone)
        if not entry or time.time() > float(entry.get("exp") or 0):
            _OTP.pop(phone, None)
            raise PermissionError("That code is expired. Request a new one.")
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        if entry["attempts"] > _OTP_ATTEMPTS:
            _OTP.pop(phone, None)
            raise PermissionError("Too many attempts. Request a new code.")
        expected = str(entry.get("code") or "")
        if len(got) != len(expected) or not hmac.compare_digest(expected, got):
            raise PermissionError("That code is not correct.")
        _OTP.pop(phone, None)
    user = find_by_phone(phone)
    if not user:
        raise LookupError("This number is not linked yet.")
    token = create_session(str(user["user_id"]))
    return {"token": token, "user": public_user(user)}


def create_session(user_id: str) -> str:
    user_id = _safe_user_id(user_id)
    token = secrets.token_urlsafe(32)
    exp = datetime.fromtimestamp(time.time() + _SESSION_TTL, tz=timezone.utc)
    db.execute(
        "INSERT INTO sessions (token, user_id, exp) VALUES (%s, %s, %s)",
        (token, user_id, exp),
    )
    return token


def drop_session(token: str) -> None:
    if not token:
        return
    db.execute("DELETE FROM sessions WHERE token = %s", (token,))


def user_from_authorization(header: str | None) -> dict[str, Any] | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    token = token.strip()
    row = db.fetch_one(
        "SELECT user_id, exp FROM sessions WHERE token = %s",
        (token,),
    )
    if not row:
        return None
    exp = row["exp"]
    if hasattr(exp, "timestamp"):
        exp_ts = exp.timestamp()
    else:
        exp_ts = float(exp)
    if exp_ts < time.time():
        db.execute("DELETE FROM sessions WHERE token = %s", (token,))
        return None
    return get_user(str(row["user_id"]))


def bearer_token(header: str | None) -> str:
    if not header:
        return ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def model_selection(user_id: str) -> dict[str, str] | None:
    user = get_user(user_id)
    if not user:
        return None
    provider = str(user.get("provider") or "").strip()
    model_id = str(user.get("model_id") or "").strip()
    if provider and model_id:
        return {"provider": provider, "model_id": model_id}
    return None


def save_model_selection(user_id: str, provider: str, model_id: str) -> None:
    user_id = _safe_user_id(user_id)
    if not get_user(user_id):
        raise LookupError("Unknown user.")
    db.execute(
        """
        UPDATE users
        SET provider = %s, model_id = %s, updated_at = now()
        WHERE user_id = %s
        """,
        (provider, model_id, user_id),
    )


def _read_email(user_id: str) -> dict[str, Any]:
    row = db.fetch_one("SELECT * FROM email_settings WHERE user_id = %s", (user_id,))
    return dict(row) if row else {}


def public_email(user_id: str) -> dict[str, Any]:
    data = _read_email(user_id)
    return {
        "email": str(data.get("email") or ""),
        "imap_server": str(data.get("imap_server") or "imap.gmail.com"),
        "smtp_server": str(data.get("smtp_server") or "smtp.gmail.com"),
        "smtp_port": int(data.get("smtp_port") or 587),
        "news_to": str(data.get("news_to") or ""),
        "password_set": bool(data.get("app_password")),
    }


def save_email(user_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    user_id = _safe_user_id(user_id)
    current = _read_email(user_id)
    email = str(fields.get("email") or "").strip()
    if "@" not in email:
        raise ValueError("A valid email address is required.")
    news_to = str(fields.get("news_to") or "").strip()
    if news_to and "@" not in news_to:
        raise ValueError("News recipient must be an email address.")
    password = str(fields.get("app_password") or "")
    if not password:
        password = str(current.get("app_password") or "")
    if not password:
        raise ValueError("An app password is required.")
    imap_server = str(fields.get("imap_server") or "imap.gmail.com").strip()
    smtp_server = str(fields.get("smtp_server") or "smtp.gmail.com").strip()
    try:
        smtp_port = int(fields.get("smtp_port") or 587)
    except (TypeError, ValueError) as exc:
        raise ValueError("SMTP port must be a number.") from exc
    if not imap_server or not smtp_server or not (1 <= smtp_port <= 65535):
        raise ValueError("IMAP server, SMTP server, and port are required.")
    db.execute(
        """
        INSERT INTO email_settings (
            user_id, email, app_password, imap_server, smtp_server, smtp_port, news_to
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            email = EXCLUDED.email,
            app_password = EXCLUDED.app_password,
            imap_server = EXCLUDED.imap_server,
            smtp_server = EXCLUDED.smtp_server,
            smtp_port = EXCLUDED.smtp_port,
            news_to = EXCLUDED.news_to
        """,
        (user_id, email, password, imap_server, smtp_server, smtp_port, news_to),
    )
    return public_email(user_id)


def email_credentials(user_id: str | None = None) -> dict[str, Any] | None:
    user_id = user_id or current_user_id()
    if not user_id:
        return None
    data = _read_email(user_id)
    if not data.get("email") or not data.get("app_password"):
        return None
    return data


def news_recipient(user_id: str | None = None) -> str:
    user_id = user_id or current_user_id()
    if not user_id:
        return ""
    data = _read_email(user_id)
    return str(data.get("news_to") or data.get("email") or "").strip()


def chat_for_send(requested: str | None) -> tuple[str, str | None]:
    from user_scope import current_reply_chat

    uid = current_user_id()
    requested = str(requested or "").strip()
    if uid:
        profile = get_user(uid) or {}
        own = str(profile.get("chat_id") or "")
        reply_chat = str(current_reply_chat() or "")
        allowed = {chat for chat in (own, reply_chat) if chat}
        if requested and requested not in allowed:
            return "", "Messages stay in your own Telegram chat."
        target = requested or reply_chat or own
        if not target:
            return "", "This account has no Telegram chat yet."
        return target, None
    if not requested:
        return "", "No Telegram chat is available for this account."
    row = db.fetch_one(
        "SELECT chat_id FROM users WHERE chat_id = %s LIMIT 1",
        (requested,),
    )
    if row:
        return requested, None
    return "", "Unknown Telegram chat."


def load_user_schedules(user_id: str) -> list[dict[str, Any]]:
    user_id = _safe_user_id(user_id)
    rows = db.fetch_all(
        """
        SELECT name, every_seconds, enabled, handler, prompt, initial_delay_seconds
        FROM schedules
        WHERE user_id = %s
        ORDER BY name
        """,
        (user_id,),
    )
    jobs: list[dict[str, Any]] = []
    for row in rows:
        jobs.append(
            {
                "name": row["name"],
                "every_seconds": int(row["every_seconds"]),
                "enabled": bool(row["enabled"]),
                "handler": row["handler"] or "",
                "prompt": row["prompt"] or "",
                "initial_delay_seconds": int(row["initial_delay_seconds"] or 5),
            }
        )
    return jobs


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for request gating."""
    text = text or ""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def questions_today(user_id: str) -> int:
    user_id = _safe_user_id(user_id)
    row = db.fetch_one(
        """
        SELECT questions FROM daily_usage
        WHERE user_id = %s AND day = CURRENT_DATE
        """,
        (user_id,),
    )
    return int((row or {}).get("questions") or 0)


def record_question(user_id: str) -> int:
    user_id = _safe_user_id(user_id)
    db.execute(
        """
        INSERT INTO daily_usage (user_id, day, questions)
        VALUES (%s, CURRENT_DATE, 1)
        ON CONFLICT (user_id, day) DO UPDATE
        SET questions = daily_usage.questions + 1
        """,
        (user_id,),
    )
    return questions_today(user_id)


def check_question_quota(user_id: str) -> dict[str, Any]:
    """Raise ValueError if the user is over the daily question limit. Admins are unlimited."""
    profile = get_user(user_id) or {}
    if is_admin(profile):
        return {
            "limited": False,
            "used": questions_today(user_id),
            "limit": None,
            "remaining": None,
        }
    limit = db.get_config_int("MAX_QUESTIONS_PER_DAY", 10)
    used = questions_today(user_id)
    if used >= limit:
        raise ValueError(
            f"Daily question limit reached ({used}/{limit}). Try again tomorrow."
        )
    return {
        "limited": True,
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
    }


def enforce_input_token_limit(message: str) -> int:
    max_tokens = db.get_config_int("MAX_INPUT_TOKENS", 2000)
    estimated = estimate_tokens(message)
    if estimated > max_tokens:
        raise ValueError(
            f"Message too long ({estimated} tokens). Max input tokens is {max_tokens}."
        )
    return estimated


def set_user_role(email_or_id: str, role: str = "admin") -> dict[str, Any]:
    role = normalize_role(role)
    key = str(email_or_id or "").strip()
    if not key:
        raise ValueError("email or user_id is required")
    row = db.fetch_one(
        "SELECT user_id FROM users WHERE email = %s OR user_id = %s",
        (key.lower(), key),
    )
    if not row:
        raise LookupError(f"No user found for {key!r}")
    db.execute(
        "UPDATE users SET role = %s, updated_at = now() WHERE user_id = %s",
        (role, row["user_id"]),
    )
    return get_user(str(row["user_id"])) or {}


def save_user_schedules(user_id: str, jobs: list[dict[str, Any]]) -> None:
    user_id = _safe_user_id(user_id)
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM schedules WHERE user_id = %s", (user_id,))
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                name = str(job.get("name") or "").strip()
                if not name:
                    continue
                cur.execute(
                    """
                    INSERT INTO schedules (
                        user_id, name, every_seconds, enabled, handler, prompt,
                        initial_delay_seconds
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        name,
                        int(job.get("every_seconds") or 3600),
                        bool(job.get("enabled", True)),
                        str(job.get("handler") or ""),
                        str(job.get("prompt") or ""),
                        int(job.get("initial_delay_seconds") or 5),
                    ),
                )
        conn.commit()
