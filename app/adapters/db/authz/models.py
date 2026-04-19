"""permission_group / permission_group_member / role_grant SQLAlchemy models.

v1 slice per cd-ctb. The richer §02 / §05 surface (``permission_rule``
join, ``started_on`` / ``ended_on``, ``revoked_*`` on ``role_grant``,
``description_md`` / ``group_kind`` / ``is_derived`` /
``updated_at`` / ``deleted_at`` on ``permission_group``,
``revoked_at`` on ``permission_group_member``, permission resolution
caches, etc.) is deferred to cd-79r (role-grant CRUD) and cd-zkr
(group CRUD) and lands via follow-up migrations without breaking this
migration's public write contract.

``scope_property_id`` on ``role_grant`` is a **soft reference**: the
``property`` table lands with cd-i6u, which owns the FK promotion in
its own migration. Leaving the column a plain ``String`` for now keeps
the v1 slice landable against the current schema and keeps
referential drift visible through application-layer checks until the
FK can be added.

See ``docs/specs/02-domain-model.md`` §"permission_group",
§"permission_group_member", §"role_grants" and
``docs/specs/05-employees-and-roles.md`` §"Roles & groups".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = ["PermissionGroup", "PermissionGroupMember", "RoleGrant"]


# Allowed ``role_grant.grant_role`` values, enforced by a CHECK
# constraint. Matches §02 "role_grants" — the v1 enum drops the v0
# ``owner`` value in favour of the ``owners`` permission group (§02
# §"permission_group"). ``guest`` is reserved but allowed in the
# schema for forward-compat.
_GRANT_ROLE_VALUES: tuple[str, ...] = ("manager", "worker", "client", "guest")


class PermissionGroup(Base):
    """Named set of users on a workspace, for granting authority.

    v1 slice: carries the owners-group governance anchor plus the
    display label + capabilities payload. Organization-scope groups
    and the ``group_kind`` / ``is_derived`` split land with cd-zkr.
    """

    __tablename__ = "permission_group"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Stable slug within the workspace. System-group values are
    # ``owners`` (seeded on workspace creation) and — when cd-zkr
    # lands — ``managers`` / ``all_workers`` / ``all_clients``.
    slug: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Flat mapping of capability-name → enabled-flag (or a nested
    # policy blob). The outer ``Any`` is scoped to SQLAlchemy's JSON
    # column type — callers writing a typed payload should use a
    # TypedDict locally and coerce into this column.
    capabilities_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "slug", name="uq_permission_group_workspace_slug"
        ),
    )


class PermissionGroupMember(Base):
    """Explicit (group, user) membership row.

    Composite PK on ``(group_id, user_id)``. ``workspace_id`` is
    denormalised so the ORM tenant filter
    (:mod:`app.tenancy.orm_filter`) can enforce workspace boundaries
    on joins that only touch this table — without the column the
    walker has no ``workspace_id`` predicate to inject.
    """

    __tablename__ = "permission_group_member"

    group_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("permission_group.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # ``added_by_user_id`` is NULL for the self-bootstrap row seeded
    # at workspace creation (there is no prior actor); every other
    # membership write records the acting user for audit.
    added_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (Index("ix_permission_group_member_workspace", "workspace_id"),)


class RoleGrant(Base):
    """Surface / persona grant: one row per ``(user, scope, grant_role)``.

    ``scope_property_id`` is nullable — NULL means the grant applies
    workspace-wide, non-NULL narrows it to a single property. The
    column is a **soft reference** for now; the ``property`` table
    lands with cd-i6u, whose migration promotes this into a real FK.

    The ``grant_role`` CHECK constraint (``manager | worker | client
    | guest``) matches §02's v1 enum and replaces the v0 ``owner``
    value — governance now lives on the ``owners`` permission group
    (see :class:`PermissionGroup`).
    """

    __tablename__ = "role_grant"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    grant_role: Mapped[str] = mapped_column(String, nullable=False)
    scope_property_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # ``created_by_user_id`` is NULL for the self-grant emitted at
    # workspace creation (there is no prior actor); every other
    # role-grant write records the acting user for audit.
    created_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "grant_role IN ('" + "', '".join(_GRANT_ROLE_VALUES) + "')",
            name="grant_role",
        ),
        Index("ix_role_grant_workspace_user", "workspace_id", "user_id"),
        Index("ix_role_grant_scope_property", "scope_property_id"),
    )
