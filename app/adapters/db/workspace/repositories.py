"""SA-backed repositories implementing the workspace-context Protocol seams.

The concrete classes here adapt SQLAlchemy ``Session`` work to the
Protocol surfaces declared on the identity context's port file:

* :class:`SqlAlchemyMembershipRepository` — wraps the ``user_workspace``
  derived junction, the ``work_engagement`` employment row, and the
  ``user_work_role`` link rows. Consumed by
  :mod:`app.services.employees.service` (profile + archive + reinstate
  + accept-time engagement seed) and by
  :mod:`app.domain.identity.work_engagements.seed_pending_work_engagement`
  (called from :func:`app.domain.identity.membership._activate_invite`).
  Closes the cd-dv2 stopgap.

The repo carries an open ``Session`` and never commits — the caller's
UoW owns the transaction boundary (§01 "Key runtime invariants" #3).
Mutating methods flush so the audit writer's FK reference to
``entity_id`` (and any peer read in the same UoW) sees the new row.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.workspace.models import (
    UserWorkRole,
    UserWorkspace,
    WorkEngagement,
)
from app.domain.identity.ports import (
    MembershipRepository,
    UserWorkRoleRow,
    UserWorkspaceRow,
    WorkEngagementRow,
)
from app.tenancy import tenant_agnostic

__all__ = [
    "SqlAlchemyMembershipRepository",
]


# ---------------------------------------------------------------------------
# Row projections
# ---------------------------------------------------------------------------


def _to_user_workspace_row(row: UserWorkspace) -> UserWorkspaceRow:
    """Project an ORM ``UserWorkspace`` into the seam-level row."""
    return UserWorkspaceRow(
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        source=row.source,
        added_at=row.added_at,
    )


def _to_work_engagement_row(row: WorkEngagement) -> WorkEngagementRow:
    """Project an ORM ``WorkEngagement`` into the seam-level row.

    Narrow shape — only the columns the employees service + the
    accept-time seeder consume. The richer mutable columns
    (``settings_override_json``, ``pay_destination_id``, …) ride on
    :mod:`app.domain.identity.work_engagements` directly until that
    module also routes through this seam.
    """
    return WorkEngagementRow(
        id=row.id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        engagement_kind=row.engagement_kind,
        supplier_org_id=row.supplier_org_id,
        started_on=row.started_on,
        archived_on=row.archived_on,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_user_work_role_row(row: UserWorkRole) -> UserWorkRoleRow:
    """Project an ORM ``UserWorkRole`` into the seam-level row."""
    return UserWorkRoleRow(
        id=row.id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        work_role_id=row.work_role_id,
        started_on=row.started_on,
        ended_on=row.ended_on,
        deleted_at=row.deleted_at,
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class SqlAlchemyMembershipRepository(MembershipRepository):
    """SA-backed concretion of :class:`MembershipRepository`.

    Wraps an open :class:`~sqlalchemy.orm.Session` and never commits —
    the caller's UoW owns the transaction boundary (§01 "Key runtime
    invariants" #3). Mutating methods flush so the audit writer's FK
    reference to ``entity_id`` sees the new row.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # -- user_workspace --------------------------------------------------

    def get_user_workspace(
        self, *, workspace_id: str, user_id: str
    ) -> UserWorkspaceRow | None:
        row = self._session.get(UserWorkspace, (user_id, workspace_id))
        # Defence-in-depth: even though the composite PK pins on the
        # tuple, re-assert ``workspace_id`` so a misconfigured filter
        # surfacing a sibling row would fail here loudly.
        if row is None or row.workspace_id != workspace_id:
            return None
        return _to_user_workspace_row(row)

    # -- work_engagement reads -------------------------------------------

    def get_active_engagement(
        self, *, workspace_id: str, user_id: str
    ) -> WorkEngagementRow | None:
        stmt = select(WorkEngagement).where(
            WorkEngagement.user_id == user_id,
            WorkEngagement.workspace_id == workspace_id,
            WorkEngagement.archived_on.is_(None),
        )
        row = self._session.scalars(stmt).one_or_none()
        return _to_work_engagement_row(row) if row is not None else None

    def get_latest_engagement(
        self, *, workspace_id: str, user_id: str
    ) -> WorkEngagementRow | None:
        stmt = (
            select(WorkEngagement)
            .where(
                WorkEngagement.user_id == user_id,
                WorkEngagement.workspace_id == workspace_id,
            )
            .order_by(WorkEngagement.created_at.desc(), WorkEngagement.id.desc())
            .limit(1)
        )
        row = self._session.scalars(stmt).one_or_none()
        return _to_work_engagement_row(row) if row is not None else None

    def list_active_engagements_for_users(
        self, *, workspace_id: str, user_ids: Iterable[str]
    ) -> Mapping[str, WorkEngagementRow]:
        ids = list(user_ids)
        if not ids:
            return {}
        stmt = select(WorkEngagement).where(
            WorkEngagement.user_id.in_(ids),
            WorkEngagement.workspace_id == workspace_id,
            WorkEngagement.archived_on.is_(None),
        )
        out: dict[str, WorkEngagementRow] = {}
        for row in self._session.scalars(stmt).all():
            out[row.user_id] = _to_work_engagement_row(row)
        return out

    def list_engagements_for_user_all_workspaces(
        self, *, user_id: str
    ) -> Sequence[WorkEngagementRow]:
        """Cross-workspace engagement scan — see Protocol docstring.

        Wraps the SELECT in :func:`tenant_agnostic` so the ORM tenant
        filter does not narrow to the caller's workspace; the
        deployment-level reinstate is the only call site (cd-pb8p) and
        owns the deployment-owner authority check upstream.
        """
        stmt = (
            select(WorkEngagement)
            .where(WorkEngagement.user_id == user_id)
            .order_by(
                WorkEngagement.workspace_id.asc(),
                WorkEngagement.created_at.asc(),
                WorkEngagement.id.asc(),
            )
        )
        # justification: deployment-level reinstate (cd-pb8p) needs every
        # workspace's engagement; the ORM tenant filter would narrow to
        # the caller's workspace. Authority is checked by the service.
        with tenant_agnostic():  # justification: cross-workspace reinstate scan.
            rows = self._session.scalars(stmt).all()
        return [_to_work_engagement_row(r) for r in rows]

    # -- work_engagement writes ------------------------------------------

    def insert_work_engagement(
        self,
        *,
        engagement_id: str,
        workspace_id: str,
        user_id: str,
        engagement_kind: str,
        supplier_org_id: str | None,
        started_on: date,
        created_at: datetime,
        updated_at: datetime,
    ) -> WorkEngagementRow:
        row = WorkEngagement(
            id=engagement_id,
            user_id=user_id,
            workspace_id=workspace_id,
            engagement_kind=engagement_kind,
            supplier_org_id=supplier_org_id,
            pay_destination_id=None,
            reimbursement_destination_id=None,
            started_on=started_on,
            archived_on=None,
            notes_md="",
            created_at=created_at,
            updated_at=updated_at,
        )
        self._session.add(row)
        self._session.flush()
        return _to_work_engagement_row(row)

    def set_engagement_archived_on(
        self,
        *,
        workspace_id: str,
        engagement_id: str,
        archived_on: date | None,
        updated_at: datetime,
    ) -> WorkEngagementRow:
        stmt = select(WorkEngagement).where(
            WorkEngagement.id == engagement_id,
            WorkEngagement.workspace_id == workspace_id,
        )
        row = self._session.scalars(stmt).one()
        row.archived_on = archived_on
        row.updated_at = updated_at
        self._session.flush()
        return _to_work_engagement_row(row)

    # -- user_work_role reads --------------------------------------------

    def list_user_work_roles(
        self,
        *,
        workspace_id: str,
        user_id: str,
        active_only: bool,
    ) -> Sequence[UserWorkRoleRow]:
        stmt = select(UserWorkRole).where(
            UserWorkRole.user_id == user_id,
            UserWorkRole.workspace_id == workspace_id,
        )
        if active_only:
            stmt = stmt.where(UserWorkRole.deleted_at.is_(None))
        rows = self._session.scalars(stmt).all()
        return [_to_user_work_role_row(r) for r in rows]

    # -- user_work_role writes -------------------------------------------

    def archive_user_work_roles(
        self,
        *,
        workspace_id: str,
        role_ids: Sequence[str],
        deleted_at: datetime,
        ended_on: date,
    ) -> None:
        if not role_ids:
            return
        # Pin the UPDATE on ``workspace_id`` as defence-in-depth even
        # though the ORM tenant filter narrows it. The id list arrives
        # from a previous SELECT inside the same UoW so the rows match.
        self._session.execute(
            update(UserWorkRole)
            .where(
                UserWorkRole.id.in_(role_ids),
                UserWorkRole.workspace_id == workspace_id,
            )
            .values(deleted_at=deleted_at, ended_on=ended_on)
            .execution_options(synchronize_session="fetch")
        )

    def reinstate_user_work_roles(
        self,
        *,
        workspace_id: str,
        role_ids: Sequence[str],
    ) -> None:
        if not role_ids:
            return
        self._session.execute(
            update(UserWorkRole)
            .where(
                UserWorkRole.id.in_(role_ids),
                UserWorkRole.workspace_id == workspace_id,
            )
            .values(deleted_at=None, ended_on=None)
            .execution_options(synchronize_session="fetch")
        )

    # -- audit reader (cd-9vi3) ------------------------------------------

    def get_latest_archive_role_ids(
        self, *, workspace_id: str, user_id: str
    ) -> list[str] | None:
        """Return the role-id scope for the *current* archive cycle.

        See :meth:`MembershipRepository.get_latest_archive_role_ids` for
        the contract. Implementation walks ``employee.archived`` and
        ``employee.reinstated`` audit rows newest-first under
        :func:`tenant_agnostic` (the deployment-wide reinstate path
        drives this per-workspace and the ORM tenant filter would
        otherwise narrow to the caller's workspace; the
        ``workspace_id`` predicate is still pinned in the WHERE clause
        as defence-in-depth).

        The "current cycle" is every ``employee.archived`` row newer
        than the most recent ``employee.reinstated`` (or the full
        archive history if no reinstate ever ran). The role-id scope
        is the **union** of every ``archived_user_work_role_ids``
        payload in that window — an idempotent re-archive of an
        already-archived user writes an empty list, so picking only
        the most recent row would lose the original scope. Union
        across the cycle preserves it.

        Returns ``None`` when no ``employee.archived`` rows exist in
        the current cycle (fresh install, or every archive in the
        cycle predates cd-3x4 and lacks the payload). The caller
        falls back to the legacy "every tombstoned row" sweep.
        """
        stmt = (
            select(AuditLog)
            .where(
                AuditLog.workspace_id == workspace_id,
                AuditLog.entity_kind == "user",
                AuditLog.entity_id == user_id,
                AuditLog.action.in_(("employee.archived", "employee.reinstated")),
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        )
        # justification: deployment-level reinstate (cd-pb8p) drives
        # this per-workspace and the ORM tenant filter would narrow to
        # the caller's workspace; pin workspace_id explicitly above.
        with tenant_agnostic():  # justification: cross-workspace audit lookup.
            rows = list(self._session.scalars(stmt).all())

        # Walk newest-first; stop at the first ``employee.reinstated``
        # because it ended the prior cycle. Union every
        # ``archived_user_work_role_ids`` payload above that boundary.
        seen: set[str] = set()
        ordered: list[str] = []
        any_archived = False
        for row in rows:
            if row.action == "employee.reinstated":
                break
            if row.action != "employee.archived":
                continue
            any_archived = True
            diff = row.diff
            if not isinstance(diff, dict):
                # Historical archives (or a future schema drift) that
                # lack a dict diff are skipped — they contribute no
                # scope. The fallback kicks in when no row contributed.
                continue
            ids = diff.get("archived_user_work_role_ids")
            if not isinstance(ids, list):
                continue
            for item in ids:
                # Defensive cast — the JSON column gives back ``Any``
                # and we only want strings on the wire. A non-string
                # slips through silently rather than poisoning the IN
                # list.
                if isinstance(item, str) and item not in seen:
                    seen.add(item)
                    ordered.append(item)

        if not any_archived:
            # No ``employee.archived`` row in the current cycle —
            # fresh install or pre-cd-3x4 history with no archives
            # since the last reinstate. Caller falls back.
            return None
        return ordered
