"""Unit tests for the cd-duv6 ``PermissionGroupRepository`` seam.

The full CRUD round-trip with a real DB session, the tenant filter
and audit writes lives under
``tests/integration/identity/test_permission_groups.py``. These
tests exercise the **seam** — confirming
:mod:`app.domain.identity.permission_groups` runs against a stub
repository without reaching for SQLAlchemy at all. Catches a
regression where a domain function silently re-imports the SA model
classes (the very stopgap cd-duv6 closes).

The stub repo also runs through the audit writer, so we cover the
shared ``repo.session`` accessor by passing a fake session that
records :meth:`add` calls.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from app.adapters.db.authz.ports import (
    PermissionGroupMemberRow,
    PermissionGroupRepository,
    PermissionGroupRow,
    PermissionGroupSlugTakenError,
)
from app.domain.identity.permission_groups import (
    PermissionGroupNotFound,
    PermissionGroupSlugTaken,
    SystemGroupProtected,
    UnknownCapability,
    add_member,
    create_group,
    delete_group,
    get_group,
    list_groups,
    list_members,
    remove_member,
    update_group,
)
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_WS_ID = "01HWA00000000000000000WS01"
_ACTOR_ID = "01HWA00000000000000000USR1"


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

    The domain service only calls :meth:`add` (via ``write_audit``),
    so we record the audit row's ORM instance for later assertions.
    Everything else raises so a missed migration shows up loudly.
    """

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, instance: object) -> None:
        self.added.append(instance)


class _FakeRepo(PermissionGroupRepository):
    """In-memory :class:`PermissionGroupRepository` stub.

    Models the workspace-scoped surface the domain service consumes:
    one ``permission_group`` table keyed by ``(workspace_id, id)``
    and one ``permission_group_member`` table keyed by
    ``(group_id, user_id)``. No tenant filter — the domain code
    always passes ``ctx.workspace_id`` explicitly.
    """

    def __init__(self) -> None:
        self._groups: dict[str, PermissionGroupRow] = {}
        self._members: dict[tuple[str, str], PermissionGroupMemberRow] = {}
        self._session = _FakeSession()

    @property
    def session(self) -> Any:
        # The Protocol declares ``Session`` but the domain code only
        # routes the accessor into ``write_audit``, which itself only
        # calls ``.add`` — see ``_FakeSession``. Returning ``Any`` here
        # avoids importing SA at all in this unit test; mypy accepts
        # ``Any`` as covariantly compatible with the declared
        # ``Session`` return without a type-ignore.
        return self._session

    # -- Group reads -----------------------------------------------------

    def list_groups(self, *, workspace_id: str) -> Sequence[PermissionGroupRow]:
        return sorted(
            (g for g in self._groups.values()),
            key=lambda g: (g.created_at, g.id),
        )

    def get_group(
        self, *, workspace_id: str, group_id: str
    ) -> PermissionGroupRow | None:
        return self._groups.get(group_id)

    # -- Group writes ----------------------------------------------------

    def insert_group(
        self,
        *,
        group_id: str,
        workspace_id: str,
        slug: str,
        name: str,
        system: bool,
        capabilities: dict[str, Any],
        created_at: datetime,
    ) -> PermissionGroupRow:
        if any(g.slug == slug for g in self._groups.values()):
            raise PermissionGroupSlugTakenError(slug)
        row = PermissionGroupRow(
            id=group_id,
            slug=slug,
            name=name,
            system=system,
            capabilities=dict(capabilities),
            created_at=created_at,
        )
        self._groups[group_id] = row
        return row

    def update_group(
        self,
        *,
        workspace_id: str,
        group_id: str,
        name: str | None = None,
        capabilities: dict[str, Any] | None = None,
    ) -> PermissionGroupRow:
        existing = self._groups[group_id]
        new_name = name if name is not None else existing.name
        new_caps = (
            dict(capabilities)
            if capabilities is not None
            else dict(existing.capabilities)
        )
        updated = PermissionGroupRow(
            id=existing.id,
            slug=existing.slug,
            name=new_name,
            system=existing.system,
            capabilities=new_caps,
            created_at=existing.created_at,
        )
        self._groups[group_id] = updated
        return updated

    def delete_group(self, *, workspace_id: str, group_id: str) -> None:
        del self._groups[group_id]

    # -- Member reads ----------------------------------------------------

    def list_members(
        self, *, workspace_id: str, group_id: str
    ) -> Sequence[PermissionGroupMemberRow]:
        return sorted(
            (m for (gid, _), m in self._members.items() if gid == group_id),
            key=lambda m: (m.added_at, m.user_id),
        )

    def get_member(
        self, *, group_id: str, user_id: str
    ) -> PermissionGroupMemberRow | None:
        return self._members.get((group_id, user_id))

    # -- Member writes ---------------------------------------------------

    def insert_member(
        self,
        *,
        group_id: str,
        user_id: str,
        workspace_id: str,
        added_at: datetime,
        added_by_user_id: str | None,
    ) -> PermissionGroupMemberRow:
        row = PermissionGroupMemberRow(
            group_id=group_id,
            user_id=user_id,
            added_at=added_at,
            added_by_user_id=added_by_user_id,
        )
        self._members[(group_id, user_id)] = row
        return row

    def delete_member(self, *, group_id: str, user_id: str) -> None:
        self._members.pop((group_id, user_id), None)


