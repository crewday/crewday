"""file_extraction cd-mo9e

Revision ID: f5a8b1c3d4e6
Revises: e4f6a8b0d2c5
Create Date: 2026-05-02 14:00:00.000000

Lands the §02 ``file_extraction`` table backing the manager
document-library text-extraction surface (``GET /documents/{id}/extraction``,
``POST /documents/{id}/extraction/retry``). The §02 prose names a
shared ``file`` table as the FK target; v1 has not landed it yet, so
the PK / FK target is :class:`AssetDocument` directly. A future
``file`` migration can rename the FK without rotating the column
name (the model uses ``synonym("asset_document_id")`` so callers
already read by intent).

State machine: ``pending`` (mint on document upload)
``-> extracting`` (worker claims the row, attempts += 1)
``-> {succeeded | failed | unsupported | empty}``. ``failed`` rows can
return to ``pending`` via the retry route (see §21).

See ``docs/specs/02-domain-model.md`` §"file_extraction" and
``docs/specs/21-assets.md`` §"Document text extraction".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5a8b1c3d4e6"
down_revision: str | Sequence[str] | None = "e4f6a8b0d2c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STATUS_VALUES: tuple[str, ...] = (
    "pending",
    "extracting",
    "succeeded",
    "failed",
    "unsupported",
    "empty",
)
_EXTRACTOR_VALUES: tuple[str, ...] = (
    "passthrough",
    "pdf",
    "docx",
    "ocr",
)


def _in_clause(values: tuple[str, ...]) -> str:
    return "'" + "', '".join(values) + "'"


def upgrade() -> None:
    """Create the ``file_extraction`` table."""
    op.create_table(
        "file_extraction",
        sa.Column(
            "id",
            sa.String(),
            sa.ForeignKey("asset_document.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.String(),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "extraction_status",
            sa.Enum(
                *_STATUS_VALUES,
                name="file_extraction_status",
                native_enum=True,
                create_constraint=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "extractor",
            sa.Enum(
                *_EXTRACTOR_VALUES,
                name="file_extraction_extractor",
                native_enum=True,
                create_constraint=False,
            ),
            nullable=True,
        ),
        sa.Column("body_text", sa.String(), nullable=True),
        sa.Column("pages_json", sa.JSON(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column(
            "has_secret_marker",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"extraction_status IN ({_in_clause(_STATUS_VALUES)})",
            name="file_extraction_status",
        ),
        sa.CheckConstraint(
            f"extractor IS NULL OR extractor IN ({_in_clause(_EXTRACTOR_VALUES)})",
            name="file_extraction_extractor",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="file_extraction_attempts_nonneg",
        ),
        sa.CheckConstraint(
            "token_count IS NULL OR token_count >= 0",
            name="file_extraction_token_count_nonneg",
        ),
    )
    op.create_index(
        "ix_file_extraction_workspace_status",
        "file_extraction",
        ["workspace_id", "extraction_status"],
    )


def downgrade() -> None:
    """Drop the ``file_extraction`` table."""
    op.drop_index("ix_file_extraction_workspace_status", table_name="file_extraction")
    op.drop_table("file_extraction")
