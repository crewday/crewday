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
