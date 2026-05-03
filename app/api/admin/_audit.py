"""Per-route deployment-audit shim for the admin tree.

Every mutating admin route writes one ``audit_log`` row with
``scope_kind='deployment'`` (§02 "audit_log", §12 "Admin
surface"). The writer in :mod:`app.audit` takes the actor /
correlation kwargs verbatim — this shim resolves them off the
:class:`DeploymentContext` + the ambient request id once, so the
route bodies only thread ``entity_kind`` / ``entity_id`` /
``action`` / ``diff``.

Keeping the resolution here (not on the writer itself) means a
future :class:`DeploymentContext` upgrade — e.g. a typed
``audit_correlation_id`` field, or a per-request actor cache —
lands in one place, without touching every admin handler.

See ``docs/specs/02-domain-model.md`` §"audit_log" and
``docs/specs/12-rest-api.md`` §"Admin surface".
"""

from __future__ import annotations

from typing import Any, Final, Literal

from fastapi import Request
from sqlalchemy.orm import Session

from app.api.transport import admin_sse
from app.audit import write_deployment_audit
from app.authz.deployment_owners import is_deployment_owner
from app.tenancy import DeploymentContext
from app.util.ulid import new_ulid

__all__ = ["audit_admin"]


# Mapping from :data:`DeploymentContext.actor_kind` to the
# :data:`app.tenancy.context.ActorKind` enum the audit writer
# accepts. The two enums diverge: deployment principals carry a
# ``"delegated"`` flavour that the workspace-side actor enum does
# not — both collapse to ``"agent"`` at the audit-row level so the
# downstream feed has one stable cardinality. ``"user"`` and
# ``"agent"`` pass through unchanged.
_ACTOR_KIND_FOR_AUDIT: Final[dict[str, Literal["user", "agent", "system"]]] = {
    "user": "user",
    "delegated": "agent",
    "agent": "agent",
}


def audit_admin(
    session: Session,
    *,
    ctx: DeploymentContext,
    request: Request,
    entity_kind: str,
    entity_id: str,
    action: str,
    diff: dict[str, Any] | list[Any] | None = None,
) -> None:
    """Append one deployment-scoped audit row inside the caller's UoW.

    Resolves the audit row's actor identity + correlation id off
    ``ctx`` and the ambient :class:`Request`:

    * ``actor_id`` — :attr:`DeploymentContext.user_id` (the human
      or delegating human; mirrors :func:`write_audit`'s shape).
    * ``actor_kind`` — narrowed via :data:`_ACTOR_KIND_FOR_AUDIT`
      so the row's ``actor_kind`` enum stays inside the
      ``user|agent|system`` set the workspace-side feed expects.
    * ``actor_grant_role`` — pinned to ``"manager"`` for v1.
      Deployment grants today land as ``grant_role='manager'``
      (§02 "role_grants"). The audit row's own
      ``actor_grant_role`` is informational, not authoritative; the
      :attr:`actor_was_owner_member` bit is the load-bearing
      governance signal.
    * ``actor_was_owner_member`` — read from ``deployment_owner`` so
      root-only mutations carry the governance signal.
    * ``correlation_id`` — the ambient ``X-Request-Id`` (the
      :class:`RequestIdMiddleware` already minted one when the
      request arrived; if it is missing for any reason, the writer
      falls back to a fresh ULID so the column's NOT NULL contract
      holds).

    The writer never commits; the caller's UoW
    (:class:`UnitOfWorkImpl`) owns the transaction boundary so a
    handler that raises after the audit write rolls the row back
    with the rest of the failure path.
    """
    correlation_id = (
        request.headers.get("X-Request-Id")
        or request.headers.get("X-Correlation-Id")
        or new_ulid()
    )
    write_deployment_audit(
        session,
        actor_id=ctx.user_id,
        actor_kind=_ACTOR_KIND_FOR_AUDIT[ctx.actor_kind],
        # ``manager`` is the v1 ``role_grant.grant_role`` value used
        # for every deployment grant (§02 "role_grants").
        actor_grant_role="manager",
        actor_was_owner_member=is_deployment_owner(session, user_id=ctx.user_id),
        correlation_id=correlation_id,
        entity_kind=entity_kind,
        entity_id=entity_id,
        action=action,
        diff=diff,
    )
    admin_sse.publish_admin_event(
        kind="admin.audit.appended",
        ctx=ctx,
        request=request,
        payload={
            "entity_kind": entity_kind,
            "entity_id": entity_id,
            "action": action,
        },
    )
