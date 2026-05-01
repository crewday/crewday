"""Smoke test for the ``llm`` unit fixtures.

Proves the shared in-memory fakes wire into this context's
``conftest.py``. Real tests land as llm's domain code grows.
"""

from __future__ import annotations

from app.adapters.llm.ports import LLMResponse, ToolCall
from app.util.clock import FrozenClock
from tests._fakes.llm import EchoLLMClient
from tests._fakes.mailer import InMemoryMailer
from tests._fakes.storage import InMemoryStorage


def test_clock_fixture_is_frozen(clock: FrozenClock) -> None:
    assert clock.now().year == 2026


def test_storage_fixture_empty(storage: InMemoryStorage) -> None:
    assert storage.exists("does-not-exist") is False


def test_mailer_fixture_empty(mailer: InMemoryMailer) -> None:
    assert mailer.sent == []


def test_llm_fixture_returns_response(llm: EchoLLMClient) -> None:
    r = llm.complete(model_id="x", prompt="hello")
    assert isinstance(r, LLMResponse)
    assert r.text == "hello"


def test_echo_llm_client_surfaces_pre_canned_tool_calls() -> None:
    """``EchoLLMClient`` returns the canned ``tool_calls`` tuple unchanged."""
    canned = (
        ToolCall(id="call_1", name="tasks.list", arguments={"property_id": "p1"}),
    )
    client = EchoLLMClient(tool_calls=canned)

    resp = client.chat(
        model_id="fake/model",
        messages=[{"role": "user", "content": "list tasks"}],
    )

    assert resp.tool_calls == canned


def test_echo_llm_client_default_tool_calls_empty() -> None:
    client = EchoLLMClient()
    resp = client.chat(
        model_id="fake/model",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert resp.tool_calls == ()
