"""agent relay request persistence cd-rfk94

Revision ID: cd_rfk94
Revises: d1e3f5a7c9b0
Create Date: 2026-05-10 14:00:00.000000

Adds workspace-scoped correlation storage for §11 agent-mediated user
requests. The row stores only relay-safe summaries and thread references;
existing chat_message rows remain the human-visible transcript.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cd_rfk94"
down_revision: str | Sequence[str] | None = "d1e3f5a7c9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create agent relay request storage."""
    op.create_table(
        "agent_relay_request",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("requester_user_id", sa.String(), nullable=True),
        sa.Column("target_user_id", sa.String(), nullable=True),
        sa.Column("requester_display_label", sa.String(), nullable=False),
        sa.Column("target_display_label", sa.String(), nullable=False),
        sa.Column("requester_scope", sa.String(), nullable=False),
        sa.Column("requester_thread_ref", sa.String(), nullable=False),
        sa.Column("requester_message_ref", sa.String(), nullable=True),
        sa.Column("target_scope", sa.String(), nullable=False),
        sa.Column("target_thread_ref", sa.String(), nullable=True),
        sa.Column("target_message_ref", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("request_summary", sa.String(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("response_summary", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'answered', 'expired', 'cancelled', 'failed')",
            name=op.f("ck_agent_relay_request_status"),
        ),
        sa.ForeignKeyConstraint(
            ["requester_user_id"],
            ["user.id"],
            name=op.f("fk_agent_relay_request_requester_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"],
            ["user.id"],
            name=op.f("fk_agent_relay_request_target_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_agent_relay_request_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_relay_request")),
    )
    op.create_index(
        "ix_agent_relay_request_open_target",
        "agent_relay_request",
        ["workspace_id", "target_user_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_relay_request_requester_thread",
        "agent_relay_request",
        ["workspace_id", "requester_scope", "requester_thread_ref", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_agent_relay_request_active_question",
        "agent_relay_request",
        [
            "workspace_id",
            "requester_user_id",
            "target_user_id",
            "request_fingerprint",
        ],
        unique=True,
        sqlite_where=sa.text("status = 'open'"),
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    """Drop agent relay request storage."""
    op.drop_index(
        "uq_agent_relay_request_active_question",
        table_name="agent_relay_request",
    )
    op.drop_index(
        "ix_agent_relay_request_requester_thread",
        table_name="agent_relay_request",
    )
    op.drop_index(
        "ix_agent_relay_request_open_target",
        table_name="agent_relay_request",
    )
    op.drop_table("agent_relay_request")
