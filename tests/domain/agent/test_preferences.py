"""Tests for agent preference storage and runtime injection."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.llm.models import AgentPreferenceRevision
from app.adapters.db.workspace.models import Workspace
from app.domain.agent.preferences import (
    INJECTION_TOKEN_CAP,
    PreferenceContainsSecret,
    PreferenceTooLarge,
    PreferenceUpdate,
    _estimate_tokens,
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


def _save_scope(
    db_session: Session,
    ctx: WorkspaceContext,
    clock: FrozenClock,
    *,
    scope_kind: str,
    scope_id: str,
    body_md: str,
    actor_user_id: str,
) -> None:
    save_preference(
        db_session,
        ctx,
        scope_kind=scope_kind,  # type: ignore[arg-type]
        scope_id=scope_id,
        update=PreferenceUpdate(body_md=body_md),
        actor_user_id=actor_user_id,
        clock=clock,
    )


# Injection budget is 8_000 tokens ~= 32_000 chars (see INJECTION_TOKEN_CAP).
# A ~33_000-char blob is over the injection budget but under the 16_000-token
# hard save cap, so save_preference accepts it while the resolver must trim.
_OVER_BUDGET = 33_000


def test_resolver_keeps_all_sections_under_budget(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    _workspace, ctx, user_id = _bind_workspace(db_session)
    _save_scope(
        db_session,
        ctx,
        clock,
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
        body_md="WORKSPACE_BODY",
        actor_user_id=user_id,
    )
    _save_scope(
        db_session,
        ctx,
        clock,
        scope_kind="property",
        scope_id="prop-1",
        body_md="PROPERTY_BODY",
        actor_user_id=user_id,
    )
    _save_scope(
        db_session,
        ctx,
        clock,
        scope_kind="user",
        scope_id=user_id,
        body_md="USER_BODY",
        actor_user_id=user_id,
    )

    bundle = resolve_preferences(
        db_session,
        ctx,
        capability="chat.manager",
        property_ids=("prop-1",),
        user_id=user_id,
    )

    assert "WORKSPACE_BODY" in bundle.text
    assert "PROPERTY_BODY" in bundle.text
    assert "USER_BODY" in bundle.text
    assert "[truncated]" not in bundle.text


def test_resolver_drops_property_first_when_just_over_budget(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    _workspace, ctx, user_id = _bind_workspace(db_session)
    _save_scope(
        db_session,
        ctx,
        clock,
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
        body_md="WORKSPACE_BODY",
        actor_user_id=user_id,
    )
    # Only the property blob is large enough to blow the budget; dropping it
    # brings workspace + user comfortably back under.
    _save_scope(
        db_session,
        ctx,
        clock,
        scope_kind="property",
        scope_id="prop-1",
        body_md="PROPERTY_BODY " + "p" * _OVER_BUDGET,
        actor_user_id=user_id,
    )
    _save_scope(
        db_session,
        ctx,
        clock,
        scope_kind="user",
        scope_id=user_id,
        body_md="USER_BODY",
        actor_user_id=user_id,
    )

    bundle = resolve_preferences(
        db_session,
        ctx,
        capability="chat.manager",
        property_ids=("prop-1",),
        user_id=user_id,
    )

    assert "PROPERTY_BODY" not in bundle.text
    assert "## Property preferences -- prop-1" not in bundle.text
    assert "WORKSPACE_BODY" in bundle.text
    assert "USER_BODY" in bundle.text
    assert "[truncated]" not in bundle.text


def test_resolver_truncates_workspace_after_dropping_property(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    _workspace, ctx, user_id = _bind_workspace(db_session)
    # Workspace alone is over budget, so dropping property is not enough:
    # the workspace blob must be truncated while the user blob survives.
    _save_scope(
        db_session,
        ctx,
        clock,
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
        body_md="WORKSPACE_BODY " + "w" * _OVER_BUDGET,
        actor_user_id=user_id,
    )
    _save_scope(
        db_session,
        ctx,
        clock,
        scope_kind="property",
        scope_id="prop-1",
        body_md="PROPERTY_BODY",
        actor_user_id=user_id,
    )
    _save_scope(
        db_session,
        ctx,
        clock,
        scope_kind="user",
        scope_id=user_id,
        body_md="USER_BODY_INTACT",
        actor_user_id=user_id,
    )

    bundle = resolve_preferences(
        db_session,
        ctx,
        capability="chat.manager",
        property_ids=("prop-1",),
        user_id=user_id,
    )

    assert "PROPERTY_BODY" not in bundle.text
    assert "[truncated]" in bundle.text
    assert bundle.text.rstrip().endswith("USER_BODY_INTACT")
    # The workspace heading survives (truncation cuts from the end), the user
    # blob is present in full, and the [truncated] marker precedes it.
    assert "## Workspace preferences" in bundle.text
    assert bundle.text.index("[truncated]") < bundle.text.index("USER_BODY_INTACT")
    # The central promise of the fix: once the user blob fits, truncating the
    # workspace blob genuinely brings the injected stack back under the cap.
    assert _estimate_tokens(bundle.text) <= INJECTION_TOKEN_CAP


def test_resolver_never_truncates_user_even_when_user_alone_over_budget(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    _workspace, ctx, user_id = _bind_workspace(db_session)
    _save_scope(
        db_session,
        ctx,
        clock,
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
        body_md="WORKSPACE_BODY " + "w" * _OVER_BUDGET,
        actor_user_id=user_id,
    )
    _save_scope(
        db_session,
        ctx,
        clock,
        scope_kind="property",
        scope_id="prop-1",
        body_md="PROPERTY_BODY",
        actor_user_id=user_id,
    )
    user_body = "USER_HEAD " + "u" * _OVER_BUDGET + " USER_TAIL"
    _save_scope(
        db_session,
        ctx,
        clock,
        scope_kind="user",
        scope_id=user_id,
        body_md=user_body,
        actor_user_id=user_id,
    )

    bundle = resolve_preferences(
        db_session,
        ctx,
        capability="chat.manager",
        property_ids=("prop-1",),
        user_id=user_id,
    )

    # User blob is never truncated: both ends survive verbatim even though the
    # total exceeds the injection budget once property is gone.
    assert user_body in bundle.text
    assert bundle.text.rstrip().endswith("USER_TAIL")
    assert "PROPERTY_BODY" not in bundle.text
    # Workspace is fully truncated down to (essentially) the marker.
    assert "[truncated]" in bundle.text
    assert "w" * 100 not in bundle.text


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
