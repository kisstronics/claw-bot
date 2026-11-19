"""
Groq chat-completions provider (OpenAI-compatible API).

Translates Bedrock-style messages/tools <-> Groq, so the agent loop stays unchanged.

Env:
  GROQ_API_KEY
  GROQ_MODEL_ID / MODEL_ID
  GROQ_BASE_URL (optional)
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from dotenv import load_dotenv
from groq import APIStatusError, Groq

load_dotenv()


def bedrock_tools_to_openai(tool_config: dict[str, Any]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for entry in tool_config.get("tools") or []:
        spec = entry.get("toolSpec") or {}
        schema = (spec.get("inputSchema") or {}).get("json") or {
            "type": "object",
            "properties": {},
        }
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": spec.get("name", "unknown"),
                    "description": spec.get("description") or "",
                    "parameters": schema,
                },
            }
        )
    return tools


def bedrock_messages_to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        blocks = msg.get("content") or []

        if role == "assistant":
            texts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in blocks:
                if "text" in block and block["text"] is not None:
                    texts.append(block["text"])
                if "toolUse" in block:
                    tu = block["toolUse"]
                    tool_calls.append(
                        {
                            "id": tu["toolUseId"],
                            "type": "function",
                            "function": {
                                "name": tu["name"],
                                "arguments": json.dumps(tu.get("input") or {}),
                            },
                        }
                    )
            item: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(texts) if texts else None,
            }
            if tool_calls:
                item["tool_calls"] = tool_calls
            out.append(item)
            continue

        if role == "user":
            texts = []
            for block in blocks:
                if "text" in block and block["text"] is not None:
                    texts.append(block["text"])
                if "toolResult" in block:
                    tr = block["toolResult"]
                    payload = tr.get("content") or []
                    if payload and isinstance(payload[0], dict) and "json" in payload[0]:
                        body = json.dumps(payload[0]["json"])
                    elif payload and isinstance(payload[0], dict) and "text" in payload[0]:
                        body = str(payload[0]["text"])
                    else:
                        body = json.dumps(payload)
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": tr["toolUseId"],
                            "content": body,
                        }
                    )
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
            continue

        # Fallback: stringify
        out.append({"role": role or "user", "content": json.dumps(blocks)})

    return out


_FUNC_RE = re.compile(
    r"<function(?:=|\s+name\s*=\s*[\"'])([A-Za-z0-9_\-]+)",
    re.I,
)
_PARAM_RE = re.compile(
    r"<(?:parameter\s*=\s*)?([A-Za-z_][A-Za-z0-9_\-]*)>\s*(.*?)\s*</(?:parameter|\1)\s*>",
    re.S | re.I,
)


def _error_payload(exc: BaseException) -> dict[str, Any]:
    body = getattr(exc, "body", None)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return {"message": body}
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return err
        return body
    return {"message": str(exc)}


def _json_objects(raw: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    index = 0
    while True:
        start = raw.find("{", index)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(raw, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        index = end
        if isinstance(obj, dict):
            found.append(obj)
    return found


def _coerce_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
        return {"raw": text} if text else {}
    return {}


def parse_failed_tool_generation(raw: str) -> list[dict[str, Any]]:
    """
    Groq returns tool_use_failed when a model emits XML (or other non-schema
    arguments) instead of JSON. Recover name + parameters from that text.
    """
    raw = raw or ""
    calls: list[dict[str, Any]] = []
    matches = list(_FUNC_RE.finditer(raw))
    for index, match in enumerate(matches):
        name = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        body = raw[match.end() : end]
        params: dict[str, Any] = {}
        for param in _PARAM_RE.finditer(body):
            key = param.group(1)
            if key.lower() in {"function", "tool_call"}:
                continue
            params[key] = param.group(2).strip()
        if not params:
            text = re.sub(r"</?[^>]+>", " ", body)
            text = " ".join(text.split()).strip()
            if text and name == "run_command":
                params["command"] = text
        if name:
            calls.append({"name": name, "input": params})

    if calls:
        return calls

    for obj in _json_objects(raw):
        name = str(obj.get("name") or obj.get("function") or "").strip()
        if not name:
            continue
        args = obj.get("arguments", obj.get("parameters", obj.get("input")))
        calls.append({"name": name, "input": _coerce_args(args)})
    return calls


def _tool_use_response(calls: list[dict[str, Any]]) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        tool_input = call.get("input") if isinstance(call.get("input"), dict) else {}
        content.append(
            {
                "toolUse": {
                    "toolUseId": f"call_recovered_{index}",
                    "name": call["name"],
                    "input": tool_input,
                }
            }
        )
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
    }


def recover_tool_use_failure(exc: BaseException) -> dict[str, Any] | None:
    """Turn Groq's tool_use_failed 400 into a normal tool-use response, if possible."""
    err = _error_payload(exc)
    code = str(err.get("code") or "")
    message = str(err.get("message") or "")
    if code != "tool_use_failed" and "tool call validation failed" not in message.lower():
        return None
    calls = parse_failed_tool_generation(str(err.get("failed_generation") or ""))
    if not calls:
        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "text": (
                                "The model tried to call a tool with malformed arguments. "
                                "Please try that request again."
                            )
                        }
                    ],
                }
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
        }
    names = ", ".join(call["name"] for call in calls)
    print(f"[groq] recovered malformed tool call: {names}")
    return _tool_use_response(calls)


def openai_response_to_bedrock(resp: Any) -> dict[str, Any]:
    choice = resp.choices[0]
    msg = choice.message
    content: list[dict[str, Any]] = []

    if msg.content:
        content.append({"text": msg.content})

    stop_reason = "end_turn"
    tool_calls = getattr(msg, "tool_calls", None) or []
    if tool_calls:
        stop_reason = "tool_use"
        for tc in tool_calls:
            raw_args = tc.function.arguments or "{}"
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                recovered = parse_failed_tool_generation(raw_args)
                if len(recovered) == 1 and not recovered[0]["input"]:
                    recovered[0]["input"] = {"raw": raw_args}
                args = recovered[0]["input"] if len(recovered) == 1 else {"raw": raw_args}
            content.append(
                {
                    "toolUse": {
                        "toolUseId": tc.id,
                        "name": tc.function.name,
                        "input": args if isinstance(args, dict) else {"value": args},
                    }
                }
            )

    # finish_reason may also indicate tool calls
    fr = (choice.finish_reason or "").lower()
    if fr == "tool_calls":
        stop_reason = "tool_use"

    usage = getattr(resp, "usage", None)
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0

    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop_reason,
        "usage": {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": input_tokens + output_tokens,
        },
    }


class GroqProvider:
    def __init__(self, model_id: str) -> None:
        api_key = os.environ.get("GROQ_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("Set GROQ_API_KEY to use MODEL_PROVIDER=groq.")
        base_url = os.environ.get("GROQ_BASE_URL", "").strip() or None
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = Groq(**kwargs)
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def provider_name(self) -> str:
        return "groq"

    def converse(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str,
        tool_config: dict[str, Any],
    ) -> dict[str, Any]:
        openai_messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            *bedrock_messages_to_openai(messages),
        ]
        tools = bedrock_tools_to_openai(tool_config)
        kwargs: dict[str, Any] = {
            "model": self._model_id,
            "messages": openai_messages,
            "max_tokens": 4096,
            "temperature": 0.2,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            resp = self._client.chat.completions.create(**kwargs)
        except APIStatusError as exc:
            recovered = recover_tool_use_failure(exc)
            if recovered is None:
                raise
            return recovered
        return openai_response_to_bedrock(resp)
