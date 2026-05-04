"""Re-export shim for the production deterministic LLM fake.

The deterministic :class:`~app.adapters.llm.ports.LLMClient` fake now
lives under :mod:`app.adapters.llm.fake` so the FastAPI factory can
import it when ``CREWDAY_LLM_PROVIDER=fake`` (e.g. the dev / Playwright
stack — see ``mocks/docker-compose.yml`` and
``mocks/docker-compose.e2e.yml``). Tests historically
imported :class:`EchoLLMClient` from ``tests._fakes.llm``; this shim
preserves that import path so existing fixtures keep working.

See ``docs/specs/11-llm-and-agents.md`` §"Provider types" and
``docs/specs/17-testing-quality.md`` §"Unit".
"""

from __future__ import annotations

from app.adapters.llm.fake import EchoLLMClient, FakeLLMClient

__all__ = ["EchoLLMClient", "FakeLLMClient"]
