"""user_push_token cd-nq9s

Revision ID: a6b8c0d2e4f7
Revises: f5a8b1c3d4e6
Create Date: 2026-05-02 15:00:00.000000

Lands the §02 ``user_push_token`` table backing the future native-app
push-notification surface (``GET / POST / PUT / DELETE
/api/v1/me/push-tokens``). Identity-scoped — one row per (user, device)
pair, with ``platform IN ('android','ios')`` and a unique ``(platform,
token)`` index so a hand-off without sign-out trips a deterministic
``409 token_claimed``.

Distinct from the workspace-scoped ``push_token`` table (web-push
endpoints): ``push_token`` carries a browser ``PushSubscription`` URL
+ encryption material; ``user_push_token`` carries a bare FCM/APNS
token + platform discriminator. Both feed the §10 "Agent-message
delivery" worker but along different fan-out branches.

See ``docs/specs/02-domain-model.md`` §"user_push_token",
``docs/specs/12-rest-api.md`` §"Device push tokens",
``docs/specs/14-web-frontend.md`` §"Native wrapper readiness", and
``docs/specs/10-messaging-notifications.md`` §"Agent-message delivery"
(60-day inactivity → ``disabled``; 90-day disabled-row purge).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a6b8c0d2e4f7"
down_revision: str | Sequence[str] | None = "f5a8b1c3d4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``user_push_token`` table."""
    op.create_table(
        "user_push_token",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("device_label", sa.String(), nullable=True),
        sa.Column("app_version", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "platform IN ('android', 'ios')",
            name="ck_user_push_token_platform",
        ),
        sa.UniqueConstraint(
            "platform",
            "token",
            name="uq_user_push_token_platform_token",
        ),
    )
    op.create_index(
        "ix_user_push_token_user",
        "user_push_token",
        ["user_id"],
    )


def downgrade() -> None:
    """Drop the ``user_push_token`` table."""
    op.drop_index("ix_user_push_token_user", table_name="user_push_token")
    op.drop_table("user_push_token")
