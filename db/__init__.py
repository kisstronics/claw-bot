"""PostgreSQL access for claw-bot. All tables live in the `claw` schema."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SCHEMA = "claw"
SYSTEM_USER_ID = "_system"

# Seeded into claw.app_config from process env (DATABASE_URL excluded).
CONFIG_KEYS = (
    "GOOGLE_CLIENT_ID",
    "MODEL_PROVIDER",
    "MODEL_ID",
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL_ID",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_SITE_URL",
    "OPENROUTER_APP_NAME",
    "GROQ_API_KEY",
    "GROQ_MODEL_ID",
    "BEDROCK_MODEL_ID",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "TELEGRAM_ENABLED",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_URL",
    "TELEGRAM_WEBHOOK_SECRET",
    "TELEGRAM_BOT_USERNAME",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_SSL_VERIFY",
    "TELEGRAM_CA_BUNDLE",
    "GMAIL_EMAIL",
    "GMAIL_APP_PASSWORD",
    "GMAIL_IMAP_SERVER",
    "GMAIL_SMTP_SERVER",
    "GMAIL_SMTP_PORT",
    "NEWS_EMAIL_TO",
    "MAX_QUESTIONS_PER_DAY",
    "MAX_INPUT_TOKENS",
)

# Admin UI editable keys (order preserved).
ADMIN_CONFIG_KEYS = CONFIG_KEYS

SECRET_CONFIG_KEYS = {
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "GMAIL_APP_PASSWORD",
}

# Managed on the Settings → Telegram tab (hidden from generic App config form).
TELEGRAM_SETTINGS_KEYS = {
    "TELEGRAM_ENABLED",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_URL",
    "TELEGRAM_WEBHOOK_SECRET",
    "TELEGRAM_BOT_USERNAME",
}

DEFAULT_CONFIG = {
    "MAX_QUESTIONS_PER_DAY": "10",
    "MAX_INPUT_TOKENS": "2000",
}

_pool = None

SCHEMA_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

CREATE TABLE IF NOT EXISTS {SCHEMA}.app_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.users (
    user_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    picture TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    auth_provider TEXT NOT NULL DEFAULT '',
    google_sub TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',
    provider TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.daily_usage (
    user_id TEXT NOT NULL REFERENCES {SCHEMA}.users(user_id) ON DELETE CASCADE,
    day DATE NOT NULL,
    questions INT NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

CREATE UNIQUE INDEX IF NOT EXISTS users_phone_uidx
    ON {SCHEMA}.users (phone) WHERE phone <> '';
CREATE UNIQUE INDEX IF NOT EXISTS users_email_uidx
    ON {SCHEMA}.users (email) WHERE email <> '';

CREATE TABLE IF NOT EXISTS {SCHEMA}.sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES {SCHEMA}.users(user_id) ON DELETE CASCADE,
    exp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.email_settings (
    user_id TEXT PRIMARY KEY REFERENCES {SCHEMA}.users(user_id) ON DELETE CASCADE,
    email TEXT NOT NULL DEFAULT '',
    app_password TEXT NOT NULL DEFAULT '',
    imap_server TEXT NOT NULL DEFAULT 'imap.gmail.com',
    smtp_server TEXT NOT NULL DEFAULT 'smtp.gmail.com',
    smtp_port INT NOT NULL DEFAULT 587,
    news_to TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.schedules (
    user_id TEXT NOT NULL REFERENCES {SCHEMA}.users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    every_seconds INT NOT NULL DEFAULT 3600,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    handler TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    initial_delay_seconds INT NOT NULL DEFAULT 5,
    PRIMARY KEY (user_id, name)
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.memory_core (
    user_id TEXT PRIMARY KEY REFERENCES {SCHEMA}.users(user_id) ON DELETE CASCADE,
    content TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.memory_notes (
    user_id TEXT NOT NULL REFERENCES {SCHEMA}.users(user_id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.journal (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES {SCHEMA}.users(user_id) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    event TEXT NOT NULL,
    meta JSONB NOT NULL DEFAULT '{{}}'::jsonb
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.reminders (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES {SCHEMA}.users(user_id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    when_at TEXT NOT NULL,
    recurring TEXT
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.reminder_sent (
    user_id TEXT NOT NULL REFERENCES {SCHEMA}.users(user_id) ON DELETE CASCADE,
    event_key TEXT NOT NULL,
    sent_on DATE NOT NULL,
    PRIMARY KEY (user_id, event_key)
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.news_sent (
    user_id TEXT NOT NULL REFERENCES {SCHEMA}.users(user_id) ON DELETE CASCADE,
    story_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, story_id)
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.token_usage (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    model TEXT NOT NULL,
    input_tokens INT NOT NULL DEFAULT 0,
    output_tokens INT NOT NULL DEFAULT 0,
    total_tokens INT NOT NULL DEFAULT 0,
    tools TEXT NOT NULL DEFAULT '-',
    rounds INT NOT NULL DEFAULT 0,
    user_id TEXT,
    duration_sec DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.global_settings (
    id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    provider TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT ''
);

INSERT INTO {SCHEMA}.global_settings (id, provider, model_id)
VALUES (1, '', '')
ON CONFLICT (id) DO NOTHING;

INSERT INTO {SCHEMA}.users (user_id, auth_provider, name)
VALUES ('{SYSTEM_USER_ID}', 'system', 'System')
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO {SCHEMA}.memory_core (user_id, content)
VALUES (
    '{SYSTEM_USER_ID}',
    '# Agent memory\n\nDurable facts and preferences go here.\n'
)
ON CONFLICT (user_id) DO NOTHING;
"""


