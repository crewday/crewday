"""agent_preferences_upstream_pii_consent_cd_ddy0

Revision ID: e0a2b3c4d5f7
Revises: d9f1a2b3c4e6
Create Date: 2026-05-03 10:00:00.000000

Adds the workspace-scoped ``upstream_pii_consent`` opt-in column to
``agent_preference`` so the §11 redaction layer can preserve named PII
fields on outbound LLM calls instead of redact-everything-by-default.

Shape:

* ``agent_preference.upstream_pii_consent`` — ``JSON`` (``JSONB`` on
  Postgres via SQLAlchemy's generic ``JSON()``), NOT NULL, default
  ``[]``. The body is a list of consent tokens drawn from the
  centralised allow-list maintained on
  :data:`app.util.redact.CONSENT_TOKENS`
  (``legal_name``, ``email``, ``phone``, ``address``). Unknown tokens
  are silently ignored at load time so a future spec addition does
  not turn a forgotten upgrade into a runtime error in the redaction
  hot path; the closed enum is enforced in the loader rather than the
  CHECK constraint to keep additive changes free of a CHECK rewrite.

The companion ``agent_preference_revision`` history table does not
mirror this column. Consent flips are workspace-level operational
configuration, not body history; the revision table snapshots the
authored markdown body and structured controls that round-trip
through the editor surface (``body_md`` / ``blocked_actions`` /
``default_approval_mode``). A consent flip changes operator policy,
not preference content, and lives outside the editor's revision
ledger by design.

Default ``[]`` matches :meth:`app.util.redact.ConsentSet.none` —
the redact-everything posture every existing call site is already
safe to start from. Existing rows backfill to ``[]`` via the server
default; no data migration is required.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e0a2b3c4d5f7"
down_revision: str | Sequence[str] | None = "d9f1a2b3c4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema: add ``agent_preference.upstream_pii_consent``."""
    with op.batch_alter_table("agent_preference", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "upstream_pii_consent",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )


def downgrade() -> None:
    """Downgrade schema: drop ``agent_preference.upstream_pii_consent``."""
    with op.batch_alter_table("agent_preference", schema=None) as batch_op:
        batch_op.drop_column("upstream_pii_consent")
