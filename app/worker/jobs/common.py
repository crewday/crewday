"""Shared helpers for worker job fan-outs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy import select

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
    must skip it (running materialisation on a soon-to-be-purged
    tenant is wasted work and would race with the ``demo_gc`` sweep).

    The :class:`DemoWorkspace` table does not exist yet — cd-otv3 +
    cd-h0ja are the open follow-ups that land it. Until then this
    helper returns an empty set, the fan-out treats every workspace
    as live, and the count surfaced in the tick summary stays at
    zero. Once the model is in place the missing-module branch falls
    away and the SELECT below picks up the filter without further
    work in the fan-out.

    Resolved via :mod:`importlib` rather than a static ``from ...
    import ...`` because the demo package does not exist on disk
    today — a static import would either hard-fail at module load
    time or force ``# type: ignore`` to placate ``mypy --strict``.
    Both are worse than this seam: ``importlib.import_module`` raises
    :class:`ModuleNotFoundError` at call time (a subclass of
    :class:`ImportError`, narrowed below), no other exception class
    is swallowed, and the helper stays type-safe.
    """
    if not workspace_ids:
        return set()

    import importlib

    try:
        demo_module = importlib.import_module("app.adapters.db.demo.models")
    except ModuleNotFoundError:
        return set()

    # The model class itself stays attribute-resolved — ``getattr``
    # is the only safe form for a runtime-only import. The
    # ``DemoWorkspace`` mapper is mandatory once the package
    # exists; an :class:`AttributeError` here would be a packaging
    # bug we want to surface, not swallow.
    demo_workspace = demo_module.DemoWorkspace

    # ``demo_workspace.id`` is a 1:1 FK to ``workspace.id`` (§24
    # "Entity"), so the predicate is a simple ``id IN ...`` plus the
    # ``expires_at`` cutoff. ``demo_workspace`` is the demo tenancy
    # anchor — it carries no ``workspace_id`` of its own — so the
    # SELECT runs inside ``tenant_agnostic`` (the caller already
    # holds that bracket via the Workspace enumeration).
    stmt = (
        select(demo_workspace.id)
        .where(demo_workspace.id.in_(workspace_ids))
        .where(demo_workspace.expires_at < now)
    )
    # ``demo_workspace`` came in via :mod:`importlib`, so the column
    # type stays ``Any`` from mypy's view. Belt-and-braces filter the
    # scalars to the input set: we promise a ``set[str]`` whose every
    # element appears in ``workspace_ids``, so a future schema change
    # that returned a wrapped type (e.g. a ULID dataclass) cannot
    # silently flag a live workspace as expired through equality
    # surprise. A string that fails the membership check is dropped
    # — fail-open is the right default for a sweep skip filter.
    candidate_set = set(workspace_ids)
    return {
        candidate_id
        for candidate_id in session.scalars(stmt).all()
        if isinstance(candidate_id, str) and candidate_id in candidate_set
    }
