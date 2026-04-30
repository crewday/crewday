"""workspace verification columns cd-s8kk

Revision ID: e4f6a8c0d2e5
Revises: d3f5a7c9e1b4
Create Date: 2026-04-30 11:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "e4f6a8c0d2e5"
down_revision: str | Sequence[str] | None = "d3f5a7c9e1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERIFICATION_STATES = frozenset(
    ("unverified", "email_verified", "human_verified", "trusted")
)
_VERIFICATION_STATE_KEY = "admin_verification_state"
_ARCHIVED_AT_KEY = "admin_archived_at"


def _settings_dict(raw: object) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return parsed
    return {}


def _archived_at(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _settings_with_lifecycle(
    raw: object,
    *,
    verification_state: object,
    archived_at: object,
) -> dict[str, Any]:
    settings = dict(_settings_dict(raw))
    if verification_state not in _VERIFICATION_STATES:
        verification_state = "unverified"
    settings[_VERIFICATION_STATE_KEY] = verification_state

    parsed_archived_at = _archived_at(archived_at)
    if parsed_archived_at is None:
        settings.pop(_ARCHIVED_AT_KEY, None)
    else:
        settings[_ARCHIVED_AT_KEY] = parsed_archived_at.astimezone(UTC).isoformat()
    return settings


def _backfill_from_settings_json() -> None:
    workspace = sa.table(
        "workspace",
        sa.column("id", sa.String()),
        sa.column("settings_json", sa.JSON()),
        sa.column("verification_state", sa.String()),
        sa.column("archived_at", sa.DateTime(timezone=True)),
    )
    conn = op.get_bind()
    rows = conn.execute(sa.select(workspace.c.id, workspace.c.settings_json)).all()
    for workspace_id, raw_settings in rows:
        settings = _settings_dict(raw_settings)
        state = settings.get(_VERIFICATION_STATE_KEY)
        if state not in _VERIFICATION_STATES:
            state = "unverified"
        conn.execute(
            workspace.update()
            .where(workspace.c.id == workspace_id)
            .values(
                verification_state=state,
                archived_at=_archived_at(settings.get(_ARCHIVED_AT_KEY)),
            )
        )


def _copy_to_settings_json() -> None:
    workspace = sa.table(
        "workspace",
        sa.column("id", sa.String()),
        sa.column("settings_json", sa.JSON()),
        sa.column("verification_state", sa.String()),
        sa.column("archived_at", sa.DateTime(timezone=True)),
    )
    conn = op.get_bind()
    rows = conn.execute(
        sa.select(
            workspace.c.id,
            workspace.c.settings_json,
            workspace.c.verification_state,
            workspace.c.archived_at,
        )
    ).all()
    for workspace_id, raw_settings, verification_state, archived_at in rows:
        conn.execute(
            workspace.update()
            .where(workspace.c.id == workspace_id)
            .values(
                settings_json=_settings_with_lifecycle(
                    raw_settings,
                    verification_state=verification_state,
                    archived_at=archived_at,
                )
            )
        )


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("workspace", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "verification_state",
                sa.String(),
                nullable=False,
                server_default="unverified",
            )
        )
        batch_op.add_column(
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_check_constraint(
            "workspace_verification_state",
            "verification_state IN ("
            "'unverified', 'email_verified', 'human_verified', 'trusted'"
            ")",
        )
        batch_op.create_index(
            "ix_workspace_verification_state",
            ["verification_state"],
            unique=False,
        )
        batch_op.create_index(
            "ix_workspace_archived_at",
            ["archived_at"],
            unique=False,
        )
    _backfill_from_settings_json()


def downgrade() -> None:
    """Downgrade schema."""
    _copy_to_settings_json()
    with op.batch_alter_table("workspace", schema=None) as batch_op:
        batch_op.drop_index("ix_workspace_archived_at")
        batch_op.drop_index("ix_workspace_verification_state")
        batch_op.drop_constraint("workspace_verification_state", type_="check")
        batch_op.drop_column("archived_at")
        batch_op.drop_column("verification_state")
