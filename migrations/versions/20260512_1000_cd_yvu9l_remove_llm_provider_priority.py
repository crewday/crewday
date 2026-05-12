"""remove_llm_provider_priority_cd_yvu9l

Revision ID: cdyvu9lprio
Revises: cdcgefwthink
Create Date: 2026-05-12 10:00:00.000000
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "cdyvu9lprio"
down_revision: str | Sequence[str] | None = "cdcgefwthink"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_REQUIRED_CAPABILITIES = '["chat", "function_calling"]'
_REQUIRED_CAPABILITIES = ("chat", "function_calling")
_ULID_PREFIX = "01K7T8YVU9"
_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


def _default_assignment_id(provider_model_id: str) -> str:
    digest = hashlib.sha256(f"default:{provider_model_id}".encode()).digest()
    value = int.from_bytes(digest[:10], "big")
    suffix = ""
    for _ in range(16):
        value, index = divmod(value, 32)
        suffix = _CROCKFORD32[index] + suffix
    return f"{_ULID_PREFIX}{suffix}"


def _default_provider_model_id() -> tuple[str, str] | None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT pm.id, p.name, m.capabilities
            FROM llm_provider_model AS pm
            JOIN llm_provider AS p ON p.id = pm.provider_id
            JOIN llm_model AS m ON m.id = pm.model_id
            WHERE pm.is_enabled IS TRUE
              AND p.is_enabled IS TRUE
              AND m.is_active IS TRUE
              AND m.canonical_name IN (
                'google/gemma-4-31b-it',
                'google/gemma-3-27b-it',
                'default/chat-base'
              )
            ORDER BY
              CASE m.canonical_name
                WHEN 'google/gemma-4-31b-it' THEN 0
                WHEN 'google/gemma-3-27b-it' THEN 1
                WHEN 'default/chat-base' THEN 2
                ELSE 3
              END,
              p.name,
              p.id,
              pm.id
            """
        )
    ).all()
    required = set(_REQUIRED_CAPABILITIES)
    for row in rows:
        if required.issubset(set(_json_list(row[2]))):
            return str(row[0]), str(row[1])
    return None


def _ensure_default_assignment() -> None:
    bind = op.get_bind()
    existing = bind.execute(
        sa.text(
            """
            SELECT id
            FROM llm_assignment
            WHERE workspace_id IS NULL
              AND capability = 'default'
              AND priority = 0
            LIMIT 1
            """
        )
    ).first()
    if existing is not None:
        return
    default_provider_model = _default_provider_model_id()
    if default_provider_model is None:
        return
    provider_model_id, provider_name = default_provider_model
    bind.execute(
        sa.text(
            """
            INSERT INTO llm_assignment (
                id,
                workspace_id,
                capability,
                model_id,
                provider,
                priority,
                enabled,
                max_tokens,
                temperature,
                extra_api_params,
                required_capabilities,
                created_at
            )
            VALUES (
                :id,
                NULL,
                'default',
                :provider_model_id,
                :provider_name,
                0,
                1,
                NULL,
                NULL,
                '{}',
                :required_capabilities,
                CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": _default_assignment_id(provider_model_id),
            "provider_model_id": provider_model_id,
            "provider_name": provider_name,
            "required_capabilities": _DEFAULT_REQUIRED_CAPABILITIES,
        },
    )


def upgrade() -> None:
    _ensure_default_assignment()
    with op.batch_alter_table("llm_provider", schema=None) as batch_op:
        batch_op.drop_column("priority")


def downgrade() -> None:
    with op.batch_alter_table("llm_provider", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "priority",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
