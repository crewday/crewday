"""Unit tests for :func:`app.authz.approval_mint.mint_approval_request` (cd-qo3g).

Covers the seam that mints an :class:`~app.adapters.db.llm.models.ApprovalRequest`
row when :func:`app.authz.require` raises :class:`ApprovalRequired`
on a direct-human request. The helper is the only writer for the
direct-human variant of the row; the tests pin its observable shape:

* ``status='pending'`` and ``workspace_id`` / ``requester_actor_id``
  pulled from the ``WorkspaceContext``.
* Direct-human marker fields (``inline_channel``, ``for_user_id``,
  ``resolved_user_mode``, ``expires_at``) are NULL.
* ``action_json`` carries the four resolver fields plus the optional
  ``method`` / ``path`` shape when supplied — and never carries the
  agent-runtime keys that the consumer pipeline uses to replay tool
  calls.
* One ``audit.approval.requested`` row is written, attributed to the
  caller's ``actor_id``.
* The helper does not commit (UoW invariant).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.llm.models import ApprovalRequest
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.authz.approval_mint import mint_approval_request
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def _seed(session: Session) -> tuple[str, str]:
    """Insert a workspace + one user; return ``(workspace_id, user_id)``."""
    workspace_id = new_ulid()
    user_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug="test-ws",
            name="Test Workspace",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    email = "actor@example.com"
    session.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name="Actor",
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id, user_id


def _ctx(*, workspace_id: str, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="test-ws",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


class TestMintShape:
    """Shape of the inserted :class:`ApprovalRequest` row."""

    def test_returns_pending_row(self, factory: sessionmaker[Session]) -> None:
        with factory() as s:
            workspace_id, user_id = _seed(s)
            s.flush()
            ctx = _ctx(workspace_id=workspace_id, actor_id=user_id)
            row = mint_approval_request(
                s,
                ctx,
                action_key="tasks.create",
                scope_kind="property",
                scope_id="01HWA00000000000000000PR01",
                clock=FrozenClock(_PINNED),
            )
            assert row.id
            assert row.workspace_id == workspace_id
            assert row.requester_actor_id == user_id
            assert row.status == "pending"
            assert row.created_at == _PINNED

    def test_direct_human_marker_fields_are_null(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Direct-human rows leave the agent-runtime columns NULL.

        The §11 LLM consumer pipeline reads ``inline_channel`` /
        ``for_user_id`` / ``resolved_user_mode`` to replay tool calls —
        keeping them NULL on direct-human rows is the marker that opts
        the row out of that path.
        """
        with factory() as s:
            workspace_id, user_id = _seed(s)
            s.flush()
            ctx = _ctx(workspace_id=workspace_id, actor_id=user_id)
            row = mint_approval_request(
                s,
                ctx,
                action_key="tasks.create",
                scope_kind="workspace",
                scope_id=workspace_id,
                clock=FrozenClock(_PINNED),
            )
            assert row.inline_channel is None
            assert row.for_user_id is None
            assert row.resolved_user_mode is None
            assert row.expires_at is None
            assert row.decided_by is None
            assert row.decided_at is None
            assert row.rationale_md is None
            assert row.result_json is None
            assert row.decision_note_md is None

    def test_action_json_carries_resolver_fields(
        self, factory: sessionmaker[Session]
    ) -> None:
        with factory() as s:
            workspace_id, user_id = _seed(s)
            s.flush()
            ctx = _ctx(workspace_id=workspace_id, actor_id=user_id)
            row = mint_approval_request(
                s,
                ctx,
                action_key="tasks.create",
                scope_kind="property",
                scope_id="01HWA00000000000000000PR01",
                clock=FrozenClock(_PINNED),
            )
            assert row.action_json == {
                "action_key": "tasks.create",
                "scope_kind": "property",
                "scope_id": "01HWA00000000000000000PR01",
                "actor_id": user_id,
            }

    def test_action_json_includes_method_and_path_when_supplied(
        self, factory: sessionmaker[Session]
    ) -> None:
        with factory() as s:
            workspace_id, user_id = _seed(s)
            s.flush()
            ctx = _ctx(workspace_id=workspace_id, actor_id=user_id)
            row = mint_approval_request(
                s,
                ctx,
                action_key="tasks.create",
                scope_kind="workspace",
                scope_id=workspace_id,
                method="POST",
                path="/w/test-ws/api/v1/tasks",
                clock=FrozenClock(_PINNED),
            )
            assert row.action_json["method"] == "POST"
            assert row.action_json["path"] == "/w/test-ws/api/v1/tasks"

    def test_action_json_omits_agent_runtime_keys(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Direct-human rows must not carry ``tool_*`` keys.

        :mod:`app.domain.agent.approval` reads ``tool_name`` /
        ``tool_call_id`` / ``tool_input`` to replay tool calls. Leaving
        them out of direct-human rows is what keeps the consumer
        pipeline from accidentally replaying a non-agent decision.
        """
        with factory() as s:
            workspace_id, user_id = _seed(s)
            s.flush()
            ctx = _ctx(workspace_id=workspace_id, actor_id=user_id)
            row = mint_approval_request(
                s,
                ctx,
                action_key="tasks.create",
                scope_kind="workspace",
                scope_id=workspace_id,
                clock=FrozenClock(_PINNED),
            )
            for forbidden in ("tool_name", "tool_call_id", "tool_input"):
                assert forbidden not in row.action_json


class TestAuditTrail:
    """One ``audit.approval.requested`` row attributes the mint."""

    def test_audit_row_written(self, factory: sessionmaker[Session]) -> None:
        with factory() as s:
            workspace_id, user_id = _seed(s)
            s.flush()
            ctx = _ctx(workspace_id=workspace_id, actor_id=user_id)
            row = mint_approval_request(
                s,
                ctx,
                action_key="tasks.create",
                scope_kind="workspace",
                scope_id=workspace_id,
                method="POST",
                path="/w/test-ws/api/v1/tasks",
                clock=FrozenClock(_PINNED),
            )
            s.flush()
            audit_rows = list(
                s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_kind == "approval_request",
                        AuditLog.entity_id == row.id,
                    )
                ).scalars()
            )
            assert len(audit_rows) == 1
            audit = audit_rows[0]
            assert audit.action == "approval.requested"
            assert audit.actor_id == user_id
            assert audit.workspace_id == workspace_id
            assert audit.diff["approval_request_id"] == row.id
            assert audit.diff["action_key"] == "tasks.create"
            assert audit.diff["scope_kind"] == "workspace"
            assert audit.diff["scope_id"] == workspace_id
            assert audit.diff["method"] == "POST"
            assert audit.diff["path"] == "/w/test-ws/api/v1/tasks"


class TestCommitInvariant:
    """Helper does not commit — only flushes."""

    def test_caller_owns_transaction(self, factory: sessionmaker[Session]) -> None:
        """Roll back after the mint and the row must vanish."""
        with factory() as s:
            workspace_id, user_id = _seed(s)
            s.commit()

        with factory() as s:
            ctx = _ctx(workspace_id=workspace_id, actor_id=user_id)
            row = mint_approval_request(
                s,
                ctx,
                action_key="tasks.create",
                scope_kind="workspace",
                scope_id=workspace_id,
                clock=FrozenClock(_PINNED),
            )
            row_id = row.id
            s.rollback()

        with factory() as s:
            assert s.get(ApprovalRequest, row_id) is None
