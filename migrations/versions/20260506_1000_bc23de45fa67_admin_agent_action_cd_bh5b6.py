"""admin agent action replay cd-bh5b6

Revision ID: bc23de45fa67
Revises: ab12cd34ef56
Create Date: 2026-05-06 10:00:00.000000

Adds deployment-scoped admin agent action storage so `/admin` inline
approvals do not overload workspace approval rows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bc23de45fa67"
down_revision: str | Sequence[str] | None = "ab12cd34ef56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create deployment-admin agent action storage."""
    op.create_table(
        "admin_agent_action",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_by_token_id", sa.String(), nullable=True),
        sa.Column("for_user_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("resolved_payload_json", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("gate_source", sa.String(), nullable=False),
        sa.Column("card_summary", sa.String(), nullable=False),
        sa.Column("card_risk", sa.String(), nullable=False),
        sa.Column("card_fields_json", sa.JSON(), nullable=False),
        sa.Column("inline_channel", sa.String(), nullable=False),
        sa.Column("page_context", sa.String(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", sa.String(), nullable=True),
        sa.Column("decision_note_md", sa.String(), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'rejected', 'executed')",
            name=op.f("ck_admin_agent_action_state"),
        ),
        sa.CheckConstraint(
            "card_risk IN ('low', 'medium', 'high')",
            name=op.f("ck_admin_agent_action_card_risk"),
        ),
        sa.CheckConstraint(
            "inline_channel IN ('desk_only', 'web_admin_sidebar')",
            name=op.f("ck_admin_agent_action_inline_channel"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["user.id"],
            name=op.f("fk_admin_agent_action_decided_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["for_user_id"],
            ["user.id"],
            name=op.f("fk_admin_agent_action_for_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_agent_action")),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_admin_agent_action_idempotency_key"),
        ),
    )
    op.create_index(
        "ix_admin_agent_action_pending_user_channel",
        "admin_agent_action",
        ["for_user_id", "state", "inline_channel", "requested_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop deployment-admin agent action storage."""
    op.drop_index(
        "ix_admin_agent_action_pending_user_channel",
        table_name="admin_agent_action",
    )
    op.drop_table("admin_agent_action")
