"""Unit tests for :class:`app.adapters.llm.openrouter.OpenRouterClient`.

Every scenario runs against an :class:`httpx.MockTransport` so the
test never opens a real socket. Fixture payloads under
``tests/fixtures/llm/`` stand in for OpenRouter's public response
shapes; each was written to mirror the ``/chat/completions`` schema
(OpenAI-compatible) so the replay stays useful even if OpenRouter
surfaces a new top-level field.

Scenario coverage (§17 Testing §"Unit" and §11 §"Providers"):

* Happy paths — ``complete`` / ``chat`` / ``ocr`` / ``stream_chat``.
* Request shape — headers (Bearer auth, attribution), body
  (messages, max_tokens, temperature, stream flag, data-URL for OCR).
* Retry taxonomy — 429 once then 200, 429 max_retries times →
  :class:`LlmRateLimited`, 500 once then 200, 500 max_retries
  times → :class:`LlmTransportError`, 400 →
  :class:`LlmProviderError`.
* Transport failures — timeouts, connect errors, malformed bodies.
* Streaming — ``[DONE]`` terminator, chunk order, mid-stream 429.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.llm.openrouter import (
    LlmProviderError,
    LlmRateLimited,
    LlmTransportError,
    OpenRouterClient,
)
from app.adapters.llm.ports import ChatMessage, LLMResponse
from app.util.clock import FrozenClock

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "llm"


def _load_fixture(name: str) -> dict[str, object]:
    """Load a fixture JSON blob as an already-parsed dict."""
    return cast(dict[str, object], json.loads((_FIXTURE_DIR / name).read_text()))


def _load_stream_fixture(name: str) -> bytes:
    """Load a fixture SSE stream as raw bytes (what httpx will stream)."""
    return (_FIXTURE_DIR / name).read_bytes()


_COMPLETE_FIXTURE = _load_fixture("openrouter_complete_smoke.json")
_CHAT_FIXTURE = _load_fixture("openrouter_chat_smoke.json")
_OCR_FIXTURE = _load_fixture("openrouter_ocr_smoke.json")
_ERROR_400_FIXTURE = _load_fixture("openrouter_error_400.json")
_STREAM_FIXTURE_BYTES = _load_stream_fixture("openrouter_stream_smoke.txt")

_API_KEY = SecretStr("sk-or-test-0123456789abcdef")
_MODEL = "google/gemma-3-27b-it"


class _RecordingHandler:
    """Records every inbound request and returns a scripted response.

    Supports two scripts:

    * ``responses``: one :class:`httpx.Response` per call. When the
      list is shorter than the request count we keep returning the
      last entry — useful when the retry loop is expected to stop
      after a known number of attempts.
    * ``raise_on``: map of ``attempt_index`` → ``Exception``. Raised
      from ``__call__`` before a response is produced, so we can
      simulate mid-request transport faults.
    """

    def __init__(
        self,
        *,
        responses: list[httpx.Response] | None = None,
        raise_on: dict[int, Exception] | None = None,
    ) -> None:
        self._responses = responses or []
        self._raise_on = raise_on or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        idx = len(self.requests)
        self.requests.append(request)
        if idx in self._raise_on:
            raise self._raise_on[idx]
        if idx < len(self._responses):
            return self._responses[idx]
        if self._responses:
            return self._responses[-1]
        raise AssertionError("handler invoked with no scripted response")


def _make_client(
    handler: _RecordingHandler,
    *,
    sleeps: list[float] | None = None,
    max_retries: int = 3,
) -> OpenRouterClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    clock = FrozenClock(datetime(2026, 4, 20, 9, 0, tzinfo=UTC))
    sleep_sink = sleeps.append if sleeps is not None else (lambda _s: None)
    return OpenRouterClient(
        _API_KEY,
        max_retries=max_retries,
        http=http,
        clock=clock,
        sleep=sleep_sink,
    )


def _json_body(request: httpx.Request) -> dict[str, object]:
    return cast(dict[str, object], json.loads(request.content.decode("utf-8")))


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestCompleteHappyPath:
    def test_returns_response_populated_from_fixture(self) -> None:
        handler = _RecordingHandler(
            responses=[httpx.Response(200, json=_COMPLETE_FIXTURE)]
        )
        client = _make_client(handler)

        resp = client.complete(
            model_id=_MODEL,
            prompt="What's the plan for the weekly bins?",
            max_tokens=64,
            temperature=0.2,
        )

        assert isinstance(resp, LLMResponse)
        choices = cast(list[dict[str, object]], _COMPLETE_FIXTURE["choices"])
        expected_text = cast(dict[str, object], choices[0]["message"])["content"]
        assert resp.text == expected_text
        assert resp.finish_reason == "stop"
        assert resp.model_id == _COMPLETE_FIXTURE["model"]
        assert resp.usage.prompt_tokens == 42
        assert resp.usage.completion_tokens == 18
        assert resp.usage.total_tokens == 60

    def test_request_body_shape_is_openai_compatible(self) -> None:
        handler = _RecordingHandler(
            responses=[httpx.Response(200, json=_COMPLETE_FIXTURE)]
        )
        client = _make_client(handler)
        client.complete(
            model_id=_MODEL,
            prompt="hello",
            max_tokens=32,
            temperature=0.1,
        )

        assert len(handler.requests) == 1
        req = handler.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/v1/chat/completions"
        body = _json_body(req)
        assert body["model"] == _MODEL
        assert body["max_tokens"] == 32
        assert body["temperature"] == 0.1
        assert "stream" not in body  # only set when streaming
        messages = cast(list[dict[str, object]], body["messages"])
        assert messages == [{"role": "user", "content": "hello"}]

    def test_request_headers_include_auth_and_attribution(self) -> None:
        handler = _RecordingHandler(
            responses=[httpx.Response(200, json=_COMPLETE_FIXTURE)]
        )
        client = _make_client(handler)
        client.complete(model_id=_MODEL, prompt="hi")

        req = handler.requests[0]
        assert req.headers["authorization"] == "Bearer sk-or-test-0123456789abcdef"
        assert req.headers["http-referer"] == "https://crew.day"
        assert req.headers["x-title"] == "crewday"
        assert req.headers["content-type"].startswith("application/json")


class TestChatHappyPath:
    def test_multi_turn_passes_messages_verbatim(self) -> None:
        handler = _RecordingHandler(responses=[httpx.Response(200, json=_CHAT_FIXTURE)])
        client = _make_client(handler)
        messages: list[ChatMessage] = [
            {"role": "system", "content": "You are a hospitality ops assistant."},
            {"role": "user", "content": "Tuesday morning overbooking on property 3"},
            {"role": "assistant", "content": "Confirmed. Which guest stays?"},
        ]

        resp = client.chat(
            model_id=_MODEL, messages=messages, max_tokens=128, temperature=0.0
        )

        assert resp.text.startswith("Block the Tuesday morning slot")
        body = _json_body(handler.requests[0])
        assert body["messages"] == messages


class TestOcrHappyPath:
    def test_image_bytes_are_base64_encoded_into_data_url(self) -> None:
        handler = _RecordingHandler(responses=[httpx.Response(200, json=_OCR_FIXTURE)])
        client = _make_client(handler)
        image_bytes = b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9"

        text = client.ocr(model_id=_MODEL, image_bytes=image_bytes)

        assert "COFFEE BEANS LTD" in text
        body = _json_body(handler.requests[0])
        messages = cast(list[dict[str, object]], body["messages"])
        assert len(messages) == 1
        content = cast(list[dict[str, object]], messages[0]["content"])
        assert content[0] == {
            "type": "text",
            "text": (
                "Extract every piece of visible text from this image verbatim. "
                "Preserve line breaks; do not summarise."
            ),
        }
        image_block = content[1]
        assert image_block["type"] == "image_url"
        image_url = cast(dict[str, object], image_block["image_url"])
        url = cast(str, image_url["url"])
        expected_b64 = base64.b64encode(image_bytes).decode("ascii")
        assert url == f"data:image/jpeg;base64,{expected_b64}"

    def test_empty_image_is_rejected(self) -> None:
        handler = _RecordingHandler()
        client = _make_client(handler)

        with pytest.raises(ValueError, match="non-empty image_bytes"):
            client.ocr(model_id=_MODEL, image_bytes=b"")
        # Never reached the wire.
        assert handler.requests == []


class TestStreamHappyPath:
    def test_yields_chunks_in_order_and_respects_done(self) -> None:
        handler = _RecordingHandler(
            responses=[
                httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=_STREAM_FIXTURE_BYTES,
                )
            ]
        )
        client = _make_client(handler)

        chunks = list(
            client.stream_chat(
                model_id=_MODEL,
                messages=[{"role": "user", "content": "Say hi."}],
            )
        )

        assert chunks == ["Hello", ", ", "crew", ".day", "!"]
        body = _json_body(handler.requests[0])
        assert body["stream"] is True
        assert body["model"] == _MODEL

    def test_stream_skips_malformed_sse_frames(self) -> None:
        """Junk between ``data:`` lines is logged and skipped — the stream survives."""
        noisy_stream = (
            b": keep-alive comment line\n\n"
            b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
            b"data: not-json-at-all\n\n"
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        handler = _RecordingHandler(
            responses=[
                httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=noisy_stream,
                )
            ]
        )
        client = _make_client(handler)

        chunks = list(
            client.stream_chat(
                model_id=_MODEL, messages=[{"role": "user", "content": "hi"}]
            )
        )
        assert chunks == ["ok"]

    def test_stream_request_disables_read_timeout(self) -> None:
        """Streaming calls lift the read timeout so long generations survive."""
        captured_timeouts: list[httpx.Timeout] = []
        real_stream = httpx.Client.stream

        def _capture_stream(
            self: httpx.Client, method: str, url: str, **kwargs: object
        ) -> object:
            timeout = kwargs.get("timeout")
            assert isinstance(timeout, httpx.Timeout)
            captured_timeouts.append(timeout)
            return real_stream(self, method, url, **kwargs)  # type: ignore[arg-type]

        handler = _RecordingHandler(
            responses=[
                httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=_STREAM_FIXTURE_BYTES,
                )
            ]
        )
        client = _make_client(handler)

        import unittest.mock as _mock

        with _mock.patch.object(httpx.Client, "stream", _capture_stream):
            list(
                client.stream_chat(
                    model_id=_MODEL, messages=[{"role": "user", "content": "hi"}]
                )
            )

        assert len(captured_timeouts) == 1
        timeout = captured_timeouts[0]
        # Read budget lifted for streaming; connect / write / pool still bounded.
        assert timeout.read is None
        assert timeout.connect is not None
        assert timeout.write is not None
        assert timeout.pool is not None


# ---------------------------------------------------------------------------
# Retry + error taxonomy
# ---------------------------------------------------------------------------


class TestRateLimitRetry:
    def test_429_then_200_succeeds_on_second_attempt(self) -> None:
        handler = _RecordingHandler(
            responses=[
                httpx.Response(429, json={"error": {"message": "slow down"}}),
                httpx.Response(200, json=_COMPLETE_FIXTURE),
            ]
        )
        sleeps: list[float] = []
        client = _make_client(handler, sleeps=sleeps)

        resp = client.complete(model_id=_MODEL, prompt="hi")

        assert resp.finish_reason == "stop"
        assert len(handler.requests) == 2
        # Single backoff slot was consumed between the two attempts.
        assert sleeps == [0.5]

    def test_429_exhausted_raises_llm_rate_limited(self) -> None:
        handler = _RecordingHandler(
            responses=[httpx.Response(429, json={"error": {"message": "slow down"}})]
        )
        sleeps: list[float] = []
        client = _make_client(handler, sleeps=sleeps, max_retries=3)

        with pytest.raises(LlmRateLimited, match="rate limited"):
            client.complete(model_id=_MODEL, prompt="hi")

        assert len(handler.requests) == 3
        # Two sleeps between three attempts, no sleep after the final failure.
        assert sleeps == [0.5, 1.0]


class TestServerErrorRetry:
    def test_500_then_200_succeeds_on_second_attempt(self) -> None:
        handler = _RecordingHandler(
            responses=[
                httpx.Response(500, text="boom"),
                httpx.Response(200, json=_COMPLETE_FIXTURE),
            ]
        )
        sleeps: list[float] = []
        client = _make_client(handler, sleeps=sleeps)

        resp = client.complete(model_id=_MODEL, prompt="hi")

        assert resp.finish_reason == "stop"
        assert len(handler.requests) == 2
        assert sleeps == [0.5]

    def test_500_exhausted_raises_llm_transport_error(self) -> None:
        handler = _RecordingHandler(responses=[httpx.Response(502, text="bad gateway")])
        sleeps: list[float] = []
        client = _make_client(handler, sleeps=sleeps, max_retries=3)

        with pytest.raises(LlmTransportError, match="502"):
            client.complete(model_id=_MODEL, prompt="hi")

        assert len(handler.requests) == 3


class TestNonRetryableClientError:
    def test_400_raises_llm_provider_error_without_retry(self) -> None:
        handler = _RecordingHandler(
            responses=[httpx.Response(400, json=_ERROR_400_FIXTURE)]
        )
        sleeps: list[float] = []
        client = _make_client(handler, sleeps=sleeps)

        with pytest.raises(LlmProviderError, match="not available"):
            client.complete(model_id="no-such-model/fake", prompt="hi")

        # One call, no retries, no sleeps.
        assert len(handler.requests) == 1
        assert sleeps == []

    def test_422_is_not_retried(self) -> None:
        """Unprocessable Entity is a spec violation — retrying is pointless."""
        handler = _RecordingHandler(
            responses=[
                httpx.Response(422, json={"error": {"message": "messages[0] invalid"}})
            ]
        )
        sleeps: list[float] = []
        client = _make_client(handler, sleeps=sleeps)

        with pytest.raises(LlmProviderError, match="422"):
            client.complete(model_id=_MODEL, prompt="hi")

        assert len(handler.requests) == 1
        assert sleeps == []


class TestRequestTimeoutRetry:
    """408 Request Timeout is transient and must retry like 429 / 5xx."""

    def test_408_then_200_succeeds_on_second_attempt(self) -> None:
        handler = _RecordingHandler(
            responses=[
                httpx.Response(408, json={"error": {"message": "took too long"}}),
                httpx.Response(200, json=_COMPLETE_FIXTURE),
            ]
        )
        sleeps: list[float] = []
        client = _make_client(handler, sleeps=sleeps)

        resp = client.complete(model_id=_MODEL, prompt="hi")

        assert resp.finish_reason == "stop"
        assert len(handler.requests) == 2
        assert sleeps == [0.5]

    def test_408_exhausted_raises_llm_transport_error(self) -> None:
        handler = _RecordingHandler(
            responses=[
                httpx.Response(408, json={"error": {"message": "took too long"}})
            ]
        )
        sleeps: list[float] = []
        client = _make_client(handler, sleeps=sleeps, max_retries=3)

        with pytest.raises(LlmTransportError, match="408"):
            client.complete(model_id=_MODEL, prompt="hi")

        assert len(handler.requests) == 3
        assert sleeps == [0.5, 1.0]


class TestTransportFailures:
    def test_timeout_exhausted_raises_llm_transport_error(self) -> None:
        handler = _RecordingHandler(
            raise_on={
                0: httpx.ReadTimeout("read timed out"),
                1: httpx.ReadTimeout("read timed out"),
                2: httpx.ReadTimeout("read timed out"),
            }
        )
        sleeps: list[float] = []
        client = _make_client(handler, sleeps=sleeps, max_retries=3)

        with pytest.raises(LlmTransportError, match="timed out"):
            client.complete(model_id=_MODEL, prompt="hi")

        assert len(handler.requests) == 3

    def test_timeout_then_success_recovers(self) -> None:
        handler = _RecordingHandler(
            responses=[httpx.Response(200, json=_COMPLETE_FIXTURE)],
            raise_on={0: httpx.ReadTimeout("first attempt timed out")},
        )
        client = _make_client(handler)

        resp = client.complete(model_id=_MODEL, prompt="hi")
        assert resp.finish_reason == "stop"
        assert len(handler.requests) == 2

    def test_connect_error_surfaces_as_transport_error(self) -> None:
        handler = _RecordingHandler(
            raise_on={
                0: httpx.ConnectError("connection refused"),
                1: httpx.ConnectError("connection refused"),
                2: httpx.ConnectError("connection refused"),
            }
        )
        client = _make_client(handler, max_retries=3)

        with pytest.raises(LlmTransportError, match="ConnectError"):
            client.complete(model_id=_MODEL, prompt="hi")

    def test_malformed_json_body_raises_transport_error(self) -> None:
        handler = _RecordingHandler(
            responses=[httpx.Response(200, text="<html>not json</html>")]
        )
        client = _make_client(handler)

        with pytest.raises(LlmTransportError, match="non-JSON"):
            client.complete(model_id=_MODEL, prompt="hi")

    def test_missing_choices_raises_transport_error(self) -> None:
        empty_payload = {"id": "gen-x", "model": _MODEL, "choices": []}
        handler = _RecordingHandler(responses=[httpx.Response(200, json=empty_payload)])
        client = _make_client(handler)

        with pytest.raises(LlmTransportError, match="no choices"):
            client.complete(model_id=_MODEL, prompt="hi")


# ---------------------------------------------------------------------------
# Streaming error paths
# ---------------------------------------------------------------------------


class TestStreamErrors:
    def test_mid_stream_429_raises_rate_limited(self) -> None:
        handler = _RecordingHandler(
            responses=[httpx.Response(429, json={"error": {"message": "slow down"}})]
        )
        client = _make_client(handler)

        iterator: Iterator[str] = client.stream_chat(
            model_id=_MODEL,
            messages=[{"role": "user", "content": "hi"}],
        )
        with pytest.raises(LlmRateLimited):
            list(iterator)

    def test_stream_4xx_raises_provider_error(self) -> None:
        handler = _RecordingHandler(
            responses=[httpx.Response(400, json=_ERROR_400_FIXTURE)]
        )
        client = _make_client(handler)

        with pytest.raises(LlmProviderError, match="not available"):
            list(
                client.stream_chat(
                    model_id="no-such-model/fake",
                    messages=[{"role": "user", "content": "hi"}],
                )
            )

    def test_stream_5xx_raises_transport_error(self) -> None:
        handler = _RecordingHandler(responses=[httpx.Response(503, text="unavailable")])
        client = _make_client(handler)

        with pytest.raises(LlmTransportError, match="503"):
            list(
                client.stream_chat(
                    model_id=_MODEL,
                    messages=[{"role": "user", "content": "hi"}],
                )
            )


# ---------------------------------------------------------------------------
# Construction + latency
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_zero_max_retries(self) -> None:
        with pytest.raises(ValueError, match="max_retries"):
            OpenRouterClient(_API_KEY, max_retries=0)

    def test_trailing_slash_in_base_url_is_normalised(self) -> None:
        handler = _RecordingHandler(
            responses=[httpx.Response(200, json=_COMPLETE_FIXTURE)]
        )
        transport = httpx.MockTransport(handler)
        http = httpx.Client(transport=transport)
        client = OpenRouterClient(
            _API_KEY,
            base_url="https://openrouter.ai/api/v1/",  # trailing slash
            http=http,
            clock=FrozenClock(datetime(2026, 4, 20, 9, 0, tzinfo=UTC)),
            sleep=lambda _s: None,
        )

        client.complete(model_id=_MODEL, prompt="hi")

        assert str(handler.requests[0].url) == (
            "https://openrouter.ai/api/v1/chat/completions"
        )


class TestLatencyMeasurement:
    def test_clock_is_consulted_around_the_request(self) -> None:
        """The adapter advances the clock twice per call (start + end).

        We don't assert on the exact duration — the adapter logs it
        but does not surface it through :class:`LLMResponse` per the
        port contract. What matters is that requesting a completion
        does not explode when ``clock.now()`` returns two distinct
        instants.
        """

        class _AdvancingClock:
            def __init__(self) -> None:
                self._t = datetime(2026, 4, 20, 9, 0, tzinfo=UTC)
                self.calls = 0

            def now(self) -> datetime:
                self.calls += 1
                self._t = self._t + timedelta(milliseconds=250)
                return self._t

        clock = _AdvancingClock()
        handler = _RecordingHandler(
            responses=[httpx.Response(200, json=_COMPLETE_FIXTURE)]
        )
        transport = httpx.MockTransport(handler)
        http = httpx.Client(transport=transport)
        client = OpenRouterClient(
            _API_KEY,
            http=http,
            clock=clock,
            sleep=lambda _s: None,
        )

        client.complete(model_id=_MODEL, prompt="hi")
        assert clock.calls >= 2
