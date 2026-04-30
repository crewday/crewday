"""Workspace ownership verification for lifting unverified LLM caps."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import Workspace
from app.adapters.mail.ports import Mailer
from app.audit import write_audit
from app.auth import magic_link
from app.auth._throttle import Throttle
from app.authz.owners import is_owner_member
from app.config import Settings
from app.services.workspace.settings_service import OwnersOnlyError
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock

__all__ = [
    "WorkspaceVerificationMismatch",
    "WorkspaceVerificationNotFound",
    "consume_ownership_verification",
    "request_ownership_verification",
]


_TTL = timedelta(minutes=15)


class WorkspaceVerificationNotFound(LookupError):
    """Workspace or owner identity could not be loaded."""


class WorkspaceVerificationMismatch(ValueError):
    """Magic-link subject does not match the current workspace."""


def request_ownership_verification(
    session: Session,
    ctx: WorkspaceContext,
    *,
    ip: str,
    mailer: Mailer,
    base_url: str,
    throttle: Throttle,
    settings: Settings,
    clock: Clock | None = None,
) -> magic_link.PendingDispatch:
    """Mail a workspace-ownership verification link to the current owner."""
    _require_owner(session, ctx)
    with tenant_agnostic():
        user = session.get(User, ctx.actor_id)
    if user is None:
        raise WorkspaceVerificationNotFound(
            f"owner user {ctx.actor_id!r} not found for workspace verification"
        )

    pending = magic_link.request_link(
        session,
        email=user.email,
        purpose="workspace_verify_ownership",
        ip=ip,
        mailer=mailer,
        base_url=base_url,
        ttl=_TTL,
        throttle=throttle,
        settings=settings,
        clock=clock,
        subject_id=ctx.workspace_id,
    )
    if pending is None:
        raise WorkspaceVerificationNotFound(
            f"workspace verification link was not minted for {ctx.workspace_id!r}"
        )
    dispatch = magic_link.PendingDispatch()
    dispatch.add_pending(pending)
    return dispatch


def consume_ownership_verification(
    session: Session,
    ctx: WorkspaceContext,
    *,
    token: str,
    ip: str,
    throttle: Throttle,
    settings: Settings,
    clock: Clock | None = None,
) -> str:
    """Consume the verification link and promote the workspace."""
    _require_owner(session, ctx)
    resolved_clock = clock if clock is not None else SystemClock()
    preview = magic_link.peek_link(
        session,
        token=token,
        expected_purpose="workspace_verify_ownership",
        ip=ip,
        throttle=throttle,
        settings=settings,
        clock=resolved_clock,
    )
    if preview.subject_id != ctx.workspace_id:
        raise WorkspaceVerificationMismatch(
            "workspace verification link does not belong to this workspace"
        )
    outcome = magic_link.consume_link(
        session,
        token=token,
        expected_purpose="workspace_verify_ownership",
        ip=ip,
        throttle=throttle,
        settings=settings,
        clock=resolved_clock,
    )

    with tenant_agnostic():
        workspace = session.get(Workspace, ctx.workspace_id)
    if workspace is None:
        raise WorkspaceVerificationNotFound(
            f"workspace {ctx.workspace_id!r} not found for verification"
        )

    before = workspace.verification_state
    if before != "human_verified":
        workspace.verification_state = "human_verified"
        workspace.updated_at = resolved_clock.now()
        session.flush()
        write_audit(
            session,
            ctx,
            entity_kind="workspace",
            entity_id=workspace.id,
            action="workspace.ownership_verified",
            diff={
                "before": {"verification_state": before},
                "after": {"verification_state": "human_verified"},
                "email_hash": outcome.email_hash,
                "ip_hash": outcome.ip_hash,
            },
            clock=resolved_clock,
        )
    return workspace.verification_state


def _require_owner(session: Session, ctx: WorkspaceContext) -> None:
    if not is_owner_member(
        session,
        workspace_id=ctx.workspace_id,
        user_id=ctx.actor_id,
    ):
        raise OwnersOnlyError(
            f"actor {ctx.actor_id!r} is not an owners-group member of "
            f"workspace {ctx.workspace_id!r}"
        )
