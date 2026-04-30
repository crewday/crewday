"""Unit tests for :mod:`app.adapters.db.authz.models`.

Pure-Python sanity on the SQLAlchemy mapped classes: construction,
tablenames, constraint shapes, and the re-exports from
``app.adapters.db.authz``. Integration coverage (migrations, FK
cascade, uniqueness + CHECK violations against a real DB, tenant
filter behaviour, ``seed_owners_system_group`` round-trip) lives in
``tests/integration/test_db_authz.py``.

See ``docs/specs/02-domain-model.md`` §"permission_group",
§"permission_group_member", §"role_grants" and
``docs/specs/05-employees-and-roles.md`` §"Roles & groups".
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from app.adapters.db.authz import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
    seed_owners_system_group,
)
from app.adapters.db.authz import models as authz_models
from app.adapters.db.authz.bootstrap import (
    seed_owners_system_group as _seed_from_bootstrap,
)

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


class TestPermissionGroupModel:
    """The ``PermissionGroup`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        group = PermissionGroup(
            id="01HWA00000000000000000PGRA",
            workspace_id="01HWA00000000000000000WSPA",
            slug="owners",
            name="Owners",
            system=True,
            capabilities_json={"all": True},
            created_at=_PINNED,
        )
        assert group.id == "01HWA00000000000000000PGRA"
        assert group.workspace_id == "01HWA00000000000000000WSPA"
        assert group.slug == "owners"
        assert group.name == "Owners"
        assert group.system is True
        assert group.capabilities_json == {"all": True}
        assert group.created_at == _PINNED

    def test_user_defined_group_construction(self) -> None:
        """A non-system group carries ``system=False`` and an empty payload."""
        group = PermissionGroup(
            id="01HWA00000000000000000PGRB",
            workspace_id="01HWA00000000000000000WSPA",
            slug="front-desk",
            name="Front Desk",
            system=False,
            capabilities_json={},
            created_at=_PINNED,
        )
        assert group.system is False
        assert group.capabilities_json == {}

    def test_tablename(self) -> None:
        assert PermissionGroup.__tablename__ == "permission_group"

    def test_workspace_slug_unique_constraint_present(self) -> None:
        """``__table_args__`` carries the ``(workspace_id, slug)`` UNIQUE."""
        uniques = [
            c for c in PermissionGroup.__table_args__ if isinstance(c, UniqueConstraint)
        ]
        assert len(uniques) == 1
        assert uniques[0].name == "uq_permission_group_workspace_slug"
        assert [c.name for c in uniques[0].columns] == ["workspace_id", "slug"]


class TestPermissionGroupMemberModel:
    """The ``PermissionGroupMember`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        member = PermissionGroupMember(
            group_id="01HWA00000000000000000PGRA",
            user_id="01HWA00000000000000000USRA",
            workspace_id="01HWA00000000000000000WSPA",
            added_at=_PINNED,
        )
        assert member.group_id == "01HWA00000000000000000PGRA"
        assert member.user_id == "01HWA00000000000000000USRA"
        assert member.workspace_id == "01HWA00000000000000000WSPA"
        assert member.added_at == _PINNED
        # Self-bootstrap membership: ``added_by_user_id`` defaults to ``None``.
        assert member.added_by_user_id is None

    def test_added_by_can_be_set(self) -> None:
        """Non-bootstrap rows record the acting user."""
        member = PermissionGroupMember(
            group_id="01HWA00000000000000000PGRA",
            user_id="01HWA00000000000000000USRA",
            workspace_id="01HWA00000000000000000WSPA",
            added_at=_PINNED,
            added_by_user_id="01HWA00000000000000000USRB",
        )
        assert member.added_by_user_id == "01HWA00000000000000000USRB"

    def test_tablename(self) -> None:
        assert PermissionGroupMember.__tablename__ == "permission_group_member"

    def test_workspace_index_present(self) -> None:
        """``__table_args__`` carries the ``workspace_id`` index."""
        indexes = [
            i for i in PermissionGroupMember.__table_args__ if isinstance(i, Index)
        ]
        names = [i.name for i in indexes]
        assert "ix_permission_group_member_workspace" in names
        target = next(
            i for i in indexes if i.name == "ix_permission_group_member_workspace"
        )
        assert [c.name for c in target.columns] == ["workspace_id"]


