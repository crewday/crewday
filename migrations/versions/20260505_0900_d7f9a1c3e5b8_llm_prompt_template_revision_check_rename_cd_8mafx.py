"""llm_prompt_template_revision CHECK rename cd-8mafx

Revision ID: d7f9a1c3e5b8
Revises: c6e8f0a2d4b6
Create Date: 2026-05-05 09:00:00.000000

Renames the ``llm_prompt_template_revision.version >= 1`` CHECK to
``ck_llm_prompt_template_revision_version_min`` on databases that applied
the original cd-4if3 migration before its fresh-schema name was shortened.

SQLite preserved the original overlong name; PostgreSQL truncated it to a
63-byte identifier with SQLAlchemy's deterministic suffix. The predicate is
unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = "d7f9a1c3e5b8"
down_revision: str | Sequence[str] | None = "c6e8f0a2d4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llm_prompt_template_revision"
_PREDICATE = "version >= 1"
_CANONICAL_NAME = "ck_llm_prompt_template_revision_version_min"
_LEGACY_NAME_TOKEN = "llm_prompt_template_revision_version"
_SQLITE_LEGACY_NAME = (
    "ck_llm_prompt_template_revision_llm_prompt_template_revision_version"
)
_POSTGRES_LEGACY_NAME = "ck_llm_prompt_template_revision_llm_prompt_template_rev_c3ca"


def _check_names() -> set[str]:
    bind = op.get_bind()
    return {c["name"] or "" for c in inspect(bind).get_check_constraints(_TABLE)}


def upgrade() -> None:
    """Rename the legacy CHECK if this database still carries it."""
    names = _check_names()
    if _CANONICAL_NAME in names:
        return

    legacy_name = next(
        (
            name
            for name in (_POSTGRES_LEGACY_NAME, _SQLITE_LEGACY_NAME)
            if name in names
        ),
        None,
    )
    if legacy_name is None:
        return

    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(op.f(legacy_name), type_="check")
        batch_op.create_check_constraint("version_min", _PREDICATE)


def downgrade() -> None:
    """Restore the original model token so each backend renders its legacy name."""
    names = _check_names()
    if _CANONICAL_NAME not in names:
        return

    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(op.f(_CANONICAL_NAME), type_="check")
        batch_op.create_check_constraint(_LEGACY_NAME_TOKEN, _PREDICATE)
