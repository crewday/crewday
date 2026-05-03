"""Deterministic in-process :class:`~app.adapters.llm.ports.LLMClient`.

Production-importable fake used by:

* the FastAPI factory when ``CREWDAY_LLM_PROVIDER=fake`` (e.g. the
  ``mocks/docker-compose.e2e.yml`` Playwright stack), and
* unit tests, via the :class:`EchoLLMClient` re-export under
  :mod:`tests._fakes.llm`.

Two posture decisions worth flagging before reading further:

* **No network.** Every method returns a canned, content-aware result.
  The fake is safe to wire into any environment.
* **Content-aware ``chat``.** When the user message contains the
  receipt-OCR prompt prefix the domain layer ships
  (:data:`app.domain.expenses.autofill._OCR_TO_JSON_PROMPT`), the fake
  returns the high-confidence autofill payload that mirrors the
  integration shape under
  :func:`tests.integration.test_receipt_ocr_job._high_confidence_payload`.
  Every other prompt falls back to the historical "echo the last
  message" behaviour so unrelated test paths keep working.

See ``docs/specs/11-llm-and-agents.md`` §"Provider types" (the
``fake`` provider type), ``docs/specs/16-deployment-operations.md``
§"Environment variables" (the ``CREWDAY_LLM_PROVIDER`` knob), and
``docs/specs/17-testing-quality.md`` §"Unit".
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator, Sequence
from typing import Final

from app.adapters.llm.ports import (
    ChatMessage,
    LLMCapabilityMissing,
    LLMResponse,
    LLMUsage,
    Tool,
    ToolCall,
)
from app.util.redact import ConsentSet

__all__ = [
    "EchoLLMClient",
    "FakeLLMClient",
]


# Sentinel that flags an OCR-to-JSON prompt. Kept as a plain substring
# match against the user message because the domain layer concatenates
# the prompt with the OCR text: ``f"{_OCR_TO_JSON_PROMPT}\n\n{ocr_text}"``.
# Mirroring the literal here avoids a back-import from domain into the
# adapter (the dependency direction is adapter→ports, not adapter→domain).
#
# Drift guard: ``tests/unit/llm/test_fake_client.py`` imports
# :data:`app.domain.expenses.autofill._OCR_TO_JSON_PROMPT` and asserts
# this marker is a substring of it, so a future prompt rewrite that
# drops the leading sentence fails the test instead of silently
# disabling the e2e autofill seam.
_OCR_PROMPT_MARKER: Final[str] = "You are a receipt-extraction tool."

# Default high-confidence autofill payload. Mirrors the shape of
# ``tests.integration.test_receipt_ocr_job._high_confidence_payload``;
# values clear ``AUTOFILL_CONFIDENCE_THRESHOLD`` so the first-attach
# autofill rule fires under the e2e stack.
_DEFAULT_OCR_PAYLOAD: Final[dict[str, object]] = {
    "vendor": "Bistro 42",
    "amount": "27.50",
    "currency": "EUR",
    "purchased_at": "2026-04-17T12:30:00+00:00",
    "category": "food",
    "confidence": {
        "vendor": 0.95,
        "amount": 0.95,
        "currency": 0.95,
        "purchased_at": 0.95,
        "category": 0.95,
    },
}

# Canned OCR text returned by :meth:`FakeLLMClient.ocr`. The downstream
# JSON parser ignores the body (the fake's ``chat`` already returns the
# structured payload), but a non-empty string keeps the contract honest
# and lets callers log / hash / diff the OCR step like a real provider.
_DEFAULT_OCR_TEXT: Final[str] = "Vendor: Bistro 42\nTotal: 27.50 EUR\n2026-04-17"


class FakeLLMClient:
    """Deterministic LLM client backed by canned responses.

    * :meth:`complete` returns the prompt verbatim.
    * :meth:`chat` returns the canned OCR-autofill JSON when the last
      message looks like the receipt-extraction prompt; otherwise it
      echoes the last message text so unrelated callers keep their
      historical behaviour.
    * :meth:`ocr` returns deterministic OCR text (no
      :class:`LLMCapabilityMissing` — the fake is the e2e seam for the
      OCR pipeline, so refusing the capability would defeat the purpose).
    * :meth:`stream_chat` yields the whitespace-split last message.

    A pre-canned ``tool_calls`` tuple may be supplied at construction;
    when set, every :meth:`chat` invocation attaches the same tuple.
    A custom ``ocr_payload`` overrides the canned high-confidence
    autofill output for tests that exercise edge shapes (low
    confidence, alternate currency, …).
    """

    def __init__(
        self,
        *,
        tool_calls: tuple[ToolCall, ...] = (),
        ocr_payload: dict[str, object] | None = None,
        ocr_text: str = _DEFAULT_OCR_TEXT,
    ) -> None:
        self._tool_calls = tool_calls
        # Deep-copy so a caller that reaches into ``self._ocr_payload``
        # (or its nested ``confidence`` map) doesn't mutate the
        # module-level default and contaminate every subsequent
        # ``FakeLLMClient()`` instance. The fake is contractually
        # stateless across instances; a shallow copy of the outer dict
        # would still alias the inner ``confidence`` dict.
        self._ocr_payload: dict[str, object] = copy.deepcopy(
            ocr_payload if ocr_payload is not None else _DEFAULT_OCR_PAYLOAD
        )
        self._ocr_text = ocr_text

    def complete(
        self,
        *,
        model_id: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        consents: ConsentSet | None = None,
    ) -> LLMResponse:
        del consents  # in-process fake never reaches an upstream provider
        return LLMResponse(
            text=prompt,
            usage=LLMUsage(
                prompt_tokens=len(prompt),
                completion_tokens=len(prompt),
                total_tokens=2 * len(prompt),
            ),
            model_id=model_id,
            finish_reason="stop",
        )

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tools: Sequence[Tool] | None = None,
        consents: ConsentSet | None = None,
    ) -> LLMResponse:
        del consents  # in-process fake never reaches an upstream provider
        last = messages[-1]["content"] if messages else ""
        text = json.dumps(self._ocr_payload) if _OCR_PROMPT_MARKER in last else last
        return LLMResponse(
            text=text,
            usage=LLMUsage(
                prompt_tokens=len(last),
                completion_tokens=len(text),
                total_tokens=len(last) + len(text),
            ),
            model_id=model_id,
            finish_reason="stop",
            tool_calls=self._tool_calls,
        )

    def ocr(
        self,
        *,
        model_id: str,
        image_bytes: bytes,
        consents: ConsentSet | None = None,
    ) -> str:
        del consents  # in-process fake never reaches an upstream provider
        return self._ocr_text

    def stream_chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tools: Sequence[Tool] | None = None,
        consents: ConsentSet | None = None,
    ) -> Iterator[str]:
        del consents  # in-process fake never reaches an upstream provider
        last = messages[-1]["content"] if messages else ""
        yield from last.split()


class EchoLLMClient(FakeLLMClient):
    """Pure-echo LLM stub kept for unit-test ergonomics.

    The unit-test surface predates the production fake and relies on
    :meth:`ocr` raising :class:`LLMCapabilityMissing` so the
    "this client can't OCR" branch stays under test. We model that
    contract as a thin subclass: the chat / complete / stream paths
    inherit the deterministic echo behaviour, and :meth:`ocr` reverts
    to the capability-missing semantics.

    Use :class:`FakeLLMClient` for any caller that wants the OCR
    capability lit up (the e2e Playwright stack, the receipt-autofill
    integration test).
    """

    def ocr(
        self,
        *,
        model_id: str,
        image_bytes: bytes,
        consents: ConsentSet | None = None,
    ) -> str:
        del consents  # in-process fake never reaches an upstream provider
        raise LLMCapabilityMissing("ocr")
