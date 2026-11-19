"""
FastAPI console: Google Sign-In, per-user chat, schedules, email, Telegram webhook.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

if sys.version_info < (3, 12):
    raise SystemExit(
        f"Python 3.12+ is required (found {sys.version.split()[0]}). "
        "Install 3.12+ and recreate your venv."
    )

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import accounts
import db
import token_tracker
from agents.news_agent import run_news_digest
from agents.reminder import run_reminders
from agents.runtime import run_agent_turn
from channels.telegram import (
    bot_handler,
    configure_webhook,
    dispatch_update,
    webhook_secret_ok,
)
from models import MODEL_CATALOG, active_selection, apply_runtime_settings, get_provider
from scheduler import SimpleScheduler
from user_scope import current_user_id, reset_user, use_user

logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent / "web"

_sessions: dict[str, dict[str, list[dict[str, Any]]]] = {}
_telegram_sessions: dict[str, list[dict[str, Any]]] = {}
_scheduler: SimpleScheduler | None = None


def _require_user() -> dict[str, Any]:
    user_id = current_user_id()
    if not user_id:
        raise HTTPException(status_code=401, detail="Login required")
    profile = accounts.get_user(user_id)
    if not profile:
        raise HTTPException(status_code=401, detail="Login required")
    return profile


def _require_admin() -> dict[str, Any]:
    user = _require_user()
    if not accounts.is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _scheduled_invoke(prompt: str) -> str:
    return run_agent_turn(get_provider(), [], prompt)


def _telegram_on_message(user_id: str, chat_id: str, text: str) -> str:
    history = _telegram_sessions.setdefault(user_id, [])
    try:
        return run_agent_turn(get_provider(), history, text)
    except Exception:
        if history and history[-1].get("role") == "user":
            history.pop()
        raise


def _attach_user(user_id: str) -> None:
    if _scheduler is not None:
        _scheduler.load_user(user_id)


def _news_digest_handler() -> str:
    return json.dumps(run_news_digest(), ensure_ascii=False)


def _reminders_handler() -> str:
    return json.dumps(run_reminders(), ensure_ascii=False)


def _run_update(update: dict[str, Any]) -> None:
    try:
        dispatch_update(update, _telegram_on_message, on_user=_attach_user)
    except Exception:
        logger.exception("Telegram update failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    db.init_db()
    _scheduler = SimpleScheduler(
        invoke=_scheduled_invoke,
        handlers={
            "news_digest": _news_digest_handler,
            "reminders": _reminders_handler,
        },
    )
    _scheduler.start()
    configure_webhook()
    yield
    if _scheduler:
        _scheduler.stop()
    db.close_pool()


app = FastAPI(
    title="Knics Agent",
    description="Per-user agent console with Google Sign-In.",
    version="0.6.0",
    lifespan=lifespan,
)


class UserContextMiddleware:
    """Bind the bearer token to the request task. Do not use BaseHTTPMiddleware."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers") or []
        }
        profile = accounts.user_from_authorization(headers.get("authorization"))
        token = use_user(profile["user_id"] if profile else None)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_user(token)


