"""Context-local fixtures for the ``places`` unit tests.

See ``docs/specs/17-testing-quality.md`` §"Unit". Provides the
in-memory fakes for adapter ports used by this context's domain tests.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.util.clock import FrozenClock
from tests._fakes.llm import EchoLLMClient
from tests._fakes.mailer import InMemoryMailer
from tests._fakes.storage import InMemoryStorage


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def mailer() -> InMemoryMailer:
    return InMemoryMailer()


@pytest.fixture
def llm() -> EchoLLMClient:
    return EchoLLMClient()
