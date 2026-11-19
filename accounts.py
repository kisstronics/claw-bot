"""Per-user profiles, Google Sign-In, Telegram channel links, email, and schedules."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from user_scope import current_user_id

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("KNICS_DATA_DIR", ROOT / "data"))
USERS_DIR = DATA_ROOT / "users"
INDEX_PATH = DATA_ROOT / "users.json"
SESSIONS_PATH = DATA_ROOT / "sessions.json"
LEGACY_MEMORY = ROOT / "memory"
LEGACY_SCHEDULES = ROOT / "config" / "schedules.json"

_LOCK = threading.Lock()
_OTP: dict[str, dict[str, Any]] = {}
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSIONS_LOADED = False

# Telegram numeric ids, or Google ids as g_<sub>
_USER_ID_RE = re.compile(r"^(?:[0-9]{1,20}|g_[a-zA-Z0-9_-]{1,60})$")
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


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) < 8 or len(digits) > 15:
        raise ValueError("Enter the phone number with country code.")
    return digits


def phone_from_text(text: str) -> str:
    """Return digits when the whole message is a phone number, ignoring spaces and punctuation."""
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
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", sub):
        digest = hashlib.sha256(sub.encode("utf-8")).hexdigest()[:40]
        return f"g_{digest}"
    return f"g_{sub}"


def user_dir(user_id: str) -> Path:
    return USERS_DIR / _safe_user_id(user_id)


def memory_dir(user_id: str) -> Path:
    path = user_dir(user_id) / "memory"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _blank_index() -> dict[str, Any]:
    return {"users": {}, "phones": {}, "emails": {}}


def _load_index() -> dict[str, Any]:
    if not INDEX_PATH.exists():
        return _blank_index()
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _blank_index()
    if not isinstance(data, dict):
        return _blank_index()
    data.setdefault("users", {})
    data.setdefault("phones", {})
    data.setdefault("emails", {})
    return data


def _save_index(data: dict[str, Any]) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _load_sessions() -> None:
    global _SESSIONS_LOADED
    if _SESSIONS_LOADED:
        return
    _SESSIONS_LOADED = True
    if not SESSIONS_PATH.exists():
        return
    try:
        data = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    now = time.time()
    for token, rec in (data.get("sessions") or {}).items():
        if isinstance(rec, dict) and float(rec.get("exp") or 0) > now:
            _SESSIONS[str(token)] = rec


def _save_sessions() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    now = time.time()
    kept = {
        token: rec
        for token, rec in _SESSIONS.items()
        if float(rec.get("exp") or 0) > now
    }
    _SESSIONS.clear()
    _SESSIONS.update(kept)
    SESSIONS_PATH.write_text(
        json.dumps({"sessions": kept}, indent=2) + "\n",
        encoding="utf-8",
    )


def list_user_ids() -> list[str]:
    with _LOCK:
        return list(_load_index().get("users") or {})


def get_user(user_id: str) -> dict[str, Any] | None:
    try:
        user_id = _safe_user_id(user_id)
    except ValueError:
        return None
    with _LOCK:
        user = (_load_index().get("users") or {}).get(user_id)
        return dict(user) if isinstance(user, dict) else None


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    phone = str(user.get("phone") or "")
    masked = phone
    if len(phone) > 4:
        masked = "•" * (len(phone) - 4) + phone[-4:]
    return {
        "user_id": user.get("user_id"),
        "name": user.get("name") or "",
        "username": user.get("username") or "",
        "email": user.get("email") or "",
        "picture": user.get("picture") or "",
        "phone": masked,
        "phone_linked": bool(phone),
        "auth_provider": user.get("auth_provider") or (
            "google" if str(user.get("user_id") or "").startswith("g_") else "telegram"
        ),
    }


def _should_import_legacy(user_id: str, chat_id: str) -> bool:
    """Move the old single-user files once, only to the account named in the env."""
    if (DATA_ROOT / ".legacy_imported").exists():
        return False
    owner = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return bool(owner) and owner in {str(user_id), str(chat_id)}


def _copy_legacy(user_id: str) -> None:
    dest = memory_dir(user_id)
    if LEGACY_MEMORY.exists():
        for name in (
            "MEMORY.md",
            "reminders.json",
            "reminder_sent.json",
            "news_sent.json",
            "journal.jsonl",
        ):
            src = LEGACY_MEMORY / name
            target = dest / name
            if src.is_file() and not target.exists():
                shutil.copy2(src, target)
        notes = LEGACY_MEMORY / "notes"
        if notes.is_dir():
            dest_notes = dest / "notes"
            dest_notes.mkdir(parents=True, exist_ok=True)
            for path in notes.glob("*.md"):
                target = dest_notes / path.name
                if not target.exists():
                    shutil.copy2(path, target)
    schedules = user_dir(user_id) / "schedules.json"
    if LEGACY_SCHEDULES.is_file() and not schedules.exists():
        user_dir(user_id).mkdir(parents=True, exist_ok=True)
        shutil.copy2(LEGACY_SCHEDULES, schedules)
    (DATA_ROOT / ".legacy_imported").write_text(user_id, encoding="utf-8")


def _provision(user_id: str, *, import_legacy: bool) -> None:
    user_dir(user_id).mkdir(parents=True, exist_ok=True)
    memory_dir(user_id)
    if import_legacy and not (DATA_ROOT / ".legacy_imported").exists():
        _copy_legacy(user_id)
    path = user_dir(user_id) / "schedules.json"
    if not path.exists():
        save_user_schedules(user_id, [dict(job) for job in _DEFAULT_JOBS])


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
    created = False
    with _LOCK:
        data = _load_index()
        users: dict[str, Any] = data["users"]
        current = users.get(user_id)
        if not isinstance(current, dict):
            created = True
            import_legacy = _should_import_legacy(user_id, chat_id)
            current = {
                "user_id": user_id,
                "chat_id": chat_id,
                "phone": "",
                "email": "",
                "picture": "",
                "name": name or "",
                "username": username or "",
                "auth_provider": "telegram",
                "provider": "",
                "model_id": "",
            }
        else:
            import_legacy = False
            if private or not current.get("chat_id"):
                current["chat_id"] = chat_id
            if name:
                current["name"] = name
            if username:
                current["username"] = username
            current.setdefault("auth_provider", "telegram")
        users[user_id] = current
        _save_index(data)
        profile = dict(current)
    if created:
        _provision(user_id, import_legacy=import_legacy)
    return profile


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
    created = False
    with _LOCK:
        data = _load_index()
        users: dict[str, Any] = data["users"]
        emails: dict[str, str] = data["emails"]
        current = users.get(user_id)
        if not isinstance(current, dict):
            created = True
            current = {
                "user_id": user_id,
                "chat_id": "",
                "phone": "",
                "email": email,
                "picture": picture,
                "name": name,
                "username": email.split("@", 1)[0] if email else "",
                "auth_provider": "google",
                "google_sub": str(sub),
                "provider": "",
                "model_id": "",
            }
        else:
            current["auth_provider"] = "google"
            current["google_sub"] = str(sub)
            if email:
                previous = str(current.get("email") or "").strip().lower()
                if previous and previous != email:
                    emails.pop(previous, None)
                current["email"] = email
            if name:
                current["name"] = name
            if picture:
                current["picture"] = picture
            if email and not current.get("username"):
                current["username"] = email.split("@", 1)[0]
        if email:
            owner = emails.get(email)
            if owner and owner != user_id:
                raise ValueError("That Google email is already linked to another account.")
            emails[email] = user_id
        users[user_id] = current
        _save_index(data)
        profile = dict(current)
    if created:
        _provision(user_id, import_legacy=False)
    return profile


def link_phone(user_id: str, phone: str) -> dict[str, Any]:
    user_id = _safe_user_id(user_id)
    phone = normalize_phone(phone)
    with _LOCK:
        data = _load_index()
        users: dict[str, Any] = data["users"]
        phones: dict[str, str] = data["phones"]
        owner = phones.get(phone)
        if owner and owner != user_id:
            raise ValueError("That phone number is already linked to another account.")
        user = users.get(user_id)
        if not isinstance(user, dict):
            raise LookupError("Telegram user is not registered yet.")
        previous = str(user.get("phone") or "")
        if previous and previous != phone:
            phones.pop(previous, None)
        user["phone"] = phone
        phones[phone] = user_id
        _save_index(data)
        return dict(user)


def _same_phone(left: str, right: str) -> bool:
    if left == right:
        return True
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    if len(short) < 8 or len(long) - len(short) > 3:
        return False
    return long.endswith(short)


def find_by_phone(phone: str) -> dict[str, Any] | None:
    phone = normalize_phone(phone)
    with _LOCK:
        data = _load_index()
        phones: dict[str, str] = data.get("phones") or {}
        user_id = phones.get(phone)
        if not user_id:
            matches = [uid for stored, uid in phones.items() if _same_phone(str(stored), phone)]
            user_id = matches[0] if len(set(matches)) == 1 else None
        if not user_id:
            return None
        user = (data.get("users") or {}).get(user_id)
        return dict(user) if isinstance(user, dict) else None


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
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def login_with_google(credential: str) -> dict[str, Any]:
    """Verify a Google Identity Services ID token and open a session."""
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
    except Exception as exc:  # noqa: BLE001 — surface as auth failure
        raise PermissionError("Google login could not be verified.") from exc

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
    """Accept the Login Widget payload after checking Telegram's signature."""
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
    with _LOCK:
        _load_sessions()
        _SESSIONS[token] = {"user_id": user_id, "exp": time.time() + _SESSION_TTL}
        _save_sessions()
    return token


