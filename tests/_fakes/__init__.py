"""Shared in-memory fakes for adapter ports.

Every side-effect seam under ``app.adapters.<name>.ports`` gets an
in-memory test double here. Per-context ``conftest.py`` fixtures wire
these into each unit test package so domain tests never touch the
filesystem, the network, or the database.

See ``docs/specs/17-testing-quality.md`` §"Unit" for the contract:
``Clock``, ``Storage``, ``Mailer``, and ``LLMClient`` must each have a
fake that satisfies its port structurally (``mypy --strict`` clean).

The fakes deliberately live **outside** ``tests/unit`` so integration,
API, and LLM-regression tests can import them too without crossing a
context boundary.
"""

from __future__ import annotations

from app.util.clock import FrozenClock
from tests._fakes.llm import EchoLLMClient
from tests._fakes.mailer import InMemoryMailer, SentMessage
from tests._fakes.storage import InMemoryStorage

__all__ = [
    "EchoLLMClient",
    "FrozenClock",
    "InMemoryMailer",
    "InMemoryStorage",
    "SentMessage",
]
