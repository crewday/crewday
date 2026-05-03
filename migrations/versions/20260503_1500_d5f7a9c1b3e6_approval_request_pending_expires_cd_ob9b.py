"""approval request pending expires index cd-ob9b

Revision ID: d5f7a9c1b3e6
Revises: c4e6f8a0b2d6
Create Date: 2026-05-03 15:00:00.000000

Adds a partial index for the deployment-scope approval TTL sweep:
``status = 'pending' AND expires_at IS NOT NULL AND expires_at <= now``.
The index keys ``expires_at`` and filters to pending rows so the worker
can range-scan due approvals without walking terminal history or every
workspace's desk-pagination index.

The partial predicate intentionally stays at ``status = 'pending'``.
The sweep's ``expires_at <= now`` predicate already excludes NULLs, and
leaving NULL filtering out of the partial predicate matches the portable
PostgreSQL / SQLite DDL shape used by the ORM metadata.

See ``docs/specs/11-llm-and-agents.md`` section "TTL".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5f7a9c1b3e6"
down_revision: str | Sequence[str] | None = "c4e6f8a0b2d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_NAME = "ix_approval_request_pending_expires"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        _INDEX_NAME,
        "approval_request",
        ["expires_at"],
        unique=False,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(_INDEX_NAME, table_name="approval_request")
