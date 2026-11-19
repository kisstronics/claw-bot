"""
OpenRouter chat-completions provider (OpenAI-compatible API).

Uses the same Bedrock-style messages and tools as Groq.

Env:
  OPENROUTER_API_KEY or OPEN_ROUTER_KEY
  OPENROUTER_MODEL_ID / MODEL_ID
  OPENROUTER_BASE_URL (optional, default https://openrouter.ai/api/v1)
  OPENROUTER_SITE_URL (optional HTTP-Referer)
  OPENROUTER_APP_NAME (optional X-Title, default open-claw-knics)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from models.groq import (
    bedrock_messages_to_openai,
    bedrock_tools_to_openai,
    openai_response_to_bedrock,
)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL_ID = "openai/gpt-4o-mini"
_RETRYABLE_STATUS = {429, 502, 503, 504}
_MAX_ATTEMPTS = 3
_MAX_RETRY_WAIT_SEC = 8.0


def openrouter_api_key() -> str:
    return (
        os.environ.get("OPENROUTER_API_KEY", "").strip()
        or os.environ.get("OPEN_ROUTER_KEY", "").strip()
    )


def _response_obj(data: dict[str, Any]) -> SimpleNamespace:
    choices: list[SimpleNamespace] = []
    for choice in data.get("choices") or []:
        message = choice.get("message") or {}
        tool_calls: list[SimpleNamespace] = []
        for index, tool_call in enumerate(message.get("tool_calls") or []):
            function = tool_call.get("function") or {}
            tool_calls.append(
                SimpleNamespace(
                    id=tool_call.get("id") or f"call_openrouter_{index}",
                    function=SimpleNamespace(
                        name=function.get("name") or "",
                        arguments=function.get("arguments") or "{}",
                    ),
                )
            )
        choices.append(
            SimpleNamespace(
                message=SimpleNamespace(
                    content=message.get("content"),
                    tool_calls=tool_calls,
                ),
                finish_reason=choice.get("finish_reason") or "",
            )
        )
    usage = data.get("usage") or {}
    return SimpleNamespace(
        choices=choices,
        usage=SimpleNamespace(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        ),
    )


def _error_message(raw: str) -> str:
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip() or "request failed"
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or "request failed")
            meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
            extra: list[str] = []
            provider = meta.get("provider_name")
            if provider:
                extra.append(str(provider))
            raw_detail = meta.get("raw")
            if isinstance(raw_detail, str):
                detail = raw_detail.strip()
                if detail and detail != message:
                    extra.append(detail[:180])
            if extra:
                return f"{message} ({'; '.join(extra)})"
            return message
        if err:
            return str(err)
    return raw.strip() or "request failed"


def _retry_wait_seconds(
    status: int,
    retry_after: str | None,
    attempt: int,
    *,
    max_attempts: int = _MAX_ATTEMPTS,
) -> float | None:
    """Seconds to wait before another try. None means do not retry."""
    if status not in _RETRYABLE_STATUS or attempt >= max_attempts:
        return None
    delay: float | None = None
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            delay = None
    if delay is None:
        delay = float(2 ** (attempt - 1))
    if delay > _MAX_RETRY_WAIT_SEC:
        return None
    return max(0.0, delay)


def _rate_limit_response(model_id: str) -> dict[str, Any]:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "text": (
                            f"OpenRouter rate-limited `{model_id}` (429). "
                            "Wait a minute and try again."
                        )
                    }
                ],
            }
        },
        "stopReason": "end_turn",
        "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
    }


class OpenRouterProvider:
    def __init__(self, model_id: str) -> None:
        api_key = openrouter_api_key()
        if not api_key:
            raise RuntimeError(
                "Set OPENROUTER_API_KEY or OPEN_ROUTER_KEY to use MODEL_PROVIDER=openrouter."
            )
        self._api_key = api_key
        self._model_id = model_id or DEFAULT_MODEL_ID
        base = os.environ.get("OPENROUTER_BASE_URL", "").strip() or DEFAULT_BASE_URL
        self._url = base.rstrip("/") + "/chat/completions"
        self._site_url = os.environ.get("OPENROUTER_SITE_URL", "").strip()
        self._app_name = os.environ.get("OPENROUTER_APP_NAME", "").strip() or "open-claw-knics"

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def provider_name(self) -> str:
        return "openrouter"

    def converse(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str,
        tool_config: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model_id,
            "messages": [
                {"role": "system", "content": system},
                *bedrock_messages_to_openai(messages),
            ],
            "max_tokens": 4096,
            "temperature": 0.2,
        }
        tools = bedrock_tools_to_openai(tool_config)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Title": self._app_name,
        }
        if self._site_url:
            headers["HTTP-Referer"] = self._site_url

        body = json.dumps(payload).encode("utf-8")
        data: dict[str, Any] | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            request = urllib.request.Request(
                self._url,
                data=body,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    data = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                message = _error_message(raw)
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                wait = _retry_wait_seconds(exc.code, retry_after, attempt)
                if wait is None:
                    if exc.code == 429:
                        print(f"[openrouter] rate limited `{self._model_id}`: {message}")
                        return _rate_limit_response(self._model_id)
                    raise RuntimeError(
                        f"OpenRouter error {exc.code}: {message}"
                    ) from exc
                print(
                    f"[openrouter] {exc.code} on `{self._model_id}`; "
                    f"retrying in {wait:.1f}s ({attempt}/{_MAX_ATTEMPTS})"
                )
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise RuntimeError(f"OpenRouter request failed: {exc}") from exc

        if not isinstance(data, dict) or not data.get("choices"):
            raise RuntimeError("OpenRouter returned no choices.")
        return openai_response_to_bedrock(_response_obj(data))
