"""Shared helpers for worker job fan-outs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy import select

from app.adapters.db.demo.models import DemoWorkspace
from app.tenancy import WorkspaceContext

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session


# Pinned system-actor identifiers for worker-initiated fan-outs
# (LLM-budget refresh, occurrence-generator tick, future tenant-
# scoped sweeps). ``WorkspaceContext`` requires non-empty ULIDs on
# ``actor_id`` + ``audit_correlation_id``; the fan-out paths write
# either zero audit rows (refresh_aggregate) or workspace-anchored
# audit rows whose provenance the spec already pins by
# ``actor_kind = 'system'`` (generator's
# ``schedules.generation_tick``, ``schedules.skipped_for_closure``).
# A zero-ULID string satisfies the dataclass invariant without lying
# about provenance: any downstream seam that eventually reads these
# fields (e.g. a future worker-audit writer) sees an all-zero
# sentinel that operators can pattern-match on.
#
# Matches the convention in :func:`app.auth.signup._agnostic_audit_ctx`
# (system actor with zero-ULID ids) — kept module-private so callers
# don't accidentally construct the sentinel outside the scheduler's
# fan-out loops.
_SYSTEM_ACTOR_ZERO_ULID: Final[str] = "00000000000000000000000000"


def _system_actor_context(
    *,
    workspace_id: str,
    workspace_slug: str,
) -> WorkspaceContext:
    """Build a system-actor :class:`WorkspaceContext` for a worker fan-out.

    Shared by every worker fan-out body that needs a per-workspace
    context the tenant filter will accept (LLM-budget refresh,
    occurrence-generator tick, future tenant-scoped sweeps).
    ``actor_grant_role`` uses ``"manager"`` to mirror the established
    system-actor convention in the auth modules
    (:func:`app.auth.signup._agnostic_audit_ctx`,
    :func:`app.auth.recovery._agnostic_audit_ctx`,
    :func:`app.auth.magic_link._agnostic_audit_ctx`, and the passkey
    and session ``actor_kind="system"`` sites). The field is unused
    for ``actor_kind="system"`` rows in audit writes; picking the same
    canonical value across every system-actor context lets operators
    ``grep`` one shape when triaging a "which ctx fired this?" thread.
    """
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=_SYSTEM_ACTOR_ZERO_ULID,
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=_SYSTEM_ACTOR_ZERO_ULID,
        principal_kind="system",
    )


def _demo_expired_workspace_ids(
    session: Session,
    workspace_ids: list[str],
    *,
    now: datetime,
) -> set[str]:
    """Return the subset of ``workspace_ids`` whose demo TTL has passed.

    §24 "Demo mode" / "Garbage collection" is the spec of record:
    every ``demo_workspace`` row carries an ``expires_at``; once it
    is in the past the workspace is awaiting GC and the generator
    fan-outs must skip it (running materialisation on a soon-to-be-
    purged tenant is wasted work and would race the ``demo_gc``
    sweep). The result is the set of workspace ids the caller should
    drop from its per-workspace loop and roll into
    ``total_workspaces_skipped``.

    ``demo_workspace`` is the demo tenancy anchor — it carries no
    ``workspace_id`` of its own — so the SELECT runs inside
    ``tenant_agnostic`` (the caller already holds that bracket via
    the surrounding workspace enumeration).
    """
    if not workspace_ids:
        return set()

    stmt = (
        select(DemoWorkspace.id)
        .where(DemoWorkspace.id.in_(workspace_ids))
        .where(DemoWorkspace.expires_at < now)
    )
    # ``DemoWorkspace.id`` is a ``Mapped[str]`` (SQLAlchemy 2.x typed
    # column), so ``session.scalars(stmt).all()`` is exactly
    # ``Sequence[str]`` — no membership re-filter needed; the IN-list
    # in the WHERE clause already constrains the result to a subset
    # of the input.
    return set(session.scalars(stmt).all())
