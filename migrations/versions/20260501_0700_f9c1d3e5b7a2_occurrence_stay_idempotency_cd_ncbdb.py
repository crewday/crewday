"""occurrence stay idempotency cd-ncbdb

Revision ID: f9c1d3e5b7a2
Revises: e8b0c2d4f6a9
Create Date: 2026-05-01 07:00:00.000000

Extends ``occurrence`` with the natural-key columns the
:class:`~app.ports.tasks_create_occurrence.TasksCreateOccurrencePort`
SQLAlchemy concretion (cd-ncbdb) keys idempotency on:

* ``reservation_id`` — soft pointer to ``reservation.id``. No FK so
  losing the reservation does not orphan the historical occurrence
  row (the cancellation cascade is the tasks-side service's job, not
  the schema's).
* ``lifecycle_rule_id`` — pinned ``stay_lifecycle_rule.id`` (or the
  in-memory built-in id today; the table lands with cd-1ai). Plain
  String — soft pointer for the same reason as ``reservation_id``.
* ``occurrence_key`` — per-rule occurrence identity for recurring
  rules (e.g. ``during_stay:0``, ``during_stay:1``). Optional;
  falls back to ``''`` in the partial unique index so single-shot
  rules without an explicit key still dedup on
  ``(reservation_id, lifecycle_rule_id)``.

All three columns are nullable; only stay-driven occurrences carry
them. Pre-existing schedule-driven and one-off occurrences leave them
``NULL`` so this migration is a no-op for the cd-22e / cd-0rf rows
already in the database.

**Idempotency guard.** Adds the partial unique index

    UNIQUE(workspace_id, reservation_id, lifecycle_rule_id, occurrence_key)
        WHERE reservation_id IS NOT NULL AND state != 'cancelled'

matching the
:mod:`app.ports.tasks_create_occurrence` "Idempotency contract":
``(reservation_id, rule_id, occurrence_key)`` is the dedup key for
the **live** row. ``workspace_id`` leads the index for tenant
locality and to satisfy the ORM tenant filter's predicate. Scoped
to ``reservation_id IS NOT NULL`` so non-stay occurrences do not
trip the unique on ``NULL`` keys; scoped to
``state != 'cancelled'`` so a regenerate flow can cancel the
existing row and insert a fresh one carrying the same triple
without colliding (the cancelled tombstone keeps its key for
audit; only one live row per triple). Both SQLite (3.24+) and
PostgreSQL honour partial unique indexes via the dialect-specific
``_where`` kwargs.

**Reversibility.** ``downgrade()`` drops the index and the three
columns. Data in the added columns is discarded; idempotency on
stay-driven occurrences relies on the live application stack and
must be reasserted before the next upgrade.

See ``docs/specs/04-properties-and-stays.md`` §"Stay task bundles",
``docs/specs/06-tasks-and-scheduling.md`` §"Task row", and the
"Idempotency contract" docstring on
:mod:`app.ports.tasks_create_occurrence`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f9c1d3e5b7a2"
down_revision: str | Sequence[str] | None = "e8b0c2d4f6a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reservation_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("lifecycle_rule_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("occurrence_key", sa.String(), nullable=True))

    # Partial unique index. ``batch_alter_table`` does not pass the
    # dialect ``_where`` kwargs through SQLite's table-copy path, so
    # the index is emitted at the top level. SQLite 3.24+ and
    # PostgreSQL both accept the predicate; the ``sqlite_where`` /
    # ``postgresql_where`` kwargs select the dialect-specific
    # rendering. ``COALESCE(occurrence_key, '')`` would let two NULL
    # keys collide, so we keep ``occurrence_key`` raw and let the
    # adapter normalise to an empty string before insert.
    op.create_index(
        "uq_occurrence_reservation_rule_key",
        "occurrence",
        ["workspace_id", "reservation_id", "lifecycle_rule_id", "occurrence_key"],
        unique=True,
        sqlite_where=sa.text("reservation_id IS NOT NULL AND state != 'cancelled'"),
        postgresql_where=sa.text("reservation_id IS NOT NULL AND state != 'cancelled'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_occurrence_reservation_rule_key", table_name="occurrence")
    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        batch_op.drop_column("occurrence_key")
        batch_op.drop_column("lifecycle_rule_id")
        batch_op.drop_column("reservation_id")
