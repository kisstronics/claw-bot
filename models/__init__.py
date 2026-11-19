"""
Model providers (Bedrock, Groq, OpenRouter).

Select with MODEL_PROVIDER=bedrock|groq|openrouter (default: bedrock).
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


def _env_provider() -> str:
    return _clean_provider(os.environ.get("MODEL_PROVIDER", "bedrock") or "bedrock")


def _env_model_id(provider: str) -> str:
    explicit = os.environ.get("MODEL_ID", "").strip().strip('"').strip("'")
    if explicit:
        return explicit
    if provider == "groq":
        return os.environ.get("GROQ_MODEL_ID", "llama-3.3-70b-versatile").strip()
    if provider in {"openrouter", "open_router"}:
        return os.environ.get("OPENROUTER_MODEL_ID", "openai/gpt-4o-mini").strip()
    return os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-haiku-5-5").strip()


def _initial_selection() -> tuple[str, str]:
    saved = load_settings()
    provider = _clean_provider(str(saved.get("provider") or _env_provider()))
    model_id = str(saved.get("model_id") or "").strip() or _env_model_id(provider)
    return provider, model_id


PROVIDER_NAME, MODEL_ID = _initial_selection()

# Re-export common settings used by API/health
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID",
    "global.anthropic.claude-haiku-5-5",
)
GROQ_MODEL_ID = os.environ.get(
    "GROQ_MODEL_ID",
    "llama-3.3-70b-versatile",
)
OPENROUTER_MODEL_ID = os.environ.get(
    "OPENROUTER_MODEL_ID",
    "openai/gpt-4o-mini",
)

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
    return {"provider": PROVIDER_NAME, "model_id": MODEL_ID, "region": AWS_REGION}


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
    if provider == "aws" or provider == "aws_bedrock":
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
    return active_selection()


def get_provider() -> ModelProvider:
    """Model provider for the signed-in user, else the process default."""
    global _provider
    from user_scope import current_user_id

    user_id = current_user_id()
    if user_id:
        from accounts import model_selection

        chosen = model_selection(user_id)
        if chosen:
            return _construct(chosen["provider"], chosen["model_id"])
    if _provider is not None:
        return _provider
    _provider = _construct(PROVIDER_NAME, MODEL_ID)
    return _provider


# Back-compat alias used by older call sites
def make_client():
    return get_provider()
