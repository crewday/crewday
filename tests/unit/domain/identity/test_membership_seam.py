"""Unit tests for the cd-hso7 ``MembershipRepository`` seam.

The full integration round-trip with a real DB session, the tenant
filter, and ``write_audit`` lives under
``tests/integration/identity/test_invite_accept.py`` (accept-time
seed) and ``tests/unit/services/test_service_employees.py`` (profile
+ archive + reinstate, against the SA-backed concretion). These tests
exercise the **seam** — confirming the domain entry point
:func:`app.domain.identity.work_engagements.seed_pending_work_engagement`
runs against a stub :class:`MembershipRepository` without reaching
for SQLAlchemy at all. Catches a regression where a domain function
silently re-imports the SA model classes (the very stopgap cd-dv2 /
cd-hso7 closes).

The fake repo also routes through the audit writer via
:attr:`MembershipRepository.session`, so we cover the shared accessor
by passing a fake session that records :meth:`add` calls.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from app.domain.identity.ports import (
    MembershipRepository,
    UserWorkRoleRow,
    UserWorkspaceRow,
    WorkEngagementRow,
)
from app.domain.identity.work_engagements import seed_pending_work_engagement
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_WS_ID = "01HWA00000000000000000WS01"
_ACTOR_ID = "01HWA00000000000000000USR1"
_TARGET_ID = "01HWA00000000000000000USR2"


def _ctx() -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=_WS_ID,
        workspace_slug="ws",
        actor_id=_ACTOR_ID,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


class _FakeSession:
    """Tiny stand-in for :class:`sqlalchemy.orm.Session`.

    The accept-time seeder only routes the session into
    :func:`app.audit.write_audit`, which itself only calls ``.add``
    on the audit row. Anything else raises so a missed migration
    shows up loudly.
    """

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, instance: object) -> None:
        self.added.append(instance)


class _FakeRepo(MembershipRepository):
    """In-memory :class:`MembershipRepository` stub.

    Models the workspace-scoped surface the domain consumes — one
    ``user_workspace`` table keyed by ``(user_id, workspace_id)``,
    one ``work_engagement`` table keyed by ``id`` (with a partial
    UNIQUE on ``(user_id, workspace_id) WHERE archived_on IS NULL``
    that the seeder relies on), and one ``user_work_role`` table
    keyed by ``id``. No tenant filter — the domain code always passes
    ``ctx.workspace_id`` explicitly and the fake re-asserts the
    predicate as defence-in-depth.
    """

    def __init__(self) -> None:
        self._user_workspaces: dict[tuple[str, str], UserWorkspaceRow] = {}
        self._engagements: dict[str, WorkEngagementRow] = {}
        self._roles: dict[str, UserWorkRoleRow] = {}
        self._session = _FakeSession()
        # Recording surfaces so callers can assert on the writes that
        # landed without traversing the table state.
        self.inserted_engagements: list[WorkEngagementRow] = []

    @property
    def session(self) -> Any:
        # The Protocol declares ``Session`` but the domain code only
        # routes the accessor into ``write_audit``, which itself only
        # calls ``.add`` — see ``_FakeSession``. Returning ``Any``
        # avoids importing SA at all in this unit test; mypy accepts
        # ``Any`` as covariantly compatible with the declared
        # ``Session`` return without a type-ignore.
        return self._session

    # -- user_workspace --------------------------------------------------

    def get_user_workspace(
        self, *, workspace_id: str, user_id: str
    ) -> UserWorkspaceRow | None:
        return self._user_workspaces.get((user_id, workspace_id))

    # -- work_engagement reads -------------------------------------------

    def get_active_engagement(
        self, *, workspace_id: str, user_id: str
    ) -> WorkEngagementRow | None:
        for row in self._engagements.values():
            if (
                row.user_id == user_id
                and row.workspace_id == workspace_id
                and row.archived_on is None
            ):
                return row
        return None

    def get_latest_engagement(
        self, *, workspace_id: str, user_id: str
    ) -> WorkEngagementRow | None:
        candidates = [
            row
            for row in self._engagements.values()
            if row.user_id == user_id and row.workspace_id == workspace_id
        ]
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda r: (r.created_at, r.id),
            reverse=True,
        )[0]

    def list_active_engagements_for_users(
        self, *, workspace_id: str, user_ids: Iterable[str]
    ) -> Mapping[str, WorkEngagementRow]:
        ids = set(user_ids)
        out: dict[str, WorkEngagementRow] = {}
        for row in self._engagements.values():
            if (
                row.workspace_id == workspace_id
                and row.user_id in ids
                and row.archived_on is None
            ):
                out[row.user_id] = row
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
        row = WorkEngagementRow(
            id=engagement_id,
            user_id=user_id,
            workspace_id=workspace_id,
            engagement_kind=engagement_kind,
            supplier_org_id=supplier_org_id,
            started_on=started_on,
            archived_on=None,
            created_at=created_at,
            updated_at=updated_at,
        )
        self._engagements[engagement_id] = row
        self.inserted_engagements.append(row)
        return row

    def set_engagement_archived_on(
        self,
        *,
        workspace_id: str,
        engagement_id: str,
        archived_on: date | None,
        updated_at: datetime,
    ) -> WorkEngagementRow:
        existing = self._engagements[engagement_id]
        assert existing.workspace_id == workspace_id
        updated = WorkEngagementRow(
            id=existing.id,
            user_id=existing.user_id,
            workspace_id=existing.workspace_id,
            engagement_kind=existing.engagement_kind,
            supplier_org_id=existing.supplier_org_id,
            started_on=existing.started_on,
            archived_on=archived_on,
            created_at=existing.created_at,
            updated_at=updated_at,
        )
        self._engagements[engagement_id] = updated
        return updated

    # -- user_work_role reads --------------------------------------------

    def list_user_work_roles(
        self,
        *,
        workspace_id: str,
        user_id: str,
        active_only: bool,
    ) -> Sequence[UserWorkRoleRow]:
        out = [
            r
            for r in self._roles.values()
            if r.user_id == user_id and r.workspace_id == workspace_id
        ]
        if active_only:
            out = [r for r in out if r.deleted_at is None]
        return out

    # -- user_work_role writes -------------------------------------------

    def archive_user_work_roles(
        self,
        *,
        workspace_id: str,
        role_ids: Sequence[str],
        deleted_at: datetime,
        ended_on: date,
    ) -> None:
        for rid in role_ids:
            row = self._roles[rid]
            assert row.workspace_id == workspace_id
            self._roles[rid] = UserWorkRoleRow(
                id=row.id,
                user_id=row.user_id,
                workspace_id=row.workspace_id,
                work_role_id=row.work_role_id,
                started_on=row.started_on,
                ended_on=ended_on,
                deleted_at=deleted_at,
            )

    def reinstate_user_work_roles(
        self,
        *,
        workspace_id: str,
        role_ids: Sequence[str],
    ) -> None:
        for rid in role_ids:
            row = self._roles[rid]
            assert row.workspace_id == workspace_id
            self._roles[rid] = UserWorkRoleRow(
                id=row.id,
                user_id=row.user_id,
                workspace_id=row.workspace_id,
                work_role_id=row.work_role_id,
                started_on=row.started_on,
                ended_on=None,
                deleted_at=None,
            )

    # -- audit reader (cd-9vi3) ------------------------------------------

    def get_latest_archive_role_ids(
        self, *, workspace_id: str, user_id: str
    ) -> list[str] | None:
        # The fake doesn't model the audit log; tests that drive the
        # reinstate path against this seam don't exercise the
        # archive-scope lookup. Returning ``None`` keeps the legacy
        # full-sweep contract for any caller that does.
        return None


class TestSeedPendingWorkEngagementSeam:
    """Drive :func:`seed_pending_work_engagement` against a fake repo.

    No DB session, no SA model imports — proves the domain helper
    routes every workspace-context read/write through the
    :class:`MembershipRepository` Protocol.
    """

    def test_inserts_a_pending_payroll_engagement(self) -> None:
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        ctx = _ctx()

        row = seed_pending_work_engagement(
            repo, ctx, user_id=_TARGET_ID, now=_PINNED, clock=clock
        )

        # Exactly one engagement row landed via the repo seam.
        assert len(repo.inserted_engagements) == 1
        assert repo.inserted_engagements[0].id == row.id
        # Phase 1 defaults match §22 + the helper's docstring.
        assert row.engagement_kind == "payroll"
        assert row.supplier_org_id is None
        assert row.archived_on is None
        assert row.started_on == _PINNED.date()
        assert row.user_id == _TARGET_ID
        assert row.workspace_id == _WS_ID
        # The audit writer received exactly one row through the
        # threaded session — proves the ``repo.session`` accessor is
        # the seam ``write_audit`` uses.
        assert len(repo.session.added) == 1

    def test_idempotent_replay_returns_existing_row(self) -> None:
        """Second call returns the existing row without re-inserting.

        Mirrors the partial UNIQUE index guarantee on
        ``(user_id, workspace_id) WHERE archived_on IS NULL``.
        """
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        ctx = _ctx()

        first = seed_pending_work_engagement(
            repo, ctx, user_id=_TARGET_ID, now=_PINNED, clock=clock
        )
        second = seed_pending_work_engagement(
            repo, ctx, user_id=_TARGET_ID, now=_PINNED, clock=clock
        )

        assert first.id == second.id
        # Only the first call wrote a row + an audit entry.
        assert len(repo.inserted_engagements) == 1
        assert len(repo.session.added) == 1

    def test_default_clock_is_resolved_lazily(self) -> None:
        """``clock=None`` falls back to a real-time SystemClock.

        The seeder must accept ``clock=None`` and not crash on a
        missing ULID generator — the ULID is built from the same
        clock argument the caller passes (which may be ``None``).
        """
        repo = _FakeRepo()
        ctx = _ctx()

        row = seed_pending_work_engagement(
            repo, ctx, user_id=_TARGET_ID, now=_PINNED, clock=None
        )

        assert row.user_id == _TARGET_ID
        assert len(repo.inserted_engagements) == 1
