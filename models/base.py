"""Shared model provider interface (Bedrock-shaped responses)."""

from __future__ import annotations

from typing import Any, Protocol

from dotenv import load_dotenv

load_dotenv()


class ModelProvider(Protocol):
    """Providers accept Bedrock-style messages/tools and return Bedrock-like dicts."""

    @property
    def model_id(self) -> str: ...

    @property
    def provider_name(self) -> str: ...

    def converse(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str,
        tool_config: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Returns:
          {
            "output": {"message": {"role": "assistant", "content": [...]}},
            "stopReason": "end_turn" | "tool_use" | ...,
            "usage": {"inputTokens": int, "outputTokens": int},
          }
        """
        ...
