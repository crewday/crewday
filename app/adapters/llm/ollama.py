"""Ollama-native LLM adapter.

Ollama exposes an OpenAI-compatible surface for ordinary chat, but its
Gemma 4 audio path is native-Ollama shaped: multimodal blobs ride the
message ``images`` array, even when the blob is audio. This adapter keeps
that transport-specific shape out of the admin playground and domain
client code.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from typing import TypedDict, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import SecretStr

from app.adapters.llm.ports import (
    ChatInputAudioRef,
    ChatMessage,
    LLMCapabilityMissing,
    LlmProviderError,
    LlmRateLimited,
    LLMResponse,
    LlmThinkingLevel,
    LlmThinkingStrategy,
    LlmTransportError,
    LLMUsage,
    Tool,
)
from app.adapters.llm.shared import (
    build_data_url as _build_data_url,
)
from app.adapters.llm.shared import (
    redact_body as _redact_body,
)
from app.adapters.llm.shared import (
    safe_error_detail as _safe_error_detail,
)
from app.util.redact import ConsentSet

__all__ = ["OllamaClient", "ollama_api_base_url"]


class _OllamaMessage(TypedDict, total=False):
    role: str
    content: str
    images: list[str]


class _OllamaResponseMessage(TypedDict, total=False):
    role: str
    content: str


class _OllamaResponse(TypedDict, total=False):
    model: str
    message: _OllamaResponseMessage
    done_reason: str
    prompt_eval_count: int
    eval_count: int


def ollama_api_base_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint.rstrip("/"))
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    if not path.endswith("/api"):
        path = f"{path}/api" if path else "/api"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class OllamaClient:
    def __init__(
        self,
        api_key: SecretStr | None,
        *,
        base_url: str,
        timeout: float = 60.0,
        provider_label: str = "ollama",
        http: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = ollama_api_base_url(base_url)
        self._timeout = timeout
        self._provider_label = provider_label.strip() or "ollama"
        self._http = http or httpx.Client(timeout=timeout)

    def is_configured(self) -> bool:
        return True

    def complete(
        self,
        *,
        model_id: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        thinking_level: LlmThinkingLevel = "disabled",
        thinking_strategy: LlmThinkingStrategy = "none",
        consents: ConsentSet | None = None,
    ) -> LLMResponse:
        del thinking_level, thinking_strategy
        return self.chat(
            model_id=model_id,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
            consents=consents,
        )

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        thinking_level: LlmThinkingLevel = "disabled",
        thinking_strategy: LlmThinkingStrategy = "none",
        tools: Sequence[Tool] | None = None,
        consents: ConsentSet | None = None,
    ) -> LLMResponse:
        del thinking_strategy
        body: dict[str, object] = {
            "model": model_id,
            "messages": _ollama_messages(messages),
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if thinking_level != "disabled":
            body["think"] = thinking_level
        if tools:
            body["tools"] = [_ollama_tool(tool) for tool in tools]
        payload = self._post("/chat", _redact_body(body, consents))
        return _parse_chat_response(payload, requested_model=model_id)

    def transcribe(
        self,
        *,
        model_id: str,
        audio: ChatInputAudioRef,
        temperature: float = 0.0,
        consents: ConsentSet | None = None,
    ) -> LLMResponse:
        return self.chat(
            model_id=model_id,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Transcribe this audio."},
                        {"type": "input_audio", "input_audio": audio},
                    ],
                }
            ],
            temperature=temperature,
            consents=consents,
        )

    def ocr(
        self,
        *,
        model_id: str,
        image_bytes: bytes,
        consents: ConsentSet | None = None,
    ) -> str:
        if not image_bytes:
            raise ValueError("ocr requires non-empty image_bytes")
        response = self.chat(
            model_id=model_id,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract the visible text."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": _build_data_url(
                                    image_bytes, mime_type="image/jpeg"
                                )
                            },
                        },
                    ],
                }
            ],
            consents=consents,
        )
        return response.text

    def stream_chat(self, **_kwargs: object) -> Iterator[str]:
        raise LLMCapabilityMissing("stream_chat")

    def _post(self, path: str, body: Mapping[str, object]) -> Mapping[str, object]:
        headers = {"Content-Type": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"
        try:
            response = self._http.post(
                f"{self._base_url}{path}",
                headers=headers,
                json=dict(body),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise LlmTransportError(
                f"{self._provider_label} request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise LlmTransportError(
                f"{self._provider_label} transport failed: {type(exc).__name__}"
            ) from exc
        if response.status_code == 429:
            raise LlmRateLimited(f"{self._provider_label} rate limited")
        if 400 <= response.status_code < 500:
            raise LlmProviderError(
                f"{self._provider_label} rejected request: {response.status_code} "
                f"{_safe_error_detail(response)}"
            )
        if response.status_code >= 500:
            raise LlmTransportError(
                f"{self._provider_label} returned {response.status_code}: "
                f"{_safe_error_detail(response)}"
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise LlmTransportError(
                f"{self._provider_label} returned non-JSON body"
            ) from exc
        if not isinstance(payload, dict):
            raise LlmTransportError(f"{self._provider_label} returned non-object JSON")
        return cast(Mapping[str, object], payload)


def _ollama_messages(messages: Sequence[ChatMessage]) -> list[_OllamaMessage]:
    return [_ollama_message(message) for message in messages]


def _ollama_message(message: ChatMessage) -> _OllamaMessage:
    content = message["content"]
    if isinstance(content, str):
        return {"role": message["role"], "content": content}
    text: list[str] = []
    blobs: list[str] = []
    for block in content:
        if block["type"] == "text":
            text.append(block["text"])
        elif block["type"] == "image_url":
            blobs.append(_image_data(block["image_url"]["url"]))
        elif block["type"] == "input_audio":
            blobs.append(block["input_audio"]["data"])
    out: _OllamaMessage = {"role": message["role"], "content": "\n".join(text)}
    if blobs:
        out["images"] = blobs
    return out


def _image_data(value: str) -> str:
    if value.startswith("data:"):
        _prefix, sep, payload = value.partition(",")
        if sep and payload:
            return payload
    return value


def _ollama_tool(tool: Tool) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }


def _parse_chat_response(
    payload: Mapping[str, object], *, requested_model: str
) -> LLMResponse:
    response = cast(_OllamaResponse, payload)
    message = response.get("message") or {}
    text = message.get("content", "") or ""
    prompt_tokens = int(response.get("prompt_eval_count", 0) or 0)
    completion_tokens = int(response.get("eval_count", 0) or 0)
    return LLMResponse(
        text=text,
        usage=LLMUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        model_id=response.get("model", "") or requested_model,
        finish_reason=response.get("done_reason", "") or "stop",
    )