def _seed_owners(repo: _FakeRepo) -> PermissionGroupRow:
    """Seed the ``owners`` system group + the bootstrap actor as a member.

    Mirrors what :mod:`app.adapters.db.authz.bootstrap` lands on a
    real workspace at signup. Lets the unit suite cover the
    last-owner guard without spinning up SQLAlchemy.
    """
    row = PermissionGroupRow(
        id="01HWA00000000000000000GR01",
        slug="owners",
        name="Owners",
        system=True,
        capabilities={"all": True},
        created_at=_PINNED,
    )
    repo._groups[row.id] = row
    repo._members[(row.id, _ACTOR_ID)] = PermissionGroupMemberRow(
        group_id=row.id,
        user_id=_ACTOR_ID,
        added_at=_PINNED,
        added_by_user_id=None,
    )
    return row


class TestCreateGroup:
    def test_happy_path(self) -> None:
        repo = _FakeRepo()
        ref = create_group(
            repo,
            _ctx(),
            slug="family",
            name="Family",
            capabilities={"tasks.create": True},
            clock=FrozenClock(_PINNED),
        )
        assert ref.slug == "family"
        assert ref.system is False
        # Audit row queued through the repo's session accessor.
        assert len(repo._session.added) == 1

    def test_slug_taken_maps_to_public_error(self) -> None:
        repo = _FakeRepo()
        create_group(
            repo,
            _ctx(),
            slug="family",
            name="Family",
            capabilities={},
            clock=FrozenClock(_PINNED),
        )
        with pytest.raises(PermissionGroupSlugTaken):
            create_group(
                repo,
                _ctx(),
                slug="family",
                name="Family II",
                capabilities={},
                clock=FrozenClock(_PINNED),
            )

    def test_unknown_capability_is_pre_db(self) -> None:
        """Unknown capabilities trip BEFORE any repo write.

        Audit row count stays at zero so the caller's UoW has nothing
        to roll back.
        """
        repo = _FakeRepo()
        with pytest.raises(UnknownCapability):
            create_group(
                repo,
                _ctx(),
                slug="bad",
                name="Bad",
                capabilities={"does.not.exist": True},
                clock=FrozenClock(_PINNED),
            )
        assert repo._groups == {}
        assert repo._session.added == []


