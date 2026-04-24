"""ical_feed_extensions_cd_ewd7

Revision ID: f2b4c5d6e7a8
Revises: e1a3b4c5d6f7
Create Date: 2026-04-24 21:00:00.000000

Extends ``ical_feed`` to the full §04 "iCal feed" shape. The cd-1b2
slice (migration ``92f86ca1f70b``) landed only the minimum needed to
prove the external-calendar → reservation → turnover-bundle chain
(``url`` / ``provider`` / ``last_polled_at`` / ``last_etag`` /
``enabled``). cd-ewd7 adds:

* ``last_error`` — nullable TEXT. Persists the most recent §04
  ``ical_url_*`` code (``ical_url_timeout``,
  ``ical_url_private_address``, ...) so the operator UI can render
  a live-vs-stale indicator without tailing the audit stream. NULL
  when the last probe succeeded; the domain service clears on
  success.
* ``poll_cadence`` — NOT NULL TEXT with server default
  ``*/15 * * * *``. Per-feed cron the poller (cd-d48) honours via
  APScheduler. Server default backfills every pre-cd-ewd7 row with
  the §04 baseline so no data migration is needed.
* ``unit_id`` — nullable VARCHAR with an FK to ``unit.id`` and
  ``ON DELETE SET NULL``. §04 keys stay uniqueness on
  ``(unit_id, source, external_id)`` when ``unit_id`` is set, else
  ``(property_id, source, external_id)``. ``SET NULL`` preserves
  the feed row through unit churn (rename / merge / delete).

Constraint widening:

* ``ck_ical_feed_provider`` — the existing CHECK admits
  ``airbnb | vrbo | booking | custom``. cd-ewd7 widens it to
  ``airbnb | vrbo | booking | gcal | generic | custom`` so the
  :mod:`app.adapters.ical.providers` detector's result (which can
  be ``gcal`` or ``generic``) lands verbatim and the cd-1ai
  ``_to_db_provider`` collapse can retire. ``custom`` stays in the
  accept set so pre-cd-ewd7 rows that carried that value remain
  readable.

Implementation notes:

* SQLite doesn't support ``ALTER TABLE … DROP CONSTRAINT`` directly,
  so the CHECK swap runs inside ``op.batch_alter_table`` which
  recreates the table under the naming-convention-driven constraint
  name (``ck_ical_feed_provider``). Sibling migrations
  (``cd-i1qe``, ``cd-8u5``) use the same recipe.
* The ``poll_cadence`` server default is intentionally *only* a
  backfill: the domain service writes the value explicitly on
  every insert, so a future migration can drop the default without
  changing runtime behaviour.
* The new FK to ``unit.id`` uses the
  ``fk_ical_feed_unit_id_unit`` name the shared naming convention
  emits.

Reversibility:

``downgrade()`` drops the unit FK + index + the three added
columns, and narrows the provider CHECK back to the cd-1b2
``airbnb | vrbo | booking | custom`` set. A row that carries
``provider = 'gcal'`` or ``provider = 'generic'`` on rollback
violates the narrower CHECK; we ``UPDATE … SET provider = 'custom'``
before the CHECK swap so the rollback doesn't fail on live data.
That collapse matches the cd-1ai-era behaviour (both slugs used to
land as ``custom`` anyway) so the rollback is data-lossless in
practice.

See ``docs/specs/04-properties-and-stays.md`` §"iCal feed" /
§"Supported providers", ``docs/specs/02-domain-model.md``
§"ical_feed".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2b4c5d6e7a8"
down_revision: str | Sequence[str] | None = "e1a3b4c5d6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Shared between upgrade and downgrade — each path carves a different
# subset out of this list, and keeping the enums beside the migration
# entry points makes it obvious the two sides agree.
_WIDENED_PROVIDERS: tuple[str, ...] = (
    "airbnb",
    "vrbo",
    "booking",
    "gcal",
    "generic",
    "custom",
)
_V1_PROVIDERS: tuple[str, ...] = ("airbnb", "vrbo", "booking", "custom")

_DEFAULT_POLL_CADENCE: str = "*/15 * * * *"


def _in_clause(values: tuple[str, ...]) -> str:
    return "'" + "', '".join(values) + "'"


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("ical_feed", schema=None) as batch_op:
        # Three new columns. Every added column is either nullable
        # (``last_error``, ``unit_id``) or carries a server default
        # (``poll_cadence``) so pre-cd-ewd7 rows survive the
        # migration without a bespoke backfill statement.
        batch_op.add_column(sa.Column("unit_id", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "poll_cadence",
                sa.String(),
                nullable=False,
                server_default=_DEFAULT_POLL_CADENCE,
            )
        )
        batch_op.add_column(sa.Column("last_error", sa.String(), nullable=True))

        # Swap the provider CHECK in place — drop the narrow v1 set,
        # create the widened set. The body ``"provider"`` matches the
        # ``name=`` passed on the original create (see sibling cd-8u5
        # migration); the shared naming convention renders the final
        # constraint name as ``ck_ical_feed_provider``.
        batch_op.drop_constraint("provider", type_="check")
        batch_op.create_check_constraint(
            "provider",
            f"provider IN ({_in_clause(_WIDENED_PROVIDERS)})",
        )

        # FK to ``unit.id``; ``SET NULL`` so a unit hard-delete
        # doesn't cascade into the feed row.
        batch_op.create_foreign_key(
            "fk_ical_feed_unit_id_unit",
            referent_table="unit",
            local_cols=["unit_id"],
            remote_cols=["id"],
            ondelete="SET NULL",
        )

        # Lookup index on ``unit_id``. Most rows will be NULL (feeds
        # default to property-scoped until the manager maps them),
        # but the poller's upsert scan keys on ``unit_id`` when it's
        # set so a plain index keeps that path O(log n).
        batch_op.create_index("ix_ical_feed_unit", ["unit_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Collapses any ``gcal`` / ``generic`` provider row to ``custom``
    before narrowing the CHECK back — matches the cd-1ai-era
    ``_to_db_provider`` mapping so the rollback doesn't fail on live
    data. The collapse is data-lossless in practice because the
    pre-cd-ewd7 service layer already stored those rows as
    ``custom``; any row with ``gcal`` / ``generic`` originated after
    cd-ewd7 and, on rollback, loses its finer taxonomy to preserve
    a clean schema — flagged here so the loss is never silent.
    """
    # Pre-narrow: collapse widened slugs to the v1 ``custom`` value
    # so the CHECK swap doesn't fail on a live row.
    op.execute(
        "UPDATE ical_feed SET provider = 'custom' WHERE provider IN ('gcal', 'generic')"
    )

    with op.batch_alter_table("ical_feed", schema=None) as batch_op:
        batch_op.drop_index("ix_ical_feed_unit")
        batch_op.drop_constraint("fk_ical_feed_unit_id_unit", type_="foreignkey")

        # Narrow the CHECK back to the cd-1b2 set.
        batch_op.drop_constraint("provider", type_="check")
        batch_op.create_check_constraint(
            "provider",
            f"provider IN ({_in_clause(_V1_PROVIDERS)})",
        )

        batch_op.drop_column("last_error")
        batch_op.drop_column("poll_cadence")
        batch_op.drop_column("unit_id")
