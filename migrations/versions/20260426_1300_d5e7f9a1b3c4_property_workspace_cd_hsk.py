"""property_workspace_cd_hsk

Revision ID: d5e7f9a1b3c4
Revises: c4d6e8fab2c5
Create Date: 2026-04-26 13:00:00.000000

Lands the two columns the property↔workspace junction membership
service (cd-hsk) needs:

* ``share_guest_identity BOOLEAN NOT NULL DEFAULT FALSE`` — §02 /
  §15 PII boundary widening flag. False by default; the owner
  workspace toggles it per non-owner row to widen the cross-workspace
  PII boundary (§15 "Cross-workspace visibility"). Existing rows
  default to ``FALSE`` so the conservative read-redaction stays
  in force on legacy data.

* ``status TEXT NOT NULL DEFAULT 'active'`` — invite lifecycle
  marker. The cd-hsk membership service flips a non-owner row from
  ``invited`` → ``active`` on accept; existing rows are all
  bootstrapped owner / live links so the ``'active'`` default is
  correct on backfill. CHECK constraint pins the enum.

The remaining §02 columns (``invite_id``, ``added_via``,
``added_by_user_id``) defer to the larger ``property_workspace_invite``
work (§22) — they belong to the proper invite-table flow, not the
direct-membership v1 service. Leaving them out keeps this migration
narrow + reversible.

**Reversibility.** ``downgrade()`` drops the CHECK and both columns.
Data on rows tagged ``status = 'invited'`` is lost on rollback —
acceptable for a dev-database rollback. An operator running a real
rollback should first either accept the pending rows or hard-delete
them.

See ``docs/specs/02-domain-model.md`` §"property_workspace",
``docs/specs/04-properties-and-stays.md`` §"Multi-belonging",
``docs/specs/15-security-privacy.md`` §"Cross-workspace visibility".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e7f9a1b3c4"
down_revision: str | Sequence[str] | None = "c4d6e8fab2c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Allowed ``property_workspace.status`` values. ``invited`` covers a
# non-owner row that the owner has minted but the recipient workspace
# has not yet accepted; ``active`` covers the live, in-force row.
# Owner-workspace bootstrap rows always carry ``active`` (the seeding
# workspace consents implicitly by creating the property).
_STATUS_VALUES: tuple[str, ...] = ("invited", "active")


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("property_workspace", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "share_guest_identity",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "status",
                sa.String(),
                nullable=False,
                server_default="active",
            )
        )
        batch_op.create_check_constraint(
            "status",
            "status IN ('" + "', '".join(_STATUS_VALUES) + "')",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("property_workspace", schema=None) as batch_op:
        batch_op.drop_constraint("status", type_="check")
        batch_op.drop_column("status")
        batch_op.drop_column("share_guest_identity")
