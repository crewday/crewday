"""End-to-end round trip for :class:`app.domain.llm.client.LLMClient` (cd-weue).

Pins the wrapper against the **real** OpenRouter adapter and the
**real** ORM tenancy filter, with the upstream HTTP layer
intercepted via :class:`httpx.MockTransport`. The unit suite under
``tests/unit/domain/llm/test_client.py`` covers behaviour against a
hand-rolled stub adapter; this file proves the wrapper's adapter
interface stays the same as :class:`OpenRouterClient` (the only
production adapter today) and that a 5xx → 200 fallback writes the
two ``llm_usage`` rows we expect under the workspace-scoped tenancy
filter.

Verified here:

* End-to-end success: the wrapper resolves an assignment, the
  adapter posts to ``/chat/completions``, the recorder writes one
  ``status="ok"`` row through the tenancy filter.
* Fallback walk: a 502 on the first rung advances to the second;
  the second rung's 200 returns. Two rows on disk, the success row
  carries ``fallback_attempts=1``.

See ``docs/specs/11-llm-and-agents.md`` §"Client abstraction",
§"Retryable errors".
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import (
    BudgetLedger,
    LlmAssignment,
    LlmModel,
    LlmProvider,
    LlmProviderModel,
)
from app.adapters.db.llm.models import LlmUsage as LlmUsageRow
from app.adapters.db.workspace.models import Workspace
from app.adapters.llm.openrouter import OpenRouterClient
from app.domain.llm.budget import WINDOW_DAYS
from app.domain.llm.client import LLMClient, LLMResult
from app.domain.llm.usage_recorder import AgentAttribution
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from app.util.redact import ConsentSet
from app.util.ulid import new_ulid

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
_CAPABILITY = "chat.manager"
_API_KEY = SecretStr("sk-or-test-roundtrip")


# ---------------------------------------------------------------------------
# Canned wire shapes
# ---------------------------------------------------------------------------


def _completion_body(*, model: str, text: str = "ok") -> dict[str, object]:
    """Minimal OpenAI-compatible completion body the parser accepts."""
    return {
        "id": f"gen-{model}",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "total_tokens": 16,
        },
    }


class _ScriptedHandler:
    """Pop one scripted :class:`httpx.Response` per request.

    Re-raises ``AssertionError`` if the script is exhausted so a
    test that under-provisions fails loudly instead of looping.
    """

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses: list[httpx.Response] = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("scripted handler exhausted")
        return self._responses.pop(0)


def _make_adapter(handler: _ScriptedHandler) -> OpenRouterClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return OpenRouterClient(
        _API_KEY,
        max_retries=1,  # retry budget is owned by the wrapper, not the adapter
        http=http,
        clock=FrozenClock(_PINNED),
        sleep=lambda _s: None,
    )


# ---------------------------------------------------------------------------
# Seed helpers — kept terse
# ---------------------------------------------------------------------------


def _seed_workspace(session: Session, *, slug: str) -> Workspace:
    ws = Workspace(
        id=new_ulid(),
        slug=slug,
        name=slug,
        plan="free",
        quota_json={},
        created_at=_PINNED,
    )
    with tenant_agnostic():
        session.add(ws)
        session.flush()
    return ws


def _seed_provider_model(
    session: Session,
    *,
    api_model_id: str,
) -> LlmProviderModel:
    provider = LlmProvider(
        id=new_ulid(),
        name=f"prov-{api_model_id.replace('/', '-')}",
        provider_type="fake",
        timeout_s=60,
        requests_per_minute=60,
        priority=0,
        is_enabled=True,
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    model = LlmModel(
        id=new_ulid(),
        canonical_name=f"canonical/{api_model_id}",
        display_name=api_model_id,
        vendor="other",
        capabilities=["chat"],
        is_active=True,
        price_source="",
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add_all([provider, model])
    session.flush()
    pm = LlmProviderModel(
        id=new_ulid(),
        provider_id=provider.id,
        model_id=model.id,
        api_model_id=api_model_id,
        supports_system_prompt=True,
        supports_temperature=True,
        is_enabled=True,
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add(pm)
    session.flush()
    return pm


def _seed_assignment(
    session: Session,
    *,
    workspace_id: str,
    capability: str,
    api_model_id: str,
    priority: int = 0,
) -> LlmAssignment:
    pm = _seed_provider_model(session, api_model_id=api_model_id)
    row = LlmAssignment(
        id=new_ulid(),
        workspace_id=workspace_id,
        capability=capability,
        model_id=pm.id,
        provider="openrouter",
        priority=priority,
        enabled=True,
        max_tokens=None,
        temperature=None,
        extra_api_params={},
        required_capabilities=[],
        created_at=_PINNED,
    )
    session.add(row)
    session.flush()
    return row


def _seed_ledger(
    session: Session,
    *,
    workspace_id: str,
    cap_cents: int,
    spent_cents: int = 0,
) -> BudgetLedger:
    row = BudgetLedger(
        id=new_ulid(),
        workspace_id=workspace_id,
        period_start=_PINNED - timedelta(days=WINDOW_DAYS),
        period_end=_PINNED,
        spent_cents=spent_cents,
        cap_cents=cap_cents,
        updated_at=_PINNED,
    )
    session.add(row)
    session.flush()
    return row


def _build_context(workspace: Workspace) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=new_ulid(),
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )


def _attribution() -> AgentAttribution:
    return AgentAttribution(
        actor_user_id=new_ulid(),
        token_id=None,
        agent_label="manager-chat",
    )


def _fetch_rows(session: Session, *, workspace_id: str) -> list[LlmUsageRow]:
    with tenant_agnostic():
        return list(
            session.execute(
                select(LlmUsageRow)
                .where(LlmUsageRow.workspace_id == workspace_id)
                .order_by(LlmUsageRow.attempt.asc())
            )
            .scalars()
            .all()
        )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRoundTripAgainstOpenRouter:
    """Wrapper + real adapter + scripted upstream HTTP."""

    def test_first_rung_success_writes_one_ok_row(self, db_session: Session) -> None:
        ws = _seed_workspace(db_session, slug=f"ws-{new_ulid().lower()[:8]}")
        ctx = _build_context(ws)
        _seed_assignment(
            db_session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="primary/model",
        )
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)

        handler = _ScriptedHandler(
            [
                httpx.Response(
                    200,
                    json=_completion_body(model="primary/model", text="hi"),
                ),
            ]
        )
        client = LLMClient(_make_adapter(handler))

        token = set_current(ctx)
        try:
            result = _run(
                client.chat(
                    db_session,
                    ctx,
                    capability=_CAPABILITY,
                    messages=[{"role": "user", "content": "ping"}],
                    attribution=_attribution(),
                    consents=ConsentSet.none(),
                    clock=FrozenClock(_PINNED),
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        assert isinstance(result, LLMResult)
        assert result.text == "hi"
        assert result.fallback_attempts == 0
        assert result.model_used == "primary/model"
        assert result.correlation_id == ctx.audit_correlation_id

        rows = _fetch_rows(db_session, workspace_id=ws.id)
        assert len(rows) == 1
        assert rows[0].status == "ok"
        assert rows[0].fallback_attempts == 0
        assert rows[0].correlation_id == ctx.audit_correlation_id

        # Adapter posted exactly once with the wire-form model id.
        assert len(handler.requests) == 1
        body = handler.requests[0].content.decode("utf-8")
        assert '"model": "primary/model"' in body or '"model":"primary/model"' in body

    def test_fallback_walk_on_5xx_records_two_rows(self, db_session: Session) -> None:
        """Primary returns 502; secondary returns 200.

        Two rows land — the failing rung carries ``status="error"``
        and the succeeding one carries ``status="ok"`` with
        ``fallback_attempts=1``. Both share the request-scoped
        ``correlation_id`` so the /admin/usage feed groups them.
        """
        ws = _seed_workspace(db_session, slug=f"ws-{new_ulid().lower()[:8]}")
        ctx = _build_context(ws)
        _seed_assignment(
            db_session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="primary/model",
            priority=0,
        )
        _seed_assignment(
            db_session,
            workspace_id=ws.id,
            capability=_CAPABILITY,
            api_model_id="secondary/model",
            priority=1,
        )
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)

        handler = _ScriptedHandler(
            [
                # Primary rung: 502 — the OpenRouter adapter exhausts
                # its single internal retry (max_retries=1) and raises
                # LlmTransportError.
                httpx.Response(502, json={"error": "bad gateway"}),
                # Secondary rung: 200.
                httpx.Response(
                    200,
                    json=_completion_body(model="secondary/model", text="recovered"),
                ),
            ]
        )
        client = LLMClient(_make_adapter(handler))

        token = set_current(ctx)
        try:
            result = _run(
                client.chat(
                    db_session,
                    ctx,
                    capability=_CAPABILITY,
                    messages=[{"role": "user", "content": "ping"}],
                    attribution=_attribution(),
                    consents=ConsentSet.none(),
                    clock=FrozenClock(_PINNED),
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        assert result.text == "recovered"
        assert result.fallback_attempts == 1
        assert result.model_used == "secondary/model"

        rows = _fetch_rows(db_session, workspace_id=ws.id)
        assert len(rows) == 2
        assert rows[0].status == "error"
        assert rows[0].cost_cents == 0
        assert rows[0].fallback_attempts == 0
        assert rows[1].status == "ok"
        assert rows[1].fallback_attempts == 1

        # Two posts to the upstream URL — one per attempted rung.
        assert len(handler.requests) == 2
