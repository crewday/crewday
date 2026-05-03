"""role_grant_soft_retire_cd_x1xh

Revision ID: c8e0f4a6b9d3
Revises: b7c9d1e3a4f5
Create Date: 2026-05-02 17:00:00.000000

Lands the §02 ``role_grants`` soft-retire shape on the ``role_grant``
table. v1 stored revocation as a hard DELETE — every read path then
treated "row exists" as "grant active". Per §02 the canonical model
is a soft-retire: revocation writes ``revoked_at`` and keeps the row
for audit; re-granting the same ``(user, scope_kind, scope_id,
grant_role)`` triple lands a fresh row with a new ``id``, with the
old one preserved.

Shape changes (§02 "role_grants"):

* ``revoked_at`` ``timestamptz NULL`` — populated when the grant is
  revoked. Backed by :class:`app.adapters.db._columns.UtcDateTime`
  on the ORM side (cd-xma93) so Postgres + SQLite round-trip
  tz-aware UTC consistently.
* ``started_on`` ``date NULL`` — when the grant takes effect. Today
  every domain mint stamps "now"; the column lands NULLable so the
  backfill leaves legacy rows alone (NULL == "active since the row
  was created" — read paths consult ``created_at`` for the audit
  timeline). Future mints stamp it explicitly.
* ``ended_on`` ``date NULL`` — paired with ``revoked_at`` on the
  revoke path; an ``ended_on`` in the past without a ``revoked_at``
  is treated as "lapsed" by future read paths.
* ``revoked_by_user_id`` ``ULID FK?`` to ``user.id`` ON DELETE
  ``SET NULL`` — audit-shape mirror of ``created_by_user_id``.

Index changes (§02 "Primary key" partial-PK shape):

* New partial UNIQUE ``uq_role_grant_workspace_user_role_scope_active``
  on ``(workspace_id, user_id, grant_role, scope_property_id) WHERE
  revoked_at IS NULL AND scope_kind='workspace'`` — at most one live
  workspace-scoped grant per ``(user, role, property?)`` tuple.
  Re-grants land a fresh row only after the prior row was revoked.
  The existing workspace-side surface had no app-level uniqueness;
  duplicate live rows were never legal but never enforced either —
  this index makes the invariant material.
* Existing partial UNIQUE ``uq_role_grant_deployment_user_role`` on
  ``(user_id, grant_role) WHERE scope_kind='deployment'`` is
  rewritten to add ``AND revoked_at IS NULL`` — without that filter
  a re-grant after revoke would 409 against the dead row. Same
  deployment-scope semantics, now soft-retire-aware.

**Backfill safety.** Every legacy row is "active by construction"
(v1 == hard-DELETE); the migration leaves them with ``revoked_at IS
NULL`` so the new partial UNIQUE accepts every currently-live row
unchanged. Tests that previously hard-deleted to simulate revocation
extend to a soft-revoke + asserting the row still exists.

**Reversibility.** ``downgrade()`` reverses all four steps. Any rows
that hold a non-NULL ``revoked_at`` at downgrade time are
hard-deleted before the column drop — they cannot survive the
narrower v1 schema where revocation == row-absent. A real production
rollback should dump those rows to a side-table first; on the dev
DB they were never expected to outlive the migration.

SQLite path uses :func:`alembic.op.batch_alter_table` for the column
adds and the column drops; the partial-UNIQUE indexes stay at the
top level so the dialect-specific ``WHERE`` kwargs survive intact
(same idiom as cd-wchi's ``uq_role_grant_deployment_user_role``).

See ``docs/specs/02-domain-model.md`` §"role_grants" / §"Revocation"
and the cd-x1xh task brief.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.adapters.db._columns import UtcDateTime

# revision identifiers, used by Alembic.
revision: str = "c8e0f4a6b9d3"
down_revision: str | Sequence[str] | None = "b7c9d1e3a4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Add the four soft-retire columns. ``revoked_at`` rides
    #    :class:`UtcDateTime` so the ORM read path returns a tz-aware
    #    UTC datetime regardless of dialect (cd-xma93). Every column
    #    is NULL on legacy rows — the backfill is "every existing row
    #    is active" by construction.
    with op.batch_alter_table("role_grant", schema=None) as batch_op:
        batch_op.add_column(sa.Column("revoked_at", UtcDateTime(), nullable=True))
        batch_op.add_column(sa.Column("started_on", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("ended_on", sa.Date(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "revoked_by_user_id",
                sa.String(),
                sa.ForeignKey("user.id", ondelete="SET NULL"),
                nullable=True,
            )
        )

    # 2. Drop the existing deployment-scope partial UNIQUE so it can
    #    be re-created with the ``revoked_at IS NULL`` filter. Without
    #    that filter a re-grant after revoke would 409 against the
    #    soft-retired row.
    op.drop_index(
        "uq_role_grant_deployment_user_role",
        table_name="role_grant",
    )

    # 3. Re-create the deployment-scope partial UNIQUE with the
    #    soft-retire filter. Same shape as cd-wchi but the WHERE now
    #    includes ``revoked_at IS NULL``.
    op.create_index(
        "uq_role_grant_deployment_user_role",
        "role_grant",
        ["user_id", "grant_role"],
        unique=True,
        sqlite_where=sa.text("scope_kind = 'deployment' AND revoked_at IS NULL"),
        postgresql_where=sa.text("scope_kind = 'deployment' AND revoked_at IS NULL"),
    )

    # 4. Workspace-scope partial UNIQUE — at most one **live**
    #    ``(workspace_id, user_id, grant_role, scope_property_id)``
    #    row at a time. Workspace-scoped re-grants now stay
    #    history-preserving via the soft-retired prior row instead of
    #    a hard DELETE; the active row is the one with
    #    ``revoked_at IS NULL``. The dialect-specific ``WHERE`` kwargs
    #    are emitted at the top level (not inside batch) so SQLite
    #    keeps the predicate intact during the rebuild — same idiom
    #    as cd-wchi's deployment-scope partial UNIQUE.
    #
    #    ``scope_property_id`` is nullable, and standard SQL
    #    NULL-distinct semantics would let two live workspace-wide
    #    grants ``(ws, u, role, NULL)`` slip through. ``COALESCE(...,
    #    '')`` collapses the NULL case to a sentinel inside the index
    #    so uniqueness binds across both NULL and non-NULL property
    #    scopes. SQLite + Postgres both support indexed expressions;
    #    the ORM mirror in ``app/adapters/db/authz/models.py`` uses
    #    ``func.coalesce`` so :func:`tools.alembic.check` round-trips.
    op.create_index(
        "uq_role_grant_workspace_user_role_scope_active",
        "role_grant",
        [
            "workspace_id",
            "user_id",
            "grant_role",
            sa.text("COALESCE(scope_property_id, '')"),
        ],
        unique=True,
        sqlite_where=sa.text("scope_kind = 'workspace' AND revoked_at IS NULL"),
        postgresql_where=sa.text("scope_kind = 'workspace' AND revoked_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema.

    1. Hard-delete every soft-revoked row — they cannot survive a
       schema where revocation == row-absent. Production rollback
       should dump these rows to a side-table first; on a dev DB
       they were never expected to outlive the migration.
    2. Drop the workspace-scope partial UNIQUE.
    3. Drop the deployment-scope partial UNIQUE and re-create it
       without the ``revoked_at IS NULL`` filter (the cd-wchi shape).
    4. Drop the four soft-retire columns inside a batch_alter_table.
    """
    # 1. Soft-revoked rows cannot survive the narrower schema; drop
    #    them before the column rewrite so the FK + CHECK fences are
    #    not asked to validate orphans.
    op.execute("DELETE FROM role_grant WHERE revoked_at IS NOT NULL")

    # 2. Drop the workspace-scope partial UNIQUE first — the index
    #    references columns that survive the downgrade so its
    #    drop-order is purely dialect hygiene.
    op.drop_index(
        "uq_role_grant_workspace_user_role_scope_active",
        table_name="role_grant",
    )

    # 3. Re-shape the deployment-scope partial UNIQUE back to the
    #    cd-wchi form (no ``revoked_at IS NULL`` filter).
    op.drop_index(
        "uq_role_grant_deployment_user_role",
        table_name="role_grant",
    )
    op.create_index(
        "uq_role_grant_deployment_user_role",
        "role_grant",
        ["user_id", "grant_role"],
        unique=True,
        sqlite_where=sa.text("scope_kind = 'deployment'"),
        postgresql_where=sa.text("scope_kind = 'deployment'"),
    )

    # 4. Drop the four soft-retire columns. SQLite needs a batch
    #    rebuild for column removal; ``revoked_by_user_id`` carries
    #    a named FK constraint that the rebuild handles transparently.
    with op.batch_alter_table("role_grant", schema=None) as batch_op:
        batch_op.drop_column("revoked_by_user_id")
        batch_op.drop_column("ended_on")
        batch_op.drop_column("started_on")
        batch_op.drop_column("revoked_at")
