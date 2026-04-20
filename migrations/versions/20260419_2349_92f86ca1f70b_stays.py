"""stays

Revision ID: 92f86ca1f70b
Revises: 200bedec0eed
Create Date: 2026-04-19 23:49:42.451594

Creates the three stays tables that back the external-calendar →
reservation → turnover-bundle chain (see
``docs/specs/02-domain-model.md`` §"reservation", §"ical_feed",
§"stay_bundle" and ``docs/specs/04-properties-and-stays.md``
§"Stay (reservation)" / §"iCal feed"):

* ``ical_feed`` — the operator-supplied URL the poller ingests.
  Registered as workspace-scoped via
  ``app/adapters/db/stays/__init__.py`` so the ORM tenant filter
  auto-injects a ``workspace_id`` predicate. ``provider`` CHECK
  enforces the v1 channel enum (``airbnb | vrbo | booking |
  custom``). FK cascades on ``property`` so deleting a property
  sweeps its feeds. The ``(workspace_id, property_id)`` index
  powers the "list feeds per property" read path.

* ``reservation`` — the ingested or manually-entered stay. FK to
  ``ical_feed`` uses ``SET NULL`` so a reservation captured via
  iCal outlives the feed's deletion (agency swaps provider; the
  booking remains real work). ``status`` CHECK (``scheduled |
  checked_in | completed | cancelled``) and ``source`` CHECK
  (``ical | manual | api``) land the v1 lifecycle. ``check_out >
  check_in`` guards against zero-or-negative windows. The
  ``(ical_feed_id, external_uid)`` unique composite is what makes
  an iCal re-poll idempotent — the upsert path targets this pair.
  (When ``ical_feed_id IS NULL`` the uniqueness doesn't apply:
  Postgres and SQLite treat NULLs as distinct in unique indexes
  by default, so manual entries with colliding ``external_uid``
  never trip the constraint. OK for v1; the domain layer owns the
  richer §04 "uniqueness by (unit, source, external)" rule.) The
  ``(property_id, check_in)`` index is the per-acceptance
  criterion for "reservations for this property in time order".

* ``stay_bundle`` — group of tasks materialised against a
  reservation. ``kind`` CHECK (``turnover | welcome |
  deep_clean``) enforces the §04 rule-type enum.
  ``tasks_json`` is a list of template-ref + metadata payloads the
  scheduler uses to spawn occurrences (shape-validated in the
  domain layer). FK cascades on ``reservation`` so a cancelled
  booking sweeps its unstarted work.

All three tables are workspace-scoped. Tables are created in FK
dependency order (``ical_feed → reservation → stay_bundle``);
``downgrade()`` drops in reverse.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "92f86ca1f70b"
down_revision: str | Sequence[str] | None = "200bedec0eed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "ical_feed",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_etag", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider IN ('airbnb', 'vrbo', 'booking', 'custom')",
            name=op.f("ck_ical_feed_provider"),
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_ical_feed_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_ical_feed_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ical_feed")),
    )
    with op.batch_alter_table("ical_feed", schema=None) as batch_op:
        batch_op.create_index(
            "ix_ical_feed_workspace_property",
            ["workspace_id", "property_id"],
            unique=False,
        )

    op.create_table(
        "reservation",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=False),
        sa.Column("ical_feed_id", sa.String(), nullable=True),
        sa.Column("external_uid", sa.String(), nullable=False),
        sa.Column("check_in", sa.DateTime(timezone=True), nullable=False),
        sa.Column("check_out", sa.DateTime(timezone=True), nullable=False),
        sa.Column("guest_name", sa.String(), nullable=True),
        sa.Column("guest_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("raw_summary", sa.String(), nullable=True),
        sa.Column("raw_description", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "check_out > check_in",
            name=op.f("ck_reservation_check_out_after_check_in"),
        ),
        sa.CheckConstraint(
            "source IN ('ical', 'manual', 'api')",
            name=op.f("ck_reservation_source"),
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'checked_in', 'completed', 'cancelled')",
            name=op.f("ck_reservation_status"),
        ),
        sa.ForeignKeyConstraint(
            ["ical_feed_id"],
            ["ical_feed.id"],
            name=op.f("fk_reservation_ical_feed_id_ical_feed"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_reservation_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_reservation_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reservation")),
        sa.UniqueConstraint(
            "ical_feed_id",
            "external_uid",
            name="uq_reservation_feed_external_uid",
        ),
    )
    with op.batch_alter_table("reservation", schema=None) as batch_op:
        batch_op.create_index(
            "ix_reservation_property_check_in",
            ["property_id", "check_in"],
            unique=False,
        )

    op.create_table(
        "stay_bundle",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("reservation_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("tasks_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('turnover', 'welcome', 'deep_clean')",
            name=op.f("ck_stay_bundle_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["reservation.id"],
            name=op.f("fk_stay_bundle_reservation_id_reservation"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_stay_bundle_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stay_bundle")),
    )
    with op.batch_alter_table("stay_bundle", schema=None) as batch_op:
        batch_op.create_index(
            "ix_stay_bundle_reservation", ["reservation_id"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("stay_bundle", schema=None) as batch_op:
        batch_op.drop_index("ix_stay_bundle_reservation")
    op.drop_table("stay_bundle")

    with op.batch_alter_table("reservation", schema=None) as batch_op:
        batch_op.drop_index("ix_reservation_property_check_in")
    op.drop_table("reservation")

    with op.batch_alter_table("ical_feed", schema=None) as batch_op:
        batch_op.drop_index("ix_ical_feed_workspace_property")
    op.drop_table("ical_feed")
