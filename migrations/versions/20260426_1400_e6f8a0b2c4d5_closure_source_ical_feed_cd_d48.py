"""closure_source_ical_feed_cd_d48

Revision ID: e6f8a0b2c4d5
Revises: d5e7f9a1b3c4
Create Date: 2026-04-26 14:00:00.000000

Lands the ``property_closure.source_ical_feed_id`` column the iCal
poller (cd-d48) needs to attribute Blocked-pattern closures back to
the upstream feed. §04 "iCal feed" §"Polling behavior" pins the
contract: a VEVENT whose ``SUMMARY`` matches "Not available" /
"Blocked" / "Reserved" lands as a ``property_closure`` row with
``reason = 'ical_unavailable'`` AND ``source_ical_feed_id`` set so
the operator UI can render "this window came from the Airbnb feed"
without joining audit history.

Shape:

* ``source_ical_feed_id TEXT NULL`` — FK to ``ical_feed.id`` with
  ``ON DELETE SET NULL``. Nullable: manual closures (owner-stay,
  renovation) created through the future
  ``POST /properties/{id}/closures`` API leave this column ``NULL``;
  only iCal-sourced closures populate it.
* B-tree index ``(source_ical_feed_id)`` so the poller's "drop
  closures whose source VEVENT disappeared" lookup stays cheap on a
  property with many feeds. The poller does not yet retract
  closures (deleting a closure on VEVENT removal is a separate
  follow-up — operators may want to keep the historical record), but
  the index is cheap and unblocks the future job.

**Reversibility.** ``downgrade()`` drops the index + column. Rows
that carried a non-NULL ``source_ical_feed_id`` lose the attribution
on rollback; the closure itself survives.

See ``docs/specs/04-properties-and-stays.md`` §"iCal feed",
``docs/specs/02-domain-model.md`` §"property_closure".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f8a0b2c4d5"
down_revision: str | Sequence[str] | None = "d5e7f9a1b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("property_closure", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "source_ical_feed_id",
                sa.String(),
                nullable=True,
            )
        )
        # FK declared in the same batch so SQLite's table-rebuild
        # picks the constraint up cleanly (a top-level
        # ``op.create_foreign_key`` outside ``batch_alter_table``
        # would emit raw ALTER TABLE that SQLite cannot run on a
        # column it just added).
        batch_op.create_foreign_key(
            "fk_property_closure_source_ical_feed",
            "ical_feed",
            ["source_ical_feed_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_index(
        "ix_property_closure_source_ical_feed",
        "property_closure",
        ["source_ical_feed_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the index, the FK, and the column. Rows that carried a
    non-NULL ``source_ical_feed_id`` lose the attribution on
    rollback; the closure itself survives — only its iCal-feed
    pointer is gone.
    """
    op.drop_index(
        "ix_property_closure_source_ical_feed", table_name="property_closure"
    )
    with op.batch_alter_table("property_closure", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_property_closure_source_ical_feed", type_="foreignkey"
        )
        batch_op.drop_column("source_ical_feed_id")
