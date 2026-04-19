"""Smoke test for the ``messaging`` unit fixtures.

Proves the shared in-memory fakes wire into this context's
``conftest.py``. Real tests land as messaging's domain code grows.
"""

from __future__ import annotations

from app.adapters.llm.ports import LLMResponse
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
