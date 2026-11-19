"""AWS Bedrock Converse provider."""

from __future__ import annotations

from typing import Any

import boto3
from dotenv import load_dotenv

load_dotenv()


class BedrockProvider:
    def __init__(self, model_id: str, region: str = "us-east-1") -> None:
        self._model_id = model_id
        self._region = region
        self._client = boto3.client("bedrock-runtime", region_name=region)

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def provider_name(self) -> str:
        return "bedrock"

    @property
    def region(self) -> str:
        return self._region

    def converse(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str,
        tool_config: dict[str, Any],
    ) -> dict[str, Any]:
        return self._client.converse(
            modelId=self._model_id,
            messages=messages,
            system=[{"text": system}],
            toolConfig=tool_config,
            inferenceConfig={"maxTokens": 4096},
        )
