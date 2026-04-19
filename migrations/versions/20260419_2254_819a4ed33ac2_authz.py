"""authz

Revision ID: 819a4ed33ac2
Revises: 1dc6908659d3
Create Date: 2026-04-19 22:54:37.736592

Creates the three authz tables that back the permission-group +
role-grant surface (see ``docs/specs/02-domain-model.md``
§"permission_group", §"permission_group_member", §"role_grants" and
``docs/specs/05-employees-and-roles.md`` §"Roles & groups"):

* ``permission_group`` — named set of users on a workspace. V1 seeds
  exactly one system group per workspace (``owners``) at signup;
  user-defined groups and the ``managers`` / ``all_workers`` /
  ``all_clients`` derived groups land with cd-zkr.

* ``permission_group_member`` — explicit ``(group, user)``
  membership junction. ``workspace_id`` is denormalised so the ORM
  tenant filter (:mod:`app.tenancy.orm_filter`) can inject a
  workspace predicate on queries that only touch this table.

* ``role_grant`` — surface / persona grant anchoring the manager /
  worker / client shell a user sees on a given scope. The v1 enum
  drops the v0 ``owner`` value (governance moves to the ``owners``
  permission group); a CHECK constraint enforces the four allowed
  values.

All three tables CASCADE on ``workspace`` and ``user``, so hard-
deleting a workspace or a user sweeps the authz trail atomically.
The ``added_by_user_id`` / ``created_by_user_id`` audit columns
``SET NULL`` on user-delete so the history row survives even if the
granting actor's identity row is later removed.

``role_grant.scope_property_id`` is a **soft reference** in this
migration: the ``property`` table lands with cd-i6u, whose
migration owns the FK promotion. Leaving the column a plain
``String`` keeps this slice self-contained while the column is
still usable (NULL = workspace-wide, a ULID = property-scoped).

All three tables are registered with :mod:`app.tenancy.registry`
via the ``app/adapters/db/authz/__init__.py`` import hook, so the
``do_orm_execute`` filter auto-pins ``workspace_id`` on every ORM
read.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "819a4ed33ac2"
down_revision: str | Sequence[str] | None = "1dc6908659d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "permission_group",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("system", sa.Boolean(), nullable=False),
        sa.Column("capabilities_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_permission_group_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permission_group")),
        sa.UniqueConstraint(
            "workspace_id", "slug", name="uq_permission_group_workspace_slug"
        ),
    )

    op.create_table(
        "role_grant",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("grant_role", sa.String(), nullable=False),
        sa.Column("scope_property_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.String(), nullable=True),
        sa.CheckConstraint(
            "grant_role IN ('manager', 'worker', 'client', 'guest')",
            name=op.f("ck_role_grant_grant_role"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_role_grant_created_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_role_grant_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_role_grant_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_grant")),
    )
    with op.batch_alter_table("role_grant", schema=None) as batch_op:
        batch_op.create_index(
            "ix_role_grant_scope_property", ["scope_property_id"], unique=False
        )
        batch_op.create_index(
            "ix_role_grant_workspace_user",
            ["workspace_id", "user_id"],
            unique=False,
        )

    op.create_table(
        "permission_group_member",
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("added_by_user_id", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["added_by_user_id"],
            ["user.id"],
            name=op.f("fk_permission_group_member_added_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["permission_group.id"],
            name=op.f("fk_permission_group_member_group_id_permission_group"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_permission_group_member_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_permission_group_member_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "group_id", "user_id", name=op.f("pk_permission_group_member")
        ),
    )
    with op.batch_alter_table("permission_group_member", schema=None) as batch_op:
        batch_op.create_index(
            "ix_permission_group_member_workspace", ["workspace_id"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("permission_group_member", schema=None) as batch_op:
        batch_op.drop_index("ix_permission_group_member_workspace")
    op.drop_table("permission_group_member")

    with op.batch_alter_table("role_grant", schema=None) as batch_op:
        batch_op.drop_index("ix_role_grant_workspace_user")
        batch_op.drop_index("ix_role_grant_scope_property")
    op.drop_table("role_grant")

    op.drop_table("permission_group")
