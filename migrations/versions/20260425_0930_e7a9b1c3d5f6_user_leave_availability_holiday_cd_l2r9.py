"""user_leave + availability + holiday cd-l2r9

Revision ID: e7a9b1c3d5f6
Revises: d6f8a0c2b5e7
Create Date: 2026-04-25 09:30:00.000000

Lands the four §06 availability tables that the assignment algorithm
consumes via the "Availability precedence stack":

* ``user_leave`` — one-off date-range leave per (workspace, user).
  Five-bucket ``category`` enum, soft-delete tombstone, range CHECK.
* ``user_weekly_availability`` — standing per (workspace, user,
  weekday) pattern. **No soft delete** — there is one live row per
  (workspace, user, weekday); editing overwrites in place. ISO
  weekday CHECK plus BOTH-OR-NEITHER hours pairing CHECK.
* ``user_availability_override`` — date-specific override of the
  weekly pattern. ``UNIQUE(workspace_id, user_id, date)`` plus the
  same BOTH-OR-NEITHER hours pairing CHECK.
* ``public_holiday`` — workspace-managed holiday with scheduling
  effect (``block | allow | reduced``), reduced-hours pairing CHECK,
  optional ``payroll_multiplier`` (Numeric(5, 2)), optional
  ``recurrence`` (NULL or ``annual``), and ``UNIQUE(workspace_id,
  date, country)``.

All four are workspace-scoped — registered in their package's
``__init__`` so the ORM tenant filter rides each row's local
``workspace_id`` column. ``user_id`` and ``approved_by`` are plain
``String`` columns (soft references) until the broader tenancy-join
refactor lands; same idiom as ``user_workspace.user_id`` /
``work_engagement.user_id`` / ``work_engagement.supplier_org_id``.

CHECK / FK / UNIQUE constraints land with the table so SQLite
materialises them in the initial ``CREATE TABLE`` rather than forcing
a table-copy via ``batch_alter_table``. Hot-path indexes are emitted
via ``batch_alter_table`` for parity with the sibling cd-4saj /
cd-e4m3 migrations. There is no partial-UNIQUE here (the v1 spec
doesn't require ``WHERE deleted_at IS NULL`` on these tables — the
soft-delete filter lives in the service layer); the standard
``UniqueConstraint`` is enough.

**Reversibility.** ``downgrade()`` drops the four tables in reverse
creation order (the only inter-table FK is from each new table to
``workspace.id``, which dies with the table). The CHECK / UNIQUE /
FK constraints disappear with the tables. No data-loss concern
beyond the obvious "rolling back drops the availability catalogue" —
a real rollback should dump the four tables first.

See ``docs/specs/06-tasks-and-scheduling.md`` §"user_leave",
§"Weekly availability", §"user_availability_overrides",
§"public_holidays"; ``docs/specs/02-domain-model.md`` §"Work" entity
list (updated by the same change to use the ``user_*`` table names);
``docs/specs/05-employees-and-roles.md`` §"Property work role
assignment" (consumer of the rota composition).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7a9b1c3d5f6"
down_revision: str | Sequence[str] | None = "d6f8a0c2b5e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ``user_leave`` — one-off leave per (workspace, user). Five-bucket
    # category enum + range CHECK + soft-delete tombstone.
    op.create_table(
        "user_leave",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("note_md", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "category IN ('vacation', 'sick', 'personal', 'bereavement', 'other')",
            name=op.f("ck_user_leave_category"),
        ),
        sa.CheckConstraint(
            "ends_on >= starts_on",
            name=op.f("ck_user_leave_range"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_user_leave_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_leave")),
    )
    with op.batch_alter_table("user_leave", schema=None) as batch_op:
        batch_op.create_index(
            "ix_user_leave_workspace_user",
            ["workspace_id", "user_id"],
            unique=False,
        )

    # ``user_weekly_availability`` — standing per (workspace, user,
    # weekday) pattern. ISO weekday range CHECK + BOTH-OR-NEITHER
    # hours pairing CHECK + UNIQUE on the (workspace, user, weekday)
    # triple. **No soft delete** — exactly one live row per triple.
    op.create_table(
        "user_weekly_availability",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("starts_local", sa.Time(), nullable=True),
        sa.Column("ends_local", sa.Time(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "weekday >= 0 AND weekday <= 6",
            name=op.f("ck_user_weekly_availability_weekday_range"),
        ),
        # BOTH-OR-NEITHER — the §06 invariant. Half-set pairs are
        # rejected at the DB so a half-wired pattern can't reach the
        # assignment algorithm.
        sa.CheckConstraint(
            "(starts_local IS NULL AND ends_local IS NULL) "
            "OR (starts_local IS NOT NULL AND ends_local IS NOT NULL)",
            name=op.f("ck_user_weekly_availability_hours_pairing"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_user_weekly_availability_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_weekly_availability")),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            "weekday",
            name=op.f("uq_user_weekly_availability_user_weekday"),
        ),
    )
    with op.batch_alter_table("user_weekly_availability", schema=None) as batch_op:
        # "What's this user's full week?" — the worker /schedule view
        # scans the seven rows under one tenant-local prefix.
        batch_op.create_index(
            "ix_user_weekly_availability_workspace_user",
            ["workspace_id", "user_id"],
            unique=False,
        )

    # ``user_availability_override`` — date-specific override. Same
    # BOTH-OR-NEITHER hours pairing CHECK as the weekly pattern, plus
    # ``UNIQUE(workspace_id, user_id, date)`` and a soft-delete
    # tombstone.
    op.create_table(
        "user_availability_override",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("starts_local", sa.Time(), nullable=True),
        sa.Column("ends_local", sa.Time(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("approval_required", sa.Boolean(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        # Same biconditional as the weekly pattern — half-set pairs
        # are a half-wired override.
        sa.CheckConstraint(
            "(starts_local IS NULL AND ends_local IS NULL) "
            "OR (starts_local IS NOT NULL AND ends_local IS NOT NULL)",
            name=op.f("ck_user_availability_override_hours_pairing"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_user_availability_override_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_availability_override")),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            "date",
            name=op.f("uq_user_availability_override_user_date"),
        ),
    )
    with op.batch_alter_table("user_availability_override", schema=None) as batch_op:
        batch_op.create_index(
            "ix_user_availability_override_workspace_user",
            ["workspace_id", "user_id"],
            unique=False,
        )

    # ``public_holiday`` — workspace-managed holiday. Three CHECKs:
    # scheduling-effect enum, recurrence enum (NULL-or-``annual``),
    # reduced-hours biconditional. ``UNIQUE(workspace_id, date,
    # country)`` plus two hot-path indexes for the candidate-pool +
    # live-list views.
    op.create_table(
        "public_holiday",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("country", sa.String(), nullable=True),
        sa.Column("scheduling_effect", sa.String(), nullable=False),
        sa.Column("reduced_starts_local", sa.Time(), nullable=True),
        sa.Column("reduced_ends_local", sa.Time(), nullable=True),
        sa.Column("payroll_multiplier", sa.Numeric(5, 2), nullable=True),
        sa.Column("recurrence", sa.String(), nullable=True),
        sa.Column("notes_md", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scheduling_effect IN ('block', 'allow', 'reduced')",
            name=op.f("ck_public_holiday_scheduling_effect"),
        ),
        # NULL-or-enum — the v1 spec admits ``annual`` only. Future
        # values land via a widened CHECK without a column rename.
        sa.CheckConstraint(
            "recurrence IS NULL OR recurrence IN ('annual')",
            name=op.f("ck_public_holiday_recurrence"),
        ),
        # Reduced-hours pairing — same biconditional shape as
        # ``ck_work_engagement_supplier_org_pairing`` (cd-4saj). The
        # reduced-hours columns are populated iff
        # ``scheduling_effect = 'reduced'``.
        sa.CheckConstraint(
            "(scheduling_effect = 'reduced' "
            "AND reduced_starts_local IS NOT NULL "
            "AND reduced_ends_local IS NOT NULL) "
            "OR (scheduling_effect != 'reduced' "
            "AND reduced_starts_local IS NULL "
            "AND reduced_ends_local IS NULL)",
            name=op.f("ck_public_holiday_reduced_hours_pairing"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_public_holiday_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_public_holiday")),
        sa.UniqueConstraint(
            "workspace_id",
            "date",
            "country",
            name=op.f("uq_public_holiday_workspace_date_country"),
        ),
    )
    with op.batch_alter_table("public_holiday", schema=None) as batch_op:
        # "What holidays fall on this date in this workspace?" —
        # candidate-pool walk for the §06 availability stack.
        batch_op.create_index(
            "ix_public_holiday_workspace_date",
            ["workspace_id", "date"],
            unique=False,
        )
        # "List live holidays for this workspace" hot path.
        batch_op.create_index(
            "ix_public_holiday_workspace_deleted",
            ["workspace_id", "deleted_at"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema.

    Drop the four tables in reverse creation order. Indexes / CHECKs /
    UNIQUEs / FKs disappear with each table. No data-loss concern
    beyond "rolling back drops the availability catalogue" — a real
    rollback should dump the tables first.
    """
    with op.batch_alter_table("public_holiday", schema=None) as batch_op:
        batch_op.drop_index("ix_public_holiday_workspace_deleted")
        batch_op.drop_index("ix_public_holiday_workspace_date")
    op.drop_table("public_holiday")

    with op.batch_alter_table("user_availability_override", schema=None) as batch_op:
        batch_op.drop_index("ix_user_availability_override_workspace_user")
    op.drop_table("user_availability_override")

    with op.batch_alter_table("user_weekly_availability", schema=None) as batch_op:
        batch_op.drop_index("ix_user_weekly_availability_workspace_user")
    op.drop_table("user_weekly_availability")

    with op.batch_alter_table("user_leave", schema=None) as batch_op:
        batch_op.drop_index("ix_user_leave_workspace_user")
    op.drop_table("user_leave")
