"""Derive-refresh reconciler for the ``user_workspace`` junction (cd-yqm4).

``user_workspace`` is documented as a derived junction (§02
"user_workspace"). A row says "user U is materialised in workspace W"
because at least one upstream grants U access to W. The upstreams the
spec enumerates today (§02 line 1680, ``source`` enum) are:

* ``workspace_grant`` — a workspace-scoped :class:`RoleGrant` row
  (``scope_kind = 'workspace'``, ``scope_property_id IS NULL``).
* ``property_grant`` — a property-scoped :class:`RoleGrant` row
  resolved through :class:`PropertyWorkspace` to a workspace.
* ``org_grant`` — a deployment / organisation-scoped row resolved
  through ``org_workspace``. Forward-compat seam: the
  ``organization`` and ``org_workspace`` tables do not exist yet
  (§02 line 1673 reserves the ``scope_kind`` enum value); the
  reconciler treats them as a no-op until they land.
* ``work_engagement`` — a non-archived
  :class:`~app.adapters.db.workspace.models.WorkEngagement` row.

This module is the canonical reconciler. Two surfaces drive it:

* **Worker fan-out** —
  :func:`app.worker.scheduler._make_user_workspace_refresh_body`
  calls :func:`reconcile_user_workspace` on a tenant-agnostic UoW
  every :data:`~app.worker.scheduler.USER_WORKSPACE_REFRESH_INTERVAL_SECONDS`
  seconds. Owns steady-state churn — out-of-band writes, schema
  drift, anything the inline path missed.
* **Inline scoped reconcile** — :func:`reconcile_user_workspace_for`
  runs in the same transaction as the upstream write for the
  redirect-target flows that cannot tolerate the worker tick's
  eventual-consistency lag (signup → ``/w/<slug>/today``;
  invite-accept and member removal → the tenancy resolver fails
  closed on a missing or stale row). Same algorithm, narrowed to
  one ``(user, workspace)`` cell.

Domain services (grant, invite/accept, member removal) write the
upstream rows; they invoke :func:`reconcile_user_workspace_for` for
their own ``(user, workspace)`` pair when the redirect cannot
tolerate the lag, and otherwise let the worker tick refresh the
junction.

**Source precedence** (highest first when a row has multiple live
upstreams): ``workspace_grant > property_grant > org_grant >
work_engagement``. The :class:`UserWorkspace` model only stores one
``source`` per row; precedence picks the strongest reason the user
can see the workspace, so a manager-by-grant who is also engaged
shows the grant rather than the engagement.

**Persistence on source change.** A row whose dominant source is
revoked but a weaker upstream remains keeps its ``added_at`` and
flips ``source`` to the next-strongest active upstream. ``added_at``
records when the user FIRST became visible in the workspace; a
worker tick that observes the row already exists never overwrites
that timestamp.

**Atomicity.** :func:`reconcile_user_workspace` runs inside the
caller's UoW under :func:`tenant_agnostic` — the function never
calls ``session.commit()``. The worker fan-out body opens its own
UoW and commits on clean exit.

**Forward-compat seams.** ``org_workspace`` is unconditionally
absent today; a future migration that ships the table needs only
to drop one branch into :func:`_org_grant_pairs` (the seam returns
an empty iterable today). Tests assert the seam stays a no-op
until the table exists, so a regression that imports a not-yet-
landed model gets caught at the unit layer rather than at runtime.

See ``docs/specs/02-domain-model.md`` §"user_workspace" and §"role_grants",
``docs/specs/01-architecture.md`` §"Worker".
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.places.models import PropertyWorkspace
from app.adapters.db.workspace.models import UserWorkspace, WorkEngagement
from app.tenancy import tenant_agnostic

__all__ = [
    "ReconciliationReport",
    "reconcile_user_workspace",
    "reconcile_user_workspace_for",
]


_log = logging.getLogger(__name__)


# Source precedence — index 0 wins when a row has multiple live
# upstreams. Pinned as a tuple (not a frozenset) because order matters
# and the iteration is small enough that O(n) lookup is faster than
# the hash-table overhead. Mirrors the §02 enum order.
_SOURCE_PRECEDENCE: Final[tuple[str, ...]] = (
    "workspace_grant",
    "property_grant",
    "org_grant",
    "work_engagement",
)


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Counts surfaced by one :func:`reconcile_user_workspace` pass.

    The worker tick logs these into the
    ``worker.identity.user_workspace.tick.summary`` event so operator
    dashboards can plot churn (rows added vs. dropped vs. source-flipped)
    without re-querying the table.
    """

    rows_inserted: int
    rows_deleted: int
    rows_source_flipped: int
    upstream_pairs_seen: int


