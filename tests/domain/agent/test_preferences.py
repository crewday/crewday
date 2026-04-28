"""Tests for agent preference storage and runtime injection."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.llm.models import AgentPreferenceRevision
from app.adapters.db.workspace.models import Workspace
from app.domain.agent.preferences import (
    PreferenceContainsSecret,
    PreferenceTooLarge,
    PreferenceUpdate,
    blocked_action_result_body,
    default_approval_mode_for_workspace,
    is_action_blocked,
    resolve_preferences,
    save_preference,
)
from app.tenancy import WorkspaceContext
from app.tenancy.current import set_current
from app.util.clock import FrozenClock
from tests.domain.agent.conftest import build_context, seed_user, seed_workspace


def _bind_workspace(db_session: Session) -> tuple[Workspace, WorkspaceContext, str]:
    workspace = seed_workspace(db_session)
    user_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=user_id)
    set_current(ctx)
    return workspace, ctx, user_id


def test_workspace_preference_round_trips_with_revision_and_audit(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    _workspace, ctx, user_id = _bind_workspace(db_session)

    row = save_preference(
        db_session,
        ctx,
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
        update=PreferenceUpdate(
            body_md="Use a formal tone.",
            blocked_actions=("tasks.delete", "tasks.delete", " payroll.issue "),
            default_approval_mode="strict",
        ),
        actor_user_id=user_id,
        clock=clock,
    )

    assert row.body_md == "Use a formal tone."
    assert row.blocked_actions == ["tasks.delete", "payroll.issue"]
    assert row.default_approval_mode == "strict"
    assert default_approval_mode_for_workspace(db_session, ctx) == "strict"

    revision_count = db_session.scalar(
        select(func.count()).select_from(AgentPreferenceRevision)
    )
    assert revision_count == 1
    audit = db_session.scalar(select(AuditLog))
    assert audit is not None
    assert audit.action == "agent_preference.updated"


def test_resolver_builds_stable_sections_and_blocks_actions(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    workspace, ctx, user_id = _bind_workspace(db_session)
    save_preference(
        db_session,
        ctx,
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
        update=PreferenceUpdate(
            body_md="Plain language only.",
            blocked_actions=("tasks.cancel",),
            default_approval_mode="auto",
        ),
        actor_user_id=user_id,
        clock=clock,
    )
    save_preference(
        db_session,
        ctx,
        scope_kind="user",
        scope_id=user_id,
        update=PreferenceUpdate(body_md="Keep replies short."),
        actor_user_id=user_id,
        clock=clock,
    )

    bundle = resolve_preferences(
        db_session,
        ctx,
        capability="chat.manager",
        user_id=user_id,
    )

    assert f"## Workspace preferences -- {workspace.name}" in bundle.text
    assert "Plain language only." in bundle.text
    assert "## Your preferences --" in bundle.text
    assert "Keep replies short." in bundle.text
    assert is_action_blocked(bundle, "tasks.cancel")
    assert blocked_action_result_body("tasks.cancel") == {
        "error": "action_blocked_by_preferences",
        "action_key": "tasks.cancel",
    }


def test_non_preference_capability_gets_empty_header(
    db_session: Session,
) -> None:
    _workspace, ctx, user_id = _bind_workspace(db_session)

    bundle = resolve_preferences(
        db_session,
        ctx,
        capability="expenses.autofill",
        user_id=user_id,
    )

    assert bundle.text == "## Agent preferences\n(none)"
    assert bundle.blocked_actions == ()


def test_empty_preference_stack_gets_stable_empty_header(
    db_session: Session,
) -> None:
    _workspace, ctx, user_id = _bind_workspace(db_session)

    bundle = resolve_preferences(
        db_session,
        ctx,
        capability="chat.manager",
        user_id=user_id,
    )

    assert bundle.text == "## Agent preferences\n(none)"


def test_save_rejects_secret_like_preferences(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    _workspace, ctx, user_id = _bind_workspace(db_session)

    try:
        save_preference(
            db_session,
            ctx,
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
            update=PreferenceUpdate(body_md="door code is 1234"),
            actor_user_id=user_id,
            clock=clock,
        )
    except PreferenceContainsSecret:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("expected secret-like preference to be rejected")


def test_save_rejects_over_hard_cap(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    _workspace, ctx, user_id = _bind_workspace(db_session)

    try:
        save_preference(
            db_session,
            ctx,
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
            update=PreferenceUpdate(body_md="x" * 65_000),
            actor_user_id=user_id,
            clock=clock,
        )
    except PreferenceTooLarge:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("expected too-large preference to be rejected")
