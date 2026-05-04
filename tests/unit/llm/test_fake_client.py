"""Unit coverage for :mod:`app.adapters.llm.fake` (cd-tblly).

The deterministic fake is now production-importable so the FastAPI
factory can wire it under ``CREWDAY_LLM_PROVIDER=fake`` (the dev /
Playwright stacks). These tests
exercise the two seams the e2e journey relies on:

* ``chat`` returns the canned high-confidence OCR autofill JSON when
  the user message carries the receipt-extraction prompt prefix the
  domain layer ships.
* ``ocr`` returns deterministic OCR text (no
  :class:`LLMCapabilityMissing`); the legacy
  :class:`EchoLLMClient` shim still raises it so the
  "this client can't OCR" branch stays under test.
"""

from __future__ import annotations

import json

import pytest

from app.adapters.llm.fake import _OCR_PROMPT_MARKER, FakeLLMClient
from app.adapters.llm.ports import ChatMessage, LLMCapabilityMissing, Tool, ToolCall
from app.domain.expenses.autofill import _OCR_TO_JSON_PROMPT
from tests._fakes.llm import EchoLLMClient


def test_chat_returns_high_confidence_payload_for_ocr_prompt() -> None:
    """The fake mirrors the high-confidence shape the domain expects."""
    client = FakeLLMClient()

    ocr_text = "Vendor: Bistro 42\nTotal: 27.50 EUR"
    resp = client.chat(
        model_id="fake-ocr",
        messages=[
            {
                "role": "user",
                "content": f"{_OCR_TO_JSON_PROMPT}\n\n{ocr_text}",
            }
        ],
    )

    payload = json.loads(resp.text)
    assert payload["vendor"] == "Bistro 42"
    assert payload["amount"] == "27.50"
    assert payload["currency"] == "EUR"
    assert payload["category"] == "food"
    assert payload["purchased_at"] == "2026-04-17T12:30:00+00:00"
    # Every per-field confidence must clear the autofill threshold so
    # the first-attach autofill rule fires under the e2e stack.
    for field in ("vendor", "amount", "currency", "purchased_at", "category"):
        assert payload["confidence"][field] >= 0.9


def test_chat_is_deterministic_across_calls() -> None:
    client = FakeLLMClient()
    messages: list[ChatMessage] = [
        {"role": "user", "content": f"{_OCR_TO_JSON_PROMPT}\n\nany ocr"},
    ]

    first = client.chat(model_id="fake-ocr", messages=messages)
    second = client.chat(model_id="fake-ocr", messages=messages)

    assert first.text == second.text


def test_chat_falls_back_to_echo_for_unrelated_prompts() -> None:
    """Non-OCR prompts keep the historical echo behaviour."""
    client = FakeLLMClient()

    resp = client.chat(
        model_id="fake/model",
        messages=[
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hello"},
        ],
    )

    assert resp.text == "hello"


def test_chat_supplies_pre_canned_tool_calls() -> None:
    canned = (
        ToolCall(id="call_1", name="tasks.list", arguments={"property_id": "p1"}),
    )
    client = FakeLLMClient(tool_calls=canned)

    resp = client.chat(
        model_id="fake/model",
        messages=[{"role": "user", "content": "list tasks"}],
    )

    assert resp.tool_calls == canned


def test_ocr_returns_deterministic_text() -> None:
    client = FakeLLMClient()

    text = client.ocr(model_id="fake-ocr", image_bytes=b"any-bytes")

    assert "Bistro 42" in text
    assert text == client.ocr(model_id="fake-ocr", image_bytes=b"different-bytes")