def reconcile_user_workspace(
    session: Session,
    *,
    now: datetime,
) -> ReconciliationReport:
    """Reconcile ``user_workspace`` against every live upstream.

    Walks every workspace-scoped :class:`RoleGrant`, every
    :class:`PropertyWorkspace`-resolved property-scoped grant, and
    every active :class:`WorkEngagement`. Computes the (user, workspace,
    source) triples that should exist, deduplicates by precedence per
    (user, workspace), then UPSERTs missing rows and DELETEs orphaned
    ones.

    Runs under :func:`tenant_agnostic` because the reconciler operates
    across every workspace in one pass. The caller's UoW owns the
    transaction boundary — this function never commits.

    ``now`` is the tick's ``clock.now()``; new ``user_workspace`` rows
    are stamped with this value. Existing rows keep their ``added_at``
    even when their source flips.
    """
    with tenant_agnostic():
        desired = _collect_desired_pairs(session)
        existing = _load_existing_user_workspace(session)

        rows_inserted = 0
        rows_deleted = 0
        rows_source_flipped = 0

        # Pass 1: insert / source-flip every desired pair.
        for (user_id, workspace_id), source in desired.items():
            row = existing.get((user_id, workspace_id))
            if row is None:
                session.add(
                    UserWorkspace(
                        user_id=user_id,
                        workspace_id=workspace_id,
                        source=source,
                        added_at=now,
                    )
                )
                rows_inserted += 1
                continue
            if row.source != source:
                # ``added_at`` stays — we record when the user first
                # appeared, not when the precedence flipped.
                row.source = source
                rows_source_flipped += 1

        # Pass 2: delete every existing row whose every upstream is
        # gone. Build the list of victims first so we don't mutate the
        # ``existing`` mapping mid-iteration.
        orphans = [pk for pk in existing if pk not in desired]
        for user_id, workspace_id in orphans:
            session.execute(
                delete(UserWorkspace)
                .where(
                    UserWorkspace.user_id == user_id,
                    UserWorkspace.workspace_id == workspace_id,
                )
                .execution_options(synchronize_session="fetch")
            )
            rows_deleted += 1

        if rows_inserted or rows_deleted or rows_source_flipped:
            # ``flush`` here so the caller's subsequent reads see the
            # new state. Worker tick UoWs commit on exit; tests
            # likewise flush before asserting.
            session.flush()

    return ReconciliationReport(
        rows_inserted=rows_inserted,
        rows_deleted=rows_deleted,
        rows_source_flipped=rows_source_flipped,
        upstream_pairs_seen=len(desired),
    )


