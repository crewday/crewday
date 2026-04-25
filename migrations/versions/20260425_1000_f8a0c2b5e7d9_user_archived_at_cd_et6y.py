"""user_archived_at_cd_et6y

Revision ID: f8a0c2b5e7d9
Revises: e7a9b1c3d5f6
Create Date: 2026-04-25 10:00:00.000000

Adds the ``archived_at`` tombstone to ``user`` so :mod:`app.auth.tokens`
can return 401 when a delegated / personal-access token's delegating
or subject user has been archived (§03 "Delegated tokens" /
"Personal access tokens" — "If the delegating user is archived,
globally deactivated, or loses every non-revoked grant, requests
with the token return 401 with a clear message").

The column is **nullable** by design: the spec carries archive as a
*tombstone* (set the timestamp to archive, NULL it back out to
reinstate) rather than a separate boolean + reason column. Every
existing row pre-dates the archive concept and lands ``NULL``, which
the verifier reads as "live user". Production archive flips it to
the moment of archive; reinstate clears it back to ``NULL``.

No partial index — the read pattern that uses this column is the
per-request token verifier (one PK lookup against ``user.id``, then a
single ``archived_at IS NOT NULL`` check on the loaded row), not a
broad scan. A partial ``WHERE archived_at IS NULL`` index would only
help fleet-wide "list every live user" queries, none of which exist
today.

**Reversibility.** ``downgrade()`` drops the column. The drop is
non-destructive only in the sense that there is no FK or composite
index pinned to the column today — but the actual archive timestamps
are lost, which is acceptable for a dev-DB rollback (the archive
state is a soft-delete tombstone that can be re-derived from the
audit log if needed). Operators planning a real rollback should dump
the column first.

See ``docs/specs/03-auth-and-tokens.md`` §"Delegated tokens" /
§"Personal access tokens" and ``docs/specs/05-employees-and-roles.md``
§"Archive / reinstate".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8a0c2b5e7d9"
down_revision: str | Sequence[str] | None = "e7a9b1c3d5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "archived_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops ``user.archived_at``. Pre-existing archive timestamps are
    discarded — acceptable for a dev rollback; an operator planning a
    real rollback should dump the column first.
    """
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("archived_at")