def drop_session(token: str) -> None:
    with _LOCK:
        _load_sessions()
        _SESSIONS.pop(token or "", None)
        _save_sessions()


def user_from_authorization(header: str | None) -> dict[str, Any] | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    with _LOCK:
        _load_sessions()
        rec = _SESSIONS.get(token.strip())
        if not rec or float(rec.get("exp") or 0) < time.time():
            _SESSIONS.pop(token.strip(), None)
            return None
        user_id = str(rec.get("user_id") or "")
    return get_user(user_id)


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
    with _LOCK:
        data = _load_index()
        user = (data.get("users") or {}).get(user_id)
        if not isinstance(user, dict):
            raise LookupError("Unknown user.")
        user["provider"] = provider
        user["model_id"] = model_id
        _save_index(data)


def _email_path(user_id: str) -> Path:
    return user_dir(user_id) / "email.json"


def _read_email(user_id: str) -> dict[str, Any]:
    path = _email_path(user_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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
    stored = {
        "email": email,
        "app_password": password,
        "imap_server": imap_server,
        "smtp_server": smtp_server,
        "smtp_port": smtp_port,
        "news_to": news_to,
    }
    path = _email_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
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
    """Return the chat a send is allowed to use, or an error message."""
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
    with _LOCK:
        users = _load_index().get("users") or {}
    for user in users.values():
        if isinstance(user, dict) and str(user.get("chat_id") or "") == requested:
            return requested, None
    return "", "Unknown Telegram chat."


def load_user_schedules(user_id: str) -> list[dict[str, Any]]:
    path = user_dir(user_id) / "schedules.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    jobs = data.get("jobs", data if isinstance(data, list) else [])
    return [job for job in jobs if isinstance(job, dict)]


def save_user_schedules(user_id: str, jobs: list[dict[str, Any]]) -> None:
    path = user_dir(user_id) / "schedules.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": jobs}, indent=2) + "\n", encoding="utf-8")
