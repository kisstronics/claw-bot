"""
Model providers (Bedrock, Groq, OpenRouter).

Select with MODEL_PROVIDER=bedrock|groq|openrouter.
Resolved live from claw.app_config / env (not frozen at import).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from dotenv import load_dotenv

from config import load_settings, save_settings

load_dotenv()

if TYPE_CHECKING:
    from models.base import ModelProvider


def _clean_provider(value: str) -> str:
    return value.strip().strip('"').strip("'").lower()


def _config_value(key: str, default: str = "") -> str:
    try:
        import db

        return db.get_config(key, default)
    except Exception:  # noqa: BLE001 — DB may not be ready yet
        return os.environ.get(key, default).strip() or default


def _env_provider() -> str:
    return _clean_provider(_config_value("MODEL_PROVIDER", "openrouter") or "openrouter")


def _env_model_id(provider: str) -> str:
    explicit = _config_value("MODEL_ID", "").strip().strip('"').strip("'")
    if explicit:
        return explicit
    if provider == "groq":
        return _config_value("GROQ_MODEL_ID", "llama-3.3-70b-versatile")
    if provider in {"openrouter", "open_router"}:
        return _config_value("OPENROUTER_MODEL_ID", "openai/gpt-4o-mini")
    return _config_value("BEDROCK_MODEL_ID", "global.anthropic.claude-haiku-5-5")


def resolve_default_selection() -> tuple[str, str]:
    """Process-wide default provider/model from DB settings, then env/app_config."""
    saved = load_settings()
    provider = _clean_provider(str(saved.get("provider") or _env_provider()))
    if provider in {"aws", "aws_bedrock"}:
        provider = "bedrock"
    if provider == "open_router":
        provider = "openrouter"
    model_id = str(saved.get("model_id") or "").strip() or _env_model_id(provider)
    return provider, model_id


def refresh_defaults() -> dict[str, str]:
    """Re-read defaults after DB init. Call from app lifespan."""
    global PROVIDER_NAME, MODEL_ID, AWS_REGION, BEDROCK_MODEL_ID, GROQ_MODEL_ID, OPENROUTER_MODEL_ID
    global MODEL_CATALOG, _provider
    PROVIDER_NAME, MODEL_ID = resolve_default_selection()
    AWS_REGION = _config_value("AWS_REGION", "us-east-1") or "us-east-1"
    BEDROCK_MODEL_ID = _config_value("BEDROCK_MODEL_ID", "global.anthropic.claude-haiku-5-5")
    GROQ_MODEL_ID = _config_value("GROQ_MODEL_ID", "llama-3.3-70b-versatile")
    OPENROUTER_MODEL_ID = _config_value("OPENROUTER_MODEL_ID", "openai/gpt-4o-mini")
    MODEL_CATALOG = {
        "bedrock": [
            BEDROCK_MODEL_ID,
            "global.anthropic.claude-haiku-5-5",
            "anthropic.claude-3-5-haiku-20241022-v1:0",
        ],
        "groq": [
            GROQ_MODEL_ID,
            "llama-3.3-70b-versatile",
            "qwen/qwen3.8-27b",
            "openai/gpt-oss-120b",
        ],
        "openrouter": [
            OPENROUTER_MODEL_ID,
            "openai/gpt-4o-mini",
            "google/gemma-4-26b-a4b-it:free",
            "anthropic/claude-3.5-haiku",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
        ],
    }
    _provider = None
    return {"provider": PROVIDER_NAME, "model_id": MODEL_ID, "region": AWS_REGION}


PROVIDER_NAME, MODEL_ID = resolve_default_selection()
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-haiku-5-5")
GROQ_MODEL_ID = os.environ.get("GROQ_MODEL_ID", "llama-3.3-70b-versatile")
OPENROUTER_MODEL_ID = os.environ.get("OPENROUTER_MODEL_ID", "openai/gpt-4o-mini")

MODEL_CATALOG: dict[str, list[str]] = {
    "bedrock": [
        BEDROCK_MODEL_ID,
        "global.anthropic.claude-haiku-5-5",
        "anthropic.claude-3-5-haiku-20241022-v1:0",
    ],
    "groq": [
        GROQ_MODEL_ID,
        "llama-3.3-70b-versatile",
        "qwen/qwen3.8-27b",
        "openai/gpt-oss-120b",
    ],
    "openrouter": [
        OPENROUTER_MODEL_ID,
        "openai/gpt-4o-mini",
        "google/gemma-4-26b-a4b-it:free",
        "anthropic/claude-3.5-haiku",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
    ],
}

_provider: ModelProvider | None = None


def active_selection() -> dict[str, str]:
    from user_scope import current_user_id

    user_id = current_user_id()
    if user_id:
        from accounts import model_selection

        chosen = model_selection(user_id)
        if chosen:
            return {**chosen, "region": AWS_REGION}
    provider, model_id = resolve_default_selection()
    return {"provider": provider, "model_id": model_id, "region": AWS_REGION}


def _construct(provider: str, model_id: str) -> ModelProvider:
    if provider == "groq":
        from models.groq import GroqProvider

        return GroqProvider(model_id=model_id)
    if provider in {"openrouter", "open_router"}:
        from models.openrouter import OpenRouterProvider

        return OpenRouterProvider(model_id=model_id)
    if provider in {"bedrock", "aws", "aws_bedrock"}:
        from models.bedrock import BedrockProvider

        return BedrockProvider(model_id=model_id, region=AWS_REGION)
    raise ValueError(
        f"Unknown MODEL_PROVIDER={provider!r}. Use 'bedrock', 'groq', or 'openrouter'."
    )


def apply_runtime_settings(provider: str, model_id: str) -> dict[str, str]:
    """Switch the live model for the signed-in user, or the process default."""
    global PROVIDER_NAME, MODEL_ID, _provider
    from user_scope import current_user_id

    provider = _clean_provider(provider)
    model_id = (model_id or "").strip()
    if provider not in {"bedrock", "aws", "aws_bedrock", "groq", "openrouter", "open_router"}:
        raise ValueError("Provider must be bedrock, groq, or openrouter.")
    if not model_id:
        raise ValueError("Model id is required.")
    provider = "openrouter" if provider == "open_router" else provider
    if provider in {"aws", "aws_bedrock"}:
        provider = "bedrock"
    user_id = current_user_id()
    if user_id:
        from accounts import save_model_selection

        save_model_selection(user_id, provider, model_id)
        return {"provider": provider, "model_id": model_id, "region": AWS_REGION}
    PROVIDER_NAME = provider
    MODEL_ID = model_id
    _provider = None
    save_settings({"provider": PROVIDER_NAME, "model_id": MODEL_ID})
    try:
        import db

        db.set_config("MODEL_PROVIDER", provider)
        if provider == "openrouter":
            db.set_config("OPENROUTER_MODEL_ID", model_id)
        elif provider == "groq":
            db.set_config("GROQ_MODEL_ID", model_id)
        else:
            db.set_config("BEDROCK_MODEL_ID", model_id)
    except Exception:  # noqa: BLE001
        pass
    return active_selection()


def get_provider() -> ModelProvider:
    """Model provider for the signed-in user, else the process default."""
    global _provider, PROVIDER_NAME, MODEL_ID
    from user_scope import current_user_id

    user_id = current_user_id()
    if user_id:
        from accounts import model_selection

        chosen = model_selection(user_id)
        if chosen:
            return _construct(chosen["provider"], chosen["model_id"])

    provider, model_id = resolve_default_selection()
    PROVIDER_NAME, MODEL_ID = provider, model_id
    if (
        _provider is not None
        and getattr(_provider, "provider_name", None) == provider
        and getattr(_provider, "model_id", None) == model_id
    ):
        return _provider
    _provider = _construct(provider, model_id)
    return _provider


def make_client():
    return get_provider()