class TestRoleGrantModel:
    """The ``RoleGrant`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        grant = RoleGrant(
            id="01HWA00000000000000000RGRA",
            workspace_id="01HWA00000000000000000WSPA",
            user_id="01HWA00000000000000000USRA",
            grant_role="manager",
            created_at=_PINNED,
        )
        assert grant.id == "01HWA00000000000000000RGRA"
        assert grant.workspace_id == "01HWA00000000000000000WSPA"
        assert grant.user_id == "01HWA00000000000000000USRA"
        assert grant.grant_role == "manager"
        # NULL ``scope_property_id`` means workspace-wide.
        assert grant.scope_property_id is None
        assert grant.binding_org_id is None
        # Self-bootstrap grant: ``created_by_user_id`` defaults to ``None``.
        assert grant.created_by_user_id is None
        assert grant.created_at == _PINNED

    def test_deployment_construction(self) -> None:
        """A deployment-scope grant constructs with ``workspace_id=None``.

        cd-wchi: deployment grants live at the bare-host level —
        ``scope_kind='deployment'`` is required (no Python default for
        the deployment partition); ``workspace_id`` is None.
        """
        grant = RoleGrant(
            id="01HWA00000000000000000RGRD",
            workspace_id=None,
            user_id="01HWA00000000000000000USRD",
            grant_role="manager",
            scope_kind="deployment",
            created_at=_PINNED,
        )
        assert grant.workspace_id is None
        assert grant.scope_kind == "deployment"

    def test_property_scoped_grant_construction(self) -> None:
        """A property-scoped grant carries the FK ``scope_property_id``."""
        grant = RoleGrant(
            id="01HWA00000000000000000RGRB",
            workspace_id="01HWA00000000000000000WSPA",
            user_id="01HWA00000000000000000USRA",
            grant_role="worker",
            scope_property_id="01HWA00000000000000000PRPA",
            created_at=_PINNED,
        )
        assert grant.scope_property_id == "01HWA00000000000000000PRPA"

    def test_client_binding_org_construction(self) -> None:
        """A workspace-scoped client grant may bind to one organization."""
        grant = RoleGrant(
            id="01HWA00000000000000000RGRC",
            workspace_id="01HWA00000000000000000WSPA",
            user_id="01HWA00000000000000000USRA",
            grant_role="client",
            binding_org_id="01HWA00000000000000000ORGA",
            created_at=_PINNED,
        )
        assert grant.binding_org_id == "01HWA00000000000000000ORGA"

    def test_tablename(self) -> None:
        assert RoleGrant.__tablename__ == "role_grant"

    def test_grant_role_check_constraint_present(self) -> None:
        """``__table_args__`` carries the ``grant_role`` CHECK constraint."""
        checks = [c for c in RoleGrant.__table_args__ if isinstance(c, CheckConstraint)]
        # cd-wchi adds ``scope_kind`` + ``scope_kind_workspace_pairing``
        # CHECKs. Constraint names render under the shared naming
        # convention as ``ck_role_grant_<body>``.
        names = {c.name for c in checks}
        assert "ck_role_grant_grant_role" in names
        grant_role_check = next(
            c for c in checks if c.name == "ck_role_grant_grant_role"
        )
        sql = str(grant_role_check.sqltext)
        for role in ("manager", "worker", "client", "guest"):
            assert role in sql, f"{role} missing from CHECK constraint"
        # v0 ``owner`` value must not appear — governance is on the
        # permission group in v1 (see §02 "role_grants").
        assert "'owner'" not in sql.replace("'owners'", "")

    def test_scope_kind_check_constraint_present(self) -> None:
        """cd-wchi adds the ``scope_kind`` enum CHECK."""
        checks = [c for c in RoleGrant.__table_args__ if isinstance(c, CheckConstraint)]
        names = {c.name for c in checks}
        assert "ck_role_grant_scope_kind" in names
        scope_kind_check = next(
            c for c in checks if c.name == "ck_role_grant_scope_kind"
        )
        sql = str(scope_kind_check.sqltext)
        for value in ("workspace", "deployment"):
            assert value in sql, f"{value} missing from scope_kind CHECK"

    def test_scope_kind_workspace_pairing_check_present(self) -> None:
        """The biconditional CHECK on ``(scope_kind, workspace_id)`` is wired."""
        checks = [c for c in RoleGrant.__table_args__ if isinstance(c, CheckConstraint)]
        names = {c.name for c in checks}
        assert "ck_role_grant_scope_kind_workspace_pairing" in names
        pairing_check = next(
            c for c in checks if c.name == "ck_role_grant_scope_kind_workspace_pairing"
        )
        sql = str(pairing_check.sqltext)
        # Both directions must be expressed.
        assert "deployment" in sql
        assert "workspace" in sql
        assert "IS NULL" in sql
        assert "IS NOT NULL" in sql

    def test_client_binding_org_scope_check_present(self) -> None:
        """``binding_org_id`` is constrained to workspace client grants."""
        checks = [c for c in RoleGrant.__table_args__ if isinstance(c, CheckConstraint)]
        names = {c.name for c in checks}
        assert "ck_role_grant_client_binding_org_scope" in names
        binding_check = next(
            c for c in checks if c.name == "ck_role_grant_client_binding_org_scope"
        )
        sql = str(binding_check.sqltext)
        assert "binding_org_id IS NULL" in sql
        assert "grant_role = 'client'" in sql
        assert "scope_property_id IS NULL" in sql

    def test_deployment_partial_unique_index_present(self) -> None:
        """cd-wchi adds the partial UNIQUE on ``(user_id, grant_role)``."""
        indexes = [i for i in RoleGrant.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "uq_role_grant_deployment_user_role" in names
        target = next(
            i for i in indexes if i.name == "uq_role_grant_deployment_user_role"
        )
        assert [c.name for c in target.columns] == ["user_id", "grant_role"]
        assert target.unique is True

    def test_workspace_user_index_present(self) -> None:
        indexes = [i for i in RoleGrant.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_role_grant_workspace_user" in names
        target = next(i for i in indexes if i.name == "ix_role_grant_workspace_user")
        assert [c.name for c in target.columns] == ["workspace_id", "user_id"]

    def test_scope_property_index_present(self) -> None:
        indexes = [i for i in RoleGrant.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_role_grant_scope_property" in names
        target = next(i for i in indexes if i.name == "ix_role_grant_scope_property")
        assert [c.name for c in target.columns] == ["scope_property_id"]

    def test_binding_org_index_present(self) -> None:
        indexes = [i for i in RoleGrant.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_role_grant_binding_org" in names
        target = next(i for i in indexes if i.name == "ix_role_grant_binding_org")
        assert [c.name for c in target.columns] == ["workspace_id", "binding_org_id"]


class TestPackageReExports:
    """``app.adapters.db.authz`` re-exports models + seed helper."""

    def test_models_re_exported(self) -> None:
        """Every v1-slice class is reachable from the package root."""
        assert PermissionGroup is authz_models.PermissionGroup
        assert PermissionGroupMember is authz_models.PermissionGroupMember
        assert RoleGrant is authz_models.RoleGrant

    def test_seed_helper_re_exported(self) -> None:
        """``seed_owners_system_group`` lives on the package root."""
        assert seed_owners_system_group is _seed_from_bootstrap