def reconcile_user_workspace_for(
    session: Session,
    *,
    user_id: str,
    workspace_id: str,
    now: datetime,
) -> ReconciliationReport:
    """Reconcile ``user_workspace`` for one ``(user_id, workspace_id)`` cell.

    The scoped sibling of :func:`reconcile_user_workspace`. Used by
    transactional domain flows that cannot tolerate the worker tick's
    eventual-consistency lag — invite-accept and member removal both
    write upstream rows whose effect on the redirect target the user
    sees on the very next request:

    * **Invite-accept.** The HTTP response carries a redirect to
      ``/w/<slug>/today``; the tenancy resolver fails closed on a
      missing ``user_workspace`` row, so without this call the user
      would see a 404 for up to
      :data:`~app.worker.scheduler.USER_WORKSPACE_REFRESH_INTERVAL_SECONDS`
      seconds until the worker tick caught up.
    * **Member removal.** A removed user with a stale
      ``user_workspace`` row still resolves a :class:`WorkspaceContext`
      for the workspace until the next tick; the action catalog gates
      every write but the dashboard shell still renders. Synchronous
      drop closes the gap.

    Algorithmically this is the global reconciler narrowed to one
    cell: same source-precedence rules, same "preserve ``added_at``
    on a source flip" semantics, same idempotency. The cost is one
    SELECT per upstream (workspace-scoped grant, property-scoped
    grant resolved through :class:`PropertyWorkspace`,
    :class:`WorkEngagement`) plus one ``user_workspace`` read — a
    handful of indexed lookups, not a full-table scan. Safe to call
    inline inside an HTTP request UoW.

    Runs under :func:`tenant_agnostic` so callers in flows without an
    active :class:`WorkspaceContext` (e.g. the bare-host invite-accept
    surface for new users) do not trip the tenant filter.
    """
    with tenant_agnostic():
        desired_source = _collect_desired_source_for(
            session, user_id=user_id, workspace_id=workspace_id
        )
        existing_row = session.get(UserWorkspace, (user_id, workspace_id))

        rows_inserted = 0
        rows_deleted = 0
        rows_source_flipped = 0

        if desired_source is None:
            if existing_row is not None:
                session.execute(
                    delete(UserWorkspace)
                    .where(
                        UserWorkspace.user_id == user_id,
                        UserWorkspace.workspace_id == workspace_id,
                    )
                    .execution_options(synchronize_session="fetch")
                )
                rows_deleted = 1
        elif existing_row is None:
            session.add(
                UserWorkspace(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    source=desired_source,
                    added_at=now,
                )
            )
            rows_inserted = 1
        elif existing_row.source != desired_source:
            # ``added_at`` stays — see :func:`reconcile_user_workspace`
            # for the precedence-flip rationale.
            existing_row.source = desired_source
            rows_source_flipped = 1

        if rows_inserted or rows_deleted or rows_source_flipped:
            session.flush()

    return ReconciliationReport(
        rows_inserted=rows_inserted,
        rows_deleted=rows_deleted,
        rows_source_flipped=rows_source_flipped,
        upstream_pairs_seen=1 if desired_source is not None else 0,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_desired_source_for(
    session: Session, *, user_id: str, workspace_id: str
) -> str | None:
    """Return the dominant source for one ``(user_id, workspace_id)`` cell.

    Walks the upstreams in precedence order, returning the FIRST hit;
    the same rule the global reconciler applies, narrowed to one
    cell. Returns ``None`` if no upstream materialises the user in
    the workspace.
    """
    # workspace_grant — workspace-scoped role_grant, no property
    # sub-scope, matching workspace. cd-x1xh: live grants only — a
    # soft-retired grant must not keep the user_workspace junction
    # row alive past revoke.
    if (
        session.scalar(
            select(RoleGrant.id)
            .where(RoleGrant.scope_kind == "workspace")
            .where(RoleGrant.scope_property_id.is_(None))
            .where(RoleGrant.user_id == user_id)
            .where(RoleGrant.workspace_id == workspace_id)
            .where(RoleGrant.revoked_at.is_(None))
            .limit(1)
        )
        is not None
    ):
        return "workspace_grant"

    # property_grant — property-scoped role_grant resolved through
    # property_workspace to the target workspace. The user may hold a
    # grant on any property mapped into ``workspace_id``. cd-x1xh:
    # live grants only.
    if (
        session.scalar(
            select(RoleGrant.id)
            .join(
                PropertyWorkspace,
                PropertyWorkspace.property_id == RoleGrant.scope_property_id,
            )
            .where(RoleGrant.scope_kind == "workspace")
            .where(RoleGrant.scope_property_id.is_not(None))
            .where(RoleGrant.user_id == user_id)
            .where(PropertyWorkspace.workspace_id == workspace_id)
            .where(RoleGrant.revoked_at.is_(None))
            .limit(1)
        )
        is not None
    ):
        return "property_grant"

    # org_grant — forward-compat seam, currently always empty (see
    # :func:`_org_grant_pairs`'s docstring).

    # work_engagement — non-archived engagement.
    if (
        session.scalar(
            select(WorkEngagement.id)
            .where(WorkEngagement.user_id == user_id)
            .where(WorkEngagement.workspace_id == workspace_id)
            .where(WorkEngagement.archived_on.is_(None))
            .limit(1)
        )
        is not None
    ):
        return "work_engagement"

    return None


def _collect_desired_pairs(session: Session) -> dict[tuple[str, str], str]:
    """Return the dominant ``source`` per ``(user_id, workspace_id)``.

    Walks every upstream in precedence order; the FIRST source seen for
    a pair wins. Walking high-precedence upstreams first means we never
    overwrite a strong source with a weaker one — the spec's "at least
    one upstream" rule is satisfied by any single hit, but the column
    has to record the strongest.
    """
    desired: dict[tuple[str, str], str] = {}

    def _record(pairs: Iterable[tuple[str, str]], source: str) -> None:
        for pair in pairs:
            desired.setdefault(pair, source)

    _record(_workspace_grant_pairs(session), "workspace_grant")
    _record(_property_grant_pairs(session), "property_grant")
    _record(_org_grant_pairs(session), "org_grant")
    _record(_work_engagement_pairs(session), "work_engagement")
    return desired


def _workspace_grant_pairs(session: Session) -> list[tuple[str, str]]:
    """Pairs from workspace-scoped :class:`RoleGrant` rows.

    Filters to ``scope_kind = 'workspace'`` (the legacy default), with
    ``scope_property_id IS NULL`` so property-scoped rows route through
    the property-grant seam below. Deployment-scoped grants
    (``workspace_id IS NULL``) are not workspace memberships and stay
    out of the result.
    """
    stmt = (
        select(RoleGrant.user_id, RoleGrant.workspace_id)
        .where(RoleGrant.scope_kind == "workspace")
        .where(RoleGrant.scope_property_id.is_(None))
        .where(RoleGrant.workspace_id.is_not(None))
        # cd-x1xh: live grants only — soft-retired grants must not
        # keep the user_workspace junction row alive past revoke.
        .where(RoleGrant.revoked_at.is_(None))
    )
    return [(user_id, workspace_id) for user_id, workspace_id in session.execute(stmt)]


def _property_grant_pairs(session: Session) -> list[tuple[str, str]]:
    """Pairs from property-scoped :class:`RoleGrant` rows.

    Joins through :class:`PropertyWorkspace` so a grant on property P
    materialises the user in every workspace that holds P. The same
    physical property can belong to several workspaces, so a single
    property grant may produce more than one (user, workspace) pair.
    """
    stmt = (
        select(RoleGrant.user_id, PropertyWorkspace.workspace_id)
        .join(
            PropertyWorkspace,
            PropertyWorkspace.property_id == RoleGrant.scope_property_id,
        )
        .where(RoleGrant.scope_kind == "workspace")
        .where(RoleGrant.scope_property_id.is_not(None))
        # cd-x1xh: live grants only.
        .where(RoleGrant.revoked_at.is_(None))
    )
    return [(user_id, workspace_id) for user_id, workspace_id in session.execute(stmt)]


def _org_grant_pairs(session: Session) -> list[tuple[str, str]]:
    """Forward-compat seam: org-scoped grants resolved through ``org_workspace``.

    The ``organization`` and ``org_workspace`` tables do not exist yet
    (cd-4saj's follow-up cd-0ro4 lands them later). Returning an empty
    list keeps the reconciler's contract stable: every other source
    contributes its rows; this seam sits dormant until the table
    arrives. When it does, drop the SELECT here — no other call site
    needs to change.

    Resolved via :mod:`importlib` rather than a static import so a
    static ``from app.adapters.db.org.models import OrgWorkspace`` does
    not hard-fail at module load time on a deployment without the
    package. Mirrors the pattern in
    :func:`app.worker.scheduler._demo_expired_workspace_ids`.
    """
    del session  # pragma: no cover — see docstring; reserved for future use
    return []


def _work_engagement_pairs(session: Session) -> list[tuple[str, str]]:
    """Pairs from non-archived :class:`WorkEngagement` rows.

    A user with an active engagement (``archived_on IS NULL``) in
    workspace W is a member of W via the engagement source. Archived
    engagements stop contributing — the row falls back to a weaker
    source (or is deleted entirely) on the next tick.
    """
    stmt = select(WorkEngagement.user_id, WorkEngagement.workspace_id).where(
        WorkEngagement.archived_on.is_(None)
    )
    return [(user_id, workspace_id) for user_id, workspace_id in session.execute(stmt)]


def _load_existing_user_workspace(
    session: Session,
) -> dict[tuple[str, str], UserWorkspace]:
    """Return every :class:`UserWorkspace` row keyed by composite PK.

    One pass over the table is cheap relative to the per-pair
    ``session.get`` the alternative would imply, and the result feeds
    both the source-flip pass and the orphan-DELETE pass below.
    """
    rows = session.scalars(select(UserWorkspace)).all()
    return {(row.user_id, row.workspace_id): row for row in rows}


# ``_SOURCE_PRECEDENCE`` is exported for tests that pin the precedence
# order — pinning here keeps "workspace beats engagement" visible at the
# domain surface without forcing tests to reach into the helper module.
def is_higher_precedence(a: str, b: str) -> bool:
    """Return ``True`` if source ``a`` outranks source ``b``.

    Helper for tests and any future caller that needs to compare two
    sources without re-deriving the precedence order. The internal
    reconciler does not call this — it iterates the precedence tuple
    in order, which is faster than per-pair lookups.
    """
    if a not in _SOURCE_PRECEDENCE:
        raise ValueError(f"unknown source: {a!r}")
    if b not in _SOURCE_PRECEDENCE:
        raise ValueError(f"unknown source: {b!r}")
    return _SOURCE_PRECEDENCE.index(a) < _SOURCE_PRECEDENCE.index(b)