class TestUpdateGroup:
    def test_system_group_capabilities_rejected(self) -> None:
        repo = _FakeRepo()
        owners = _seed_owners(repo)
        with pytest.raises(SystemGroupProtected):
            update_group(
                repo,
                _ctx(),
                group_id=owners.id,
                capabilities={"tasks.create": True},
                clock=FrozenClock(_PINNED),
            )
        # The repo never touched the row.
        assert repo._groups[owners.id].capabilities == {"all": True}

    def test_system_group_name_change_succeeds(self) -> None:
        repo = _FakeRepo()
        owners = _seed_owners(repo)
        ref = update_group(
            repo,
            _ctx(),
            group_id=owners.id,
            name="Custodians",
            clock=FrozenClock(_PINNED),
        )
        assert ref.name == "Custodians"


class TestDeleteGroup:
    def test_system_group_rejected(self) -> None:
        repo = _FakeRepo()
        owners = _seed_owners(repo)
        with pytest.raises(SystemGroupProtected):
            delete_group(repo, _ctx(), group_id=owners.id, clock=FrozenClock(_PINNED))


class TestMembership:
    def test_add_member_fresh_inserts_through_seam(self) -> None:
        """A first-time add reaches :meth:`PermissionGroupRepository.insert_member`.

        Covers the happy-path write through the seam (the idempotent
        re-add path skips the insert call). Verifies the row lands in
        the fake repo and an audit row queues through ``repo.session``.
        """
        repo = _FakeRepo()
        owners = _seed_owners(repo)
        new_member = "01HWA00000000000000000USR2"
        ref = add_member(
            repo,
            _ctx(),
            group_id=owners.id,
            user_id=new_member,
            clock=FrozenClock(_PINNED),
        )
        assert ref.user_id == new_member
        assert ref.added_by_user_id == _ACTOR_ID
        # Bootstrap owner + the new member.
        assert len(repo._members) == 2
        # One audit row from the actual insert path.
        assert len(repo._session.added) == 1

    def test_add_member_idempotent(self) -> None:
        repo = _FakeRepo()
        owners = _seed_owners(repo)
        # Re-add the bootstrap owner — no-op write, audit lands.
        add_member(
            repo,
            _ctx(),
            group_id=owners.id,
            user_id=_ACTOR_ID,
            clock=FrozenClock(_PINNED),
        )
        assert len(repo._members) == 1
        # The idempotent path still emits one audit row.
        assert len(repo._session.added) == 1

    def test_remove_unknown_group_raises(self) -> None:
        repo = _FakeRepo()
        with pytest.raises(PermissionGroupNotFound):
            remove_member(
                repo,
                _ctx(),
                group_id="01HWA00000000000000000NONE",
                user_id=_ACTOR_ID,
                clock=FrozenClock(_PINNED),
            )

    def test_remove_missing_member_emits_audit_no_delete(self) -> None:
        """Removing a non-member is idempotent; audit row still lands.

        Mirrors the :func:`remove_member` contract — a stale "remove me
        again" on a missing member is a no-op write that still records
        the intent. The fake repo's :meth:`delete_member` is never
        called because the domain code skips it when ``get_member``
        returns ``None``.
        """
        repo = _FakeRepo()
        owners = _seed_owners(repo)
        absent_user = "01HWA00000000000000000USR9"
        remove_member(
            repo,
            _ctx(),
            group_id=owners.id,
            user_id=absent_user,
            clock=FrozenClock(_PINNED),
        )
        # Bootstrap owner stays — no member was deleted.
        assert (owners.id, _ACTOR_ID) in repo._members
        assert (owners.id, absent_user) not in repo._members
        # One audit row queued through the session accessor.
        assert len(repo._session.added) == 1


class TestReads:
    def test_list_groups_empty(self) -> None:
        assert list(list_groups(_FakeRepo(), _ctx())) == []

    def test_get_missing_raises(self) -> None:
        with pytest.raises(PermissionGroupNotFound):
            get_group(_FakeRepo(), _ctx(), group_id="01HWA00000000000000000NONE")

    def test_list_members_unknown_group_raises(self) -> None:
        with pytest.raises(PermissionGroupNotFound):
            list_members(_FakeRepo(), _ctx(), group_id="01HWA00000000000000000NONE")
