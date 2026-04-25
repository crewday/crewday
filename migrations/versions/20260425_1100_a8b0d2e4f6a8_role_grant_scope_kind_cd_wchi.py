"""role_grant_scope_kind_cd_wchi

Revision ID: a8b0d2e4f6a8
Revises: f8a0c2b5e7d9
Create Date: 2026-04-25 11:00:00.000000

Adds the **deployment scope** to ``role_grant`` so the admin surface
(§12 "Admin") can authorise its callers via *any active*
``role_grant`` row with ``scope_kind = 'deployment'``. Today the table
is workspace-scoped (``workspace_id NOT NULL``) and cannot represent a
grant that lives at the bare-host level — every deployment endpoint
under ``/admin/api/v1/...`` is blocked on that.

Shape changes (matches §02 ``role_grants`` and §05 "Admin team"):

* ``scope_kind`` text NOT NULL DEFAULT ``'workspace'`` —
  ``CHECK (scope_kind IN ('workspace', 'deployment'))``. Backfilled
  to ``'workspace'`` for every existing row, then the server-default
  is dropped so future writes must declare the value explicitly.
* ``workspace_id`` widened to **NULLABLE**. The new pairing CHECK
  ``(scope_kind='deployment' AND workspace_id IS NULL) OR
  (scope_kind='workspace' AND workspace_id IS NOT NULL)`` enforces
  the biconditional invariant: a deployment grant carries no
  workspace, a workspace grant carries one.
* Partial UNIQUE ``uq_role_grant_deployment_user_role`` on
  ``(user_id, grant_role) WHERE scope_kind='deployment'`` — at most
  one active deployment grant per ``(user, role)`` pair. Existing
  workspace-scope behaviour stays as-is (no app-level uniqueness was
  ever expressed on ``(workspace_id, user_id, grant_role)`` and
  re-grants stay history-preserving per §02 "Revocation").

The ``grant_role`` CHECK is left untouched — the v1 enum
(``manager | worker | client | guest``) already covers the deployment
admin surface (§05 "Permissions" §"Deployment groups"): a deployment
admin holds ``grant_role='manager'`` at ``scope_kind='deployment'``;
ownership is modelled via the deployment ``owners`` permission group
(seeded by a follow-up task per the cd-wchi brief).

**Backfill safety.** The new column lands with a server-default of
``'workspace'`` so the ``ALTER TABLE … ADD COLUMN`` step does not
leave existing rows with NULL. We follow up with an explicit UPDATE
to make the backfill defensive against future server-default
removals, then drop the server-default so the application is forced
to declare ``scope_kind`` on every insert.

**Reversibility.** ``downgrade()`` reverses all four steps. Any rows
that were inserted with ``scope_kind='deployment'`` are deleted on
downgrade — they cannot survive a ``workspace_id NOT NULL`` schema.
The drop happens before the column rewrite so the FK / CHECK fences
do not fight the dialect rewrite. A real production rollback should
dump the deployment-scoped rows first.

See ``docs/specs/02-domain-model.md`` §"role_grants" and
``docs/specs/12-rest-api.md`` §"Admin surface".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8b0d2e4f6a8"
down_revision: str | Sequence[str] | None = "f8a0c2b5e7d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Add ``scope_kind`` with a server-default so the ``ADD COLUMN``
    #    step does not stamp existing rows with NULL. We drop the
    #    server-default below — production callers must declare the
    #    column on every write.
    with op.batch_alter_table("role_grant", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "scope_kind",
                sa.String(),
                nullable=False,
                server_default="workspace",
            )
        )

    # 2. Defensive backfill — every legacy row is workspace-scoped.
    #    The server-default already covers the ADD COLUMN path; this
    #    guards against future rewrites that drop the default first.
    op.execute("UPDATE role_grant SET scope_kind = 'workspace'")

    # 3. Relax ``workspace_id`` to NULLABLE, drop the server-default on
    #    ``scope_kind`` (every future insert must declare it), and
    #    install both CHECK constraints. Doing this in a single
    #    ``batch_alter_table`` keeps SQLite's table-rebuild atomic.
    with op.batch_alter_table("role_grant", schema=None) as batch_op:
        batch_op.alter_column(
            "workspace_id",
            existing_type=sa.String(),
            nullable=True,
        )
        batch_op.alter_column(
            "scope_kind",
            existing_type=sa.String(),
            existing_nullable=False,
            server_default=None,
        )
        # Enum CHECK on ``scope_kind``. Naming via ``op.f`` so the
        # constraint name renders deterministically under the shared
        # naming convention (``ck_role_grant_scope_kind``).
        batch_op.create_check_constraint(
            "scope_kind",
            "scope_kind IN ('workspace', 'deployment')",
        )
        # Biconditional CHECK pinning workspace_id to scope_kind.
        # Both directions enforced: a deployment grant cannot carry a
        # workspace_id, a workspace grant cannot omit one. The DB-level
        # CHECK is defence-in-depth; the application's domain service
        # is the first line of defence.
        batch_op.create_check_constraint(
            "scope_kind_workspace_pairing",
            "(scope_kind = 'deployment' AND workspace_id IS NULL) "
            "OR (scope_kind = 'workspace' AND workspace_id IS NOT NULL)",
        )

    # 4. Partial UNIQUE — at most one active deployment grant per
    #    ``(user, role)``. Emitted at the top level (not inside the
    #    batch context) because the dialect-specific ``WHERE`` kwargs
    #    do not survive a batch rebuild cleanly — same idiom as the
    #    ``uq_work_engagement_user_workspace_active`` partial UNIQUE
    #    in cd-4saj.
    op.create_index(
        "uq_role_grant_deployment_user_role",
        "role_grant",
        ["user_id", "grant_role"],
        unique=True,
        sqlite_where=sa.text("scope_kind = 'deployment'"),
        postgresql_where=sa.text("scope_kind = 'deployment'"),
    )


def downgrade() -> None:
    """Downgrade schema.

    Reverses every step in :func:`upgrade`:

    1. Delete every deployment-scoped row — they cannot survive a
       ``workspace_id NOT NULL`` schema. A real production rollback
       should dump these rows to a side-table first; on a dev DB
       they were never expected to outlive the migration.
    2. Drop the partial UNIQUE index.
    3. Drop the two CHECK constraints, narrow ``workspace_id`` back
       to NOT NULL, and drop the ``scope_kind`` column itself.
    """
    # 1. Hard-delete any deployment-scoped rows so the narrower
    #    NOT NULL alteration below does not fail on legacy NULLs.
    op.execute("DELETE FROM role_grant WHERE scope_kind = 'deployment'")

    # 2. Drop the partial UNIQUE first — its dialect-specific WHERE
    #    predicate must come off before the column it references is
    #    altered or removed.
    op.drop_index(
        "uq_role_grant_deployment_user_role",
        table_name="role_grant",
    )

    # 3. Drop the CHECKs, narrow workspace_id back, drop scope_kind.
    with op.batch_alter_table("role_grant", schema=None) as batch_op:
        batch_op.drop_constraint("scope_kind_workspace_pairing", type_="check")
        batch_op.drop_constraint("scope_kind", type_="check")
        batch_op.alter_column(
            "workspace_id",
            existing_type=sa.String(),
            nullable=False,
        )
        batch_op.drop_column("scope_kind")