def _normalize_database_url(url: str) -> str:
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if "sslmode" not in query:
        query["sslmode"] = ["require"]
    return urlunparse(parsed._replace(query=urlencode({k: v[0] for k, v in query.items()})))


def database_url() -> str:
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        raise RuntimeError("DATABASE_URL is not set.")
    return _normalize_database_url(raw)


def _get_pool():
    global _pool
    if _pool is not None:
        return _pool
    from psycopg_pool import ConnectionPool

    _pool = ConnectionPool(
        conninfo=database_url(),
        min_size=1,
        max_size=8,
        kwargs={"options": f"-c search_path={SCHEMA},public"},
        open=True,
    )
    return _pool


@contextmanager
def connect() -> Iterator[Any]:
    pool = _get_pool()
    with pool.connection() as conn:
        yield conn


def execute(sql: str, params: tuple | dict | None = None) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


def fetch_one(sql: str, params: tuple | dict | None = None) -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            cols = [desc.name for desc in cur.description]
            return dict(zip(cols, row))


def fetch_all(sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            cols = [desc.name for desc in cur.description]
            return [dict(zip(cols, row)) for row in rows]


MIGRATIONS = [
    f"ALTER TABLE {SCHEMA}.users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user'",
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA}.daily_usage (
        user_id TEXT NOT NULL REFERENCES {SCHEMA}.users(user_id) ON DELETE CASCADE,
        day DATE NOT NULL,
        questions INT NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day)
    )
    """,
]


def init_db() -> None:
    """Create the claw schema/tables and sync env configuration into app_config."""
    statements = [part.strip() for part in SCHEMA_SQL.split(";") if part.strip()]
    with connect() as conn:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
            for statement in MIGRATIONS:
                cur.execute(statement)
        conn.commit()
    sync_env_config()
    seed_default_config()
    apply_config_to_environ()
    logger.info("Postgres ready (schema=%s)", SCHEMA)


def sync_env_config() -> None:
    """Seed known environment values into claw.app_config without overwriting admin edits."""
    rows = []
    for key in CONFIG_KEYS:
        value = os.environ.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if value == "":
            continue
        rows.append((key, value))
    if not rows:
        return
    with connect() as conn:
        with conn.cursor() as cur:
            for key, value in rows:
                cur.execute(
                    f"""
                    INSERT INTO {SCHEMA}.app_config (key, value, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (key) DO NOTHING
                    """,
                    (key, value),
                )
        conn.commit()


def seed_default_config() -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            for key, value in DEFAULT_CONFIG.items():
                cur.execute(
                    f"""
                    INSERT INTO {SCHEMA}.app_config (key, value, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (key) DO NOTHING
                    """,
                    (key, value),
                )
        conn.commit()


def apply_config_to_environ() -> None:
    """Apply claw.app_config into process env (DB is source of truth)."""
    rows = fetch_all(f"SELECT key, value FROM {SCHEMA}.app_config")
    for row in rows:
        key = str(row["key"])
        value = str(row["value"] or "")
        if key:
            os.environ[key] = value


def get_config(key: str, default: str = "") -> str:
    row = fetch_one(
        f"SELECT value FROM {SCHEMA}.app_config WHERE key = %s",
        (key,),
    )
    if row is not None:
        return str(row.get("value") or default)
    env_val = os.environ.get(key, "").strip()
    if env_val:
        return env_val
    return str(DEFAULT_CONFIG.get(key, default))


def get_config_int(key: str, default: int) -> int:
    raw = get_config(key, str(default)).strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def set_config(key: str, value: str) -> None:
    value = str(value or "")
    execute(
        f"""
        INSERT INTO {SCHEMA}.app_config (key, value, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE
        SET value = EXCLUDED.value, updated_at = now()
        """,
        (key, value),
    )
    os.environ[key] = value


def list_app_config(*, mask_secrets: bool = True) -> list[dict[str, str]]:
    rows = {
        str(row["key"]): str(row.get("value") or "")
        for row in fetch_all(f"SELECT key, value FROM {SCHEMA}.app_config")
    }
    items: list[dict[str, str]] = []
    for key in ADMIN_CONFIG_KEYS:
        if key in TELEGRAM_SETTINGS_KEYS:
            continue
        value = rows.get(key, DEFAULT_CONFIG.get(key, os.environ.get(key, "")))
        secret = key in SECRET_CONFIG_KEYS
        items.append(
            {
                "key": key,
                "value": "" if secret and mask_secrets and value else value,
                "secret": "1" if secret else "0",
                "set": "1" if bool(value) else "0",
            }
        )
    return items


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
