"""llm_usage_provider_model_id_rename_cd_v6dj

Revision ID: a2c4e6f8b0d2
Revises: f1b3c5d7e9a0
Create Date: 2026-05-03 12:00:00.000000

Renames ``llm_usage.model_id`` to ``llm_usage.provider_model_id`` so
the column name matches the §02 ``llm_call`` spec — the registry
relation is a **provider-model** wire reference, not the
``llm_model`` registry id. The column body is already correct
(populated from :attr:`LlmUsage.api_model_id` writes); only the name
was wrong. cd-wjpl deferred the rename to coordinate downstream
readers (cd-4btd registry, /admin/usage feed, cd-irng budget sum)
and documented the drift in :class:`LlmUsage`'s docstring + on
migration b8d0e1f2a3b4. Those readers are now in place, so the
column and every reader flip together in this revision.

The migration uses ``op.batch_alter_table`` on SQLite (which
materialises the rename as a table-copy + new column name) and a
plain ``ALTER TABLE ... RENAME COLUMN`` on PostgreSQL (cheap
metadata-only operation). The dialect branch keeps the SQLite
schema-fingerprint honest while avoiding the table-copy overhead on
PG. Indexes on ``llm_usage`` reference ``workspace_id`` /
``capability`` / ``correlation_id`` / ``actor_user_id`` /
``created_at`` only — none touch ``model_id``, so neither shard needs
an index rebuild for the rename.

``downgrade()`` is symmetric — flips the column back to ``model_id``
under the same dialect branch so the upgrade → downgrade → upgrade
cycle leaves the schema identical.

See ``docs/specs/02-domain-model.md`` §"LLM" §"llm_call",
``docs/specs/11-llm-and-agents.md`` §"Cost tracking".
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2c4e6f8b0d2"
down_revision: str | Sequence[str] | None = "f1b3c5d7e9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename ``llm_usage.model_id`` → ``llm_usage.provider_model_id``."""
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        # Cheap metadata-only rename on PG.
        op.execute("ALTER TABLE llm_usage RENAME COLUMN model_id TO provider_model_id")
    else:
        # SQLite (and any other backend without a native rename) goes
        # through batch_alter_table so the column is renamed via a
        # table-copy. Keeps the schema-fingerprint honest on the
        # upgrade → downgrade → upgrade cycle.
        with op.batch_alter_table("llm_usage", schema=None) as batch_op:
            batch_op.alter_column("model_id", new_column_name="provider_model_id")


def downgrade() -> None:
    """Revert ``provider_model_id`` back to ``model_id``."""
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("ALTER TABLE llm_usage RENAME COLUMN provider_model_id TO model_id")
    else:
        with op.batch_alter_table("llm_usage", schema=None) as batch_op:
            batch_op.alter_column("provider_model_id", new_column_name="model_id")
