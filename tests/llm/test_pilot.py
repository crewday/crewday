"""Pilot LLM-regression test.

Uses the shared :class:`~tests._fakes.llm.EchoLLMClient` to prove
the harness wires into ``tests/llm/``. Real regression suites
(receipts, intake, digests) land in follow-ups per
``docs/specs/17-testing-quality.md`` §"LLM regression".
"""

from __future__ import annotations

from app.adapters.llm.ports import LLMResponse
from tests._fakes.llm import EchoLLMClient


def test_echo_llm_complete_returns_response() -> None:
    client = EchoLLMClient()
    resp = client.complete(model_id="google/gemma-3-12b", prompt="ping")
    assert isinstance(resp, LLMResponse)
    assert resp.text == "ping"
    assert resp.finish_reason == "stop"