def test_custom_ocr_payload_overrides_default() -> None:
    overridden: dict[str, object] = {
        "vendor": "Cafe Test",
        "amount": "10.00",
        "currency": "USD",
        "purchased_at": "2026-01-01T00:00:00+00:00",
        "category": "supplies",
        "confidence": {
            "vendor": 0.99,
            "amount": 0.99,
            "currency": 0.99,
            "purchased_at": 0.99,
            "category": 0.99,
        },
    }
    client = FakeLLMClient(ocr_payload=overridden)

    resp = client.chat(
        model_id="fake-ocr",
        messages=[
            {"role": "user", "content": f"{_OCR_TO_JSON_PROMPT}\n\nany"},
        ],
    )

    assert json.loads(resp.text)["vendor"] == "Cafe Test"


def test_echo_llm_client_subclass_still_refuses_ocr() -> None:
    """Legacy unit-test contract: ``EchoLLMClient.ocr`` raises."""
    client = EchoLLMClient()

    with pytest.raises(LLMCapabilityMissing) as excinfo:
        client.ocr(model_id="x", image_bytes=b"png")

    assert excinfo.value.capability == "ocr"


def test_complete_echoes_prompt() -> None:
    client = FakeLLMClient()

    resp = client.complete(model_id="fake/model", prompt="ping")

    assert resp.text == "ping"
    expected_total = resp.usage.prompt_tokens + resp.usage.completion_tokens
    assert resp.usage.total_tokens == expected_total


def test_stream_chat_yields_whitespace_split_tokens() -> None:
    client = FakeLLMClient()

    tokens = list(
        client.stream_chat(
            model_id="fake/model",
            messages=[{"role": "user", "content": "one two three"}],
        )
    )

    assert tokens == ["one", "two", "three"]


def test_ocr_prompt_marker_is_substring_of_canonical_prompt() -> None:
    """Drift guard: the fake's marker must remain a substring of
    :data:`app.domain.expenses.autofill._OCR_TO_JSON_PROMPT`.

    The fake's ``chat`` dispatch is content-aware on the user message
    (cd-tblly). Importing the prompt directly from the domain layer
    would invert the dependency direction (adapter→domain), so the
    fake mirrors the literal — and this test owns the drift contract.
    A future prompt rewrite that drops the leading sentence fails
    here instead of silently disabling the e2e autofill seam.
    """
    assert _OCR_PROMPT_MARKER in _OCR_TO_JSON_PROMPT


def test_chat_accepts_tools_argument() -> None:
    """Protocol contract: ``chat`` must accept a ``tools=`` kwarg.

    The fake doesn't dispatch on the tool list (it returns its
    pre-canned ``tool_calls`` tuple regardless), but rejecting the
    kwarg would break callers that hand it down — the agent runtime
    routes every chat through this seam. Pin the surface so a future
    refactor that drops the parameter fails here.
    """
    canned = (ToolCall(id="call_2", name="employees.list", arguments={}),)
    client = FakeLLMClient(tool_calls=canned)
    tools: list[Tool] = [
        {
            "name": "employees.list",
            "description": "List employees in the workspace.",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]

    resp = client.chat(
        model_id="fake/model",
        messages=[{"role": "user", "content": "list employees"}],
        tools=tools,
    )

    # Pre-canned tool calls flow through verbatim regardless of
    # whether ``tools`` is passed.
    assert resp.tool_calls == canned


def test_instances_do_not_share_payload_state() -> None:
    """Idempotence: mutating one instance must not bleed into the next.

    The constructor deep-copies the OCR payload so the nested
    ``confidence`` map on instance A is a separate object from the
    map on instance B (and from the module-level default). Reaching
    into ``_ocr_payload`` to tweak a confidence in a test path was
    silently corrupting every subsequent ``FakeLLMClient()`` before
    the deep-copy fix.
    """
    a = FakeLLMClient()
    confidence_a = a._ocr_payload["confidence"]
    assert isinstance(confidence_a, dict)
    confidence_a["vendor"] = 0.10  # simulate a test mutation

    b = FakeLLMClient()
    confidence_b = b._ocr_payload["confidence"]
    assert isinstance(confidence_b, dict)
    assert confidence_b["vendor"] == 0.95
