"""Unit tests for :class:`app.adapters.llm.ollama.OllamaClient`."""

from __future__ import annotations

import base64
import json
from typing import cast

import httpx

from app.adapters.llm.ollama import OllamaClient
from app.adapters.llm.ports import ChatContent, ChatMessage


class _RecordingHandler:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "message": {"role": "assistant", "content": "translated transcript"},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 4,
                "eval_count": 2,
            },
        )


def _make_client(handler: _RecordingHandler) -> OllamaClient:
    return OllamaClient(
        None,
        base_url="http://127.0.0.1:11434/v1",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _json_body(request: httpx.Request) -> dict[str, object]:
    return cast(dict[str, object], json.loads(request.content.decode("utf-8")))


def test_multimodal_audio_keeps_large_base64_blob() -> None:
    handler = _RecordingHandler()
    client = _make_client(handler)
    audio_payload = base64.b64encode(b"\xff" * 2048).decode("ascii")
    content: ChatContent = [
        {"type": "text", "text": "Transcribe and translate this audio to English"},
        {
            "type": "input_audio",
            "input_audio": {"data": audio_payload, "format": "mp3"},
        },
    ]
    messages: list[ChatMessage] = [{"role": "user", "content": content}]

    response = client.chat(model_id="gemma4:e4b", messages=messages)

    assert response.text == "translated transcript"
    req = handler.requests[0]
    assert str(req.url) == "http://127.0.0.1:11434/api/chat"
    body = _json_body(req)
    assert body["messages"] == [
        {
            "role": "user",
            "content": "Transcribe and translate this audio to English",
            "images": [audio_payload],
        }
    ]