class SecurityHeadersMiddleware:
    """Allow Google Sign-In popup to return the credential to this origin."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"cross-origin-opener-policy", b"same-origin-allow-popups"))
                headers.append((b"referrer-policy", b"strict-origin-when-cross-origin"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(UserContextMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


class InvokeRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: str | None = None
    reset: bool = False


class GoogleLoginRequest(BaseModel):
    credential: str = Field(..., min_length=1)


class SettingsUpdate(BaseModel):
    provider: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)


class AppConfigUpdate(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class EmailUpdate(BaseModel):
    email: str = ""
    app_password: str = ""
    imap_server: str = "imap.gmail.com"
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = Field(default=587, ge=1, le=65535)
    news_to: str = ""


class ScheduleWrite(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    every_seconds: int = Field(default=3600, ge=5, le=86400 * 7)
    enabled: bool = True
    prompt: str = ""
    handler: str = ""
    initial_delay_seconds: int = Field(default=5, ge=0, le=3600)


def _schedule_payload(body: ScheduleWrite, user_id: str) -> dict[str, Any]:
    name = body.name.strip()
    if not name.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Name may use letters, numbers, _ and -.")
    handler = body.handler.strip()
    if handler and handler not in {"news_digest", "reminders"}:
        raise HTTPException(status_code=400, detail="Handler must be news_digest, reminders, or empty.")
    prompt = body.prompt.strip()
    if not handler and not prompt:
        raise HTTPException(status_code=400, detail="A prompt is required when no handler is set.")
    job: dict[str, Any] = {
        "name": name,
        "user_id": user_id,
        "enabled": body.enabled,
        "every_seconds": body.every_seconds,
        "initial_delay_seconds": body.initial_delay_seconds,
    }
    if handler:
        job["handler"] = handler
    else:
        job["prompt"] = prompt
    return job


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if key != "user_id"}


@app.get("/")
def console() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, Any]:
    current = active_selection()
    return {
        "status": "ok",
        "provider": current["provider"],
        "model": current["model_id"],
        "region": current["region"],
        "telegram": "webhook",
    }


@app.get("/api/auth/google")
def google_login_config() -> dict[str, str]:
    client_id = accounts.google_client_id()
    if not client_id:
        raise HTTPException(
            status_code=503,
            detail="Google login is not configured. Set GOOGLE_CLIENT_ID.",
        )
    return {"client_id": client_id}


@app.post("/api/auth/google")
def google_login(body: GoogleLoginRequest) -> dict[str, Any]:
    try:
        result = accounts.login_with_google(body.credential)
        logger.info(
            "Google sign-in ok user_id=%s email=%s",
            (result.get("user") or {}).get("user_id"),
            (result.get("user") or {}).get("email"),
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        logger.warning("Google sign-in rejected: %s", exc)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/auth/me")
def me() -> dict[str, Any]:
    return {"user": accounts.public_user(_require_user())}


@app.post("/api/auth/logout")
def logout(request: Request) -> dict[str, str]:
    accounts.drop_session(accounts.bearer_token(request.headers.get("authorization")))
    return {"status": "logged_out"}


@app.post("/invoke")
def invoke(req: InvokeRequest) -> dict[str, Any]:
    user = _require_user()
    user_id = str(user["user_id"])
    try:
        accounts.enforce_input_token_limit(req.message)
        quota = accounts.check_question_quota(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    session_id = req.session_id or str(uuid4())
    bucket = _sessions.setdefault(user_id, {})
    if req.reset or session_id not in bucket:
        bucket[session_id] = []
    messages = bucket[session_id]
    try:
        reply = run_agent_turn(get_provider(), messages, req.message)
    except Exception as exc:  # noqa: BLE001
        if messages and messages[-1].get("role") == "user":
            messages.pop()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    used = accounts.record_question(user_id)
    current = active_selection()
    return {
        "session_id": session_id,
        "reply": reply,
        "provider": current["provider"],
        "model": current["model_id"],
        "region": current["region"],
        "quota": {
            **quota,
            "used": used,
            "remaining": None
            if quota.get("limit") is None
            else max(0, int(quota["limit"]) - used),
        },
    }


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str) -> dict[str, str]:
    user = _require_user()
    bucket = _sessions.get(str(user["user_id"])) or {}
    bucket.pop(session_id, None)
    return {"status": "deleted", "session_id": session_id}


@app.get("/api/settings")
def read_settings() -> dict[str, Any]:
    user = _require_user()
    current = active_selection()
    catalog = {
        provider: list(dict.fromkeys(models))
        for provider, models in MODEL_CATALOG.items()
    }
    return {
        **current,
        "catalog": catalog,
        "is_admin": accounts.is_admin(user),
        "quota": {
            "used": accounts.questions_today(str(user["user_id"])),
            "limit": None
            if accounts.is_admin(user)
            else db.get_config_int("MAX_QUESTIONS_PER_DAY", 10),
            "max_input_tokens": db.get_config_int("MAX_INPUT_TOKENS", 2000),
        },
    }


@app.put("/api/settings")
def write_settings(body: SettingsUpdate) -> dict[str, str]:
    _require_admin()
    try:
        return apply_runtime_settings(body.provider, body.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/admin/config")
def read_app_config() -> dict[str, Any]:
    _require_admin()
    return {"items": db.list_app_config(mask_secrets=True)}


@app.put("/api/admin/config")
def write_app_config(body: AppConfigUpdate) -> dict[str, Any]:
    _require_admin()
    updated: list[str] = []
    for key, value in (body.values or {}).items():
        key = str(key or "").strip()
        if key not in db.ADMIN_CONFIG_KEYS:
            raise HTTPException(status_code=400, detail=f"Unknown config key: {key}")
        # Skip blank secret fields so masked placeholders do not wipe keys.
        if key in db.SECRET_CONFIG_KEYS and str(value or "").strip() == "":
            continue
        db.set_config(key, str(value if value is not None else ""))
        updated.append(key)
    return {"ok": True, "updated": updated, "items": db.list_app_config(mask_secrets=True)}


@app.get("/api/usage")
def usage(limit: int = 40) -> dict[str, Any]:
    user = _require_user()
    return token_tracker.read_usage(limit, user_id=str(user["user_id"]))


@app.get("/api/email")
def read_email() -> dict[str, Any]:
    user = _require_user()
    return accounts.public_email(str(user["user_id"]))


@app.put("/api/email")
def write_email(body: EmailUpdate) -> dict[str, Any]:
    user = _require_user()
    try:
        return accounts.save_email(str(user["user_id"]), body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _scheduler_or_503() -> SimpleScheduler:
    if _scheduler is None:
        raise HTTPException(status_code=503, detail="Scheduler not started")
    return _scheduler


@app.get("/schedules")
def list_schedules() -> dict[str, Any]:
    user = _require_user()
    if _scheduler is None:
        return {"jobs": [], "last_runs": {}}
    user_id = str(user["user_id"])
    return {
        "jobs": [_public_job(job) for job in _scheduler.jobs_for(user_id)],
        "last_runs": _scheduler.last_runs_for(user_id),
    }


@app.post("/schedules")
def create_schedule(body: ScheduleWrite) -> dict[str, Any]:
    user = _require_user()
    job = _scheduler_or_503().upsert_job(_schedule_payload(body, str(user["user_id"])))
    return {"job": _public_job(job)}


@app.put("/schedules/{name}")
def update_schedule(name: str, body: ScheduleWrite) -> dict[str, Any]:
    if body.name.strip() != name:
        raise HTTPException(status_code=400, detail="Name in the body must match the URL.")
    user = _require_user()
    job = _scheduler_or_503().upsert_job(_schedule_payload(body, str(user["user_id"])))
    return {"job": _public_job(job)}


@app.delete("/schedules/{name}")
def remove_schedule(name: str) -> dict[str, str]:
    user = _require_user()
    try:
        _scheduler_or_503().delete_job(name, str(user["user_id"]))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "deleted", "name": name}


@app.post("/schedules/{name}/run")
def run_schedule_now(name: str) -> dict[str, Any]:
    user = _require_user()
    try:
        reply = _scheduler_or_503().run_job_once(name, str(user["user_id"]))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"name": name, "reply": reply}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    print(f"Telegram webhook received: {request}")
    # log headers
    print(f"Headers: {request.headers}")
    # log body
    print(f"Body: {await request.body()}")
    if not webhook_secret_ok(request.headers.get("x-telegram-bot-api-secret-token")):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    try:
        update = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(update, dict):
        raise HTTPException(status_code=400, detail="Invalid update")
    threading.Thread(
        target=_run_update,
        args=(update,),
        name="telegram-update",
        daemon=True,
    ).start()
    return {"ok": True}


@app.post("/telegram/send")
def telegram_send(payload: dict[str, Any]) -> dict[str, Any]:
    _require_user()
    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    result = bot_handler.send(message)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "send failed"))
    return result


@app.post("/news/digest")
def news_digest_endpoint(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    _require_user()
    payload = payload or {}
    result = run_news_digest(
        dry_run=bool(payload.get("dry_run", False)),
        to_addr=payload.get("to"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "news digest failed"))
    return result


@app.post("/reminders/run")
def reminders_endpoint(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    _require_user()
    payload = payload or {}
    result = run_reminders(dry_run=bool(payload.get("dry_run", False)))
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "reminders failed"))
    return result


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
