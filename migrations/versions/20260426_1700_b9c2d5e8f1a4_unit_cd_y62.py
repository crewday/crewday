"""unit_cd_y62

Revision ID: b9c2d5e8f1a4
Revises: a8b1c4d7e0f3
Create Date: 2026-04-26 17:00:00.000000

Extends ``unit`` with the richer §02 / §04 columns the unit domain
service (cd-y62) needs. The v1 slice (cd-i6u, migration
``89ebd89b9de4``) landed only ``label`` / ``type`` / ``capacity`` —
the minimum a downstream context might want to know about a sub-
property bookable. This migration adds the spec-level fields the
manager UI and the ``UnitCreate`` / ``UnitView`` DTOs reference:

* ``name`` — human-visible name ("Room 1", "Apt 3B"). v1 rows had
  no ``name``; we backfill from ``label`` so the existing list
  surface keeps showing the same string after the migration. The
  domain service writes a non-blank value on every insert.
* ``ordinal`` — integer display order among siblings (§04 "Unit"
  fields). The default unit auto-created at property bootstrap is
  always ``ordinal = 0``.
* ``default_checkin_time`` / ``default_checkout_time`` — per-unit
  override of the property's check-in/out window (§04 "Unit").
  Nullable; null = inherit from property.
* ``max_guests`` — soft capacity cap. Nullable; null = no limit.
  (Distinct from the v1 ``capacity`` column which carries the
  physical-kind capacity; ``max_guests`` is the bookable cap the
  guest welcome page surfaces.)
* ``welcome_overrides_json`` — per-unit overrides that merge over
  the property's ``welcome_defaults_json`` at render time (§04
  "Welcome overrides merge"). Empty object by default; the merge
  is a no-op when the unit carries no override.
* ``settings_override_json`` — per-unit cascade layer between
  property and work_engagement (§02 "Settings cascade"). Empty
  object by default.
* ``notes_md`` — internal staff-visible notes. Empty string by
  default.
* ``updated_at`` — mutation timestamp. Nullable + backfilled from
  ``created_at`` so reads never see ``NULL``. The domain service
  always writes it on insert + update.
* ``deleted_at`` — soft-delete marker. Nullable; live rows carry
  ``NULL``. Every domain-level list honours it via the ``deleted``
  filter.

The legacy ``label`` / ``type`` / ``capacity`` columns survive the
migration for back-compat — adapters that read them directly keep
working. The new domain service routes through ``name`` / ``ordinal``
exclusively; ``label`` is relaxed to nullable so the service can
write rows without populating the legacy column. A future cleanup
pass (out of scope for cd-y62) may drop the v1 columns once every
caller has migrated.

**Uniqueness.** §04 "Invariants" lists
``UNIQUE(property_id, name) WHERE deleted_at IS NULL`` — the
window is per-property, not per-(workspace, property), because
with multi-belonging the same physical property may belong to
several workspaces and every linked workspace sees the same unit
list. Workspace scoping flows through ``property_workspace``
joined on the parent ``property_id`` (mirroring the property
service's pattern). The migration creates the partial UNIQUE
``uq_unit_property_name_active`` excluding tombstoned rows — the
domain service performs a pre-flight SELECT to surface a clear
``UnitNameTaken`` error before the partial UNIQUE fires.

**All new columns are nullable or carry server defaults** so
existing rows survive the migration without bespoke backfill —
the only data fix-ups are the ``name`` and ``updated_at``
backfills below.

**Reversibility.** ``downgrade()`` drops every added column and
the partial UNIQUE. Data in the added columns is discarded —
acceptable for a rollback of a feature extension on a dev
database. ``label`` is restored to NOT NULL; an operator running
a real rollback must guarantee every row has a non-null ``label``
first.

See ``docs/specs/02-domain-model.md`` §"unit",
``docs/specs/04-properties-and-stays.md`` §"Unit" /
§"Welcome overrides merge".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9c2d5e8f1a4"
down_revision: str | Sequence[str] | None = "a8b1c4d7e0f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("unit", schema=None) as batch_op:
        # Display / identity columns. ``name`` added nullable so pre-
        # existing rows survive; we backfill from ``label`` below.
        batch_op.add_column(sa.Column("name", sa.String(), nullable=True))

        # Display order among siblings. Default 0 keeps legacy rows
        # readable; the domain service always writes a value on insert.
        batch_op.add_column(
            sa.Column(
                "ordinal",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )

        # Per-unit check-in/out overrides. Stored as ISO-8601 ``HH:MM``
        # text; the domain service parses + revalidates on read. Using
        # text rather than a TIME column keeps SQLite + Postgres in
        # sync (SQLite's TIME affinity is a thin layer over TEXT).
        batch_op.add_column(
            sa.Column("default_checkin_time", sa.String(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("default_checkout_time", sa.String(), nullable=True)
        )

        # Bookable cap surfaced on the guest welcome page. Distinct
        # from v1 ``capacity`` (physical-kind cap).
        batch_op.add_column(sa.Column("max_guests", sa.Integer(), nullable=True))

        # Welcome overrides + per-unit settings cascade payloads.
        batch_op.add_column(
            sa.Column(
                "welcome_overrides_json",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )
        batch_op.add_column(
            sa.Column(
                "settings_override_json",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )

        # Internal staff-visible notes. Empty string by default so the
        # column is non-null without forcing a backfill.
        batch_op.add_column(
            sa.Column(
                "notes_md",
                sa.String(),
                nullable=False,
                server_default="",
            )
        )

        # Timestamps. ``updated_at`` nullable so the migration is cheap
        # on a large table; the backfill below pins it to ``created_at``.
        batch_op.add_column(
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
        )

        # Relax the legacy ``label`` column to nullable so the new
        # domain service can write rows without populating it. The
        # column survives so existing adapters keep reading it; future
        # cleanup may drop it entirely.
        batch_op.alter_column(
            "label",
            existing_type=sa.String(),
            nullable=True,
        )
        # Relax the legacy ``type`` column to nullable for the same
        # reason. The CHECK constraint moves to "value-or-NULL" so a
        # blank physical-kind no longer fails the insert; the existing
        # taxonomy values are still rejected at the boundary.
        batch_op.alter_column(
            "type",
            existing_type=sa.String(),
            nullable=True,
        )
        # Replace the v1 CHECK with the "NULL OR allowed value" form.
        # The v1 migration named the constraint via ``op.f("ck_unit_type")``;
        # the raw body passed to ``create_check_constraint`` was
        # ``"type"`` and Alembic's naming convention rendered it as
        # ``ck_unit_type``. Drop + recreate keeps the same final name
        # so the downgrade path stays symmetric.
        batch_op.drop_constraint("type", type_="check")
        batch_op.create_check_constraint(
            "type",
            "type IS NULL OR type IN ('apartment', 'studio', 'room', "
            "'bungalow', 'villa', 'other')",
        )

        # Index on ``deleted_at`` for the common "live list" query:
        # ``WHERE deleted_at IS NULL``.
        batch_op.create_index(
            "ix_unit_deleted",
            ["deleted_at"],
            unique=False,
        )

    # Partial UNIQUE on ``(property_id, name)`` excluding tombstoned
    # rows. The textual §04 invariant ("no two live units in the
    # same property share a name") is what matters; a re-create after
    # a soft-delete must mint a fresh row without colliding with the
    # historical one. Both SQLite + Postgres support partial indexes.
    op.create_index(
        "uq_unit_property_name_active",
        "unit",
        ["property_id", "name"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # Backfill the renamed / derived columns so existing rows stay
    # readable through the new service surface. ``label`` survives
    # untouched; we copy it into ``name`` so the manager UI keeps
    # showing the same string after the migration.
    op.execute("UPDATE unit SET name = label WHERE name IS NULL")
    op.execute("UPDATE unit SET updated_at = created_at WHERE updated_at IS NULL")


def downgrade() -> None:
    """Downgrade schema.

    Drops every added column + the partial UNIQUE. Restores ``label``
    to NOT NULL — operators must guarantee every row has a non-null
    ``label`` before running this path. Data in the added columns is
    discarded; acceptable for a rollback of a feature extension on a
    dev database.
    """
    op.drop_index("uq_unit_property_name_active", table_name="unit")
    with op.batch_alter_table("unit", schema=None) as batch_op:
        batch_op.drop_index("ix_unit_deleted")
        # Restore the v1 CHECK on ``type``. Operators must guarantee
        # every row has a non-null ``type`` before this path runs.
        # See upgrade for the Alembic naming-convention rationale.
        batch_op.drop_constraint("type", type_="check")
        batch_op.create_check_constraint(
            "type",
            "type IN ('apartment', 'studio', 'room', 'bungalow', 'villa', 'other')",
        )
        batch_op.alter_column(
            "type",
            existing_type=sa.String(),
            nullable=False,
        )
        batch_op.alter_column(
            "label",
            existing_type=sa.String(),
            nullable=False,
        )
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("notes_md")
        batch_op.drop_column("settings_override_json")
        batch_op.drop_column("welcome_overrides_json")
        batch_op.drop_column("max_guests")
        batch_op.drop_column("default_checkout_time")
        batch_op.drop_column("default_checkin_time")
        batch_op.drop_column("ordinal")
        batch_op.drop_column("name")
