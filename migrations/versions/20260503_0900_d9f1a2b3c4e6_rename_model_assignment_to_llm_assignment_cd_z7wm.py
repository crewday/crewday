"""rename_model_assignment_to_llm_assignment_cd_z7wm

Revision ID: d9f1a2b3c4e6
Revises: c8e0f4a6b9d3
Create Date: 2026-05-03 09:00:00.000000

Reconciles spec drift on the §11 capability-to-model binding table:
the spec (``docs/specs/02-domain-model.md`` §"LLM",
``docs/specs/11-llm-and-agents.md`` §"Model assignment",
``docs/specs/20-glossary.md``) names this table ``llm_assignment``,
matching the rest of the §11 ``llm_*`` family (``llm_provider`` /
``llm_model`` / ``llm_provider_model`` / ``llm_call`` /
``llm_usage_daily`` / ``llm_capability_inheritance`` /
``llm_prompt_template`` / ``llm_prompt_template_revision``). The cd-cm5
v1 slice landed it as ``model_assignment``; cd-z7wm renames the live
name without touching semantics.

Shape changes (semantic-equivalent):

* ``model_assignment`` → ``llm_assignment``.
* Constraint / index renames so the names match the new table:

  * ``pk_model_assignment`` → ``pk_llm_assignment``
  * ``fk_model_assignment_workspace_id_workspace`` →
    ``fk_llm_assignment_workspace_id_workspace``
  * ``fk_model_assignment_model_id_llm_provider_model`` →
    ``fk_llm_assignment_model_id_llm_provider_model``
  * ``ix_model_assignment_workspace_capability_priority`` →
    ``ix_llm_assignment_workspace_capability_priority``

  CHECK names (``priority_non_negative``) and column names are
  unchanged — only the qualifying ``model_assignment`` prefix on
  auto-generated identifiers needs to swap.

**SQLite path.** SQLite's ``ALTER TABLE … RENAME TO`` renames the
table but leaves CHECK / FK / index identifiers untouched (they're
stored as text inside ``sqlite_master`` against the original name).
The portable shape is to drop the table-qualified indexes first,
``rename_table`` the table, then re-create the indexes against the
new name. The ``batch_alter_table`` rebuild idiom is overkill here
because no column shape is changing.

**Postgres path.** Postgres rewrites the system catalog when the
table is renamed, so dependent indexes / FK names ride along — but
their *names* still carry the ``model_assignment`` prefix because
they were minted at create time. We drop / re-create the indexes
under the new name to keep both backends symmetric (and to match the
ORM's ``Index("ix_llm_assignment_workspace_capability_priority", …)``
declaration, which the ``schema-fingerprint`` parity gate compares
against the live schema). Constraint renames (``pk_*`` / ``fk_*``)
ride :func:`alembic.op.batch_alter_table` so SQLite reaches them via
the table-copy idiom.

**Data preservation.** No rows are touched. Every row (workspace
binding, fallback rung, primary, disabled-in-place row) round-trips
under the new name. The §11 resolver and the admin /llm pages reload
their query plans against the renamed table on next request — there
is no cache to invalidate.

**Reversibility.** ``downgrade()`` reverses the rename and restores
every original constraint / index name. The cd-4btd / cd-u84y /
cd-cm5 round-trip suites still expect the pre-rename names below
this revision, so ``downgrade -1`` from head must yield the exact
shape they pin.

See ``docs/specs/02-domain-model.md`` §"LLM",
``docs/specs/11-llm-and-agents.md`` §"Model assignment", and the
cd-z7wm task brief.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9f1a2b3c4e6"
down_revision: str | Sequence[str] | None = "c8e0f4a6b9d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema: rename ``model_assignment`` → ``llm_assignment``."""
    # 1. Drop the table-qualified composite index. SQLite stores its
    #    name as text and would silently keep pointing at
    #    ``model_assignment`` after the rename; PG would carry the old
    #    name forward. Re-creating it under the new name in step 3
    #    keeps both backends + the ORM declaration in sync.
    op.drop_index(
        "ix_model_assignment_workspace_capability_priority",
        table_name="model_assignment",
    )

    # 2. Rename the table itself. SQLite + PG both honour
    #    ``ALTER TABLE … RENAME TO``; alembic emits the right form per
    #    dialect.
    op.rename_table("model_assignment", "llm_assignment")

    # 3. Re-create the composite index against the new table name.
    #    Same shape as cd-u84y's
    #    ``ix_model_assignment_workspace_capability_priority`` — the
    #    §11 resolver's sorted-scan target.
    op.create_index(
        "ix_llm_assignment_workspace_capability_priority",
        "llm_assignment",
        ["workspace_id", "capability", "priority"],
        unique=False,
    )

    # 4. Rename the auto-generated PK / FK constraints so they match
    #    the new table prefix. ``batch_alter_table`` carries SQLite's
    #    table-copy rebuild + PG's plain ALTER TABLE through one code
    #    path. The CHECK ``priority_non_negative`` was minted with a
    #    short body name, so it is not table-qualified and survives
    #    the rename untouched.
    with op.batch_alter_table("llm_assignment", schema=None) as batch_op:
        batch_op.drop_constraint("pk_model_assignment", type_="primary")
        batch_op.create_primary_key("pk_llm_assignment", ["id"])

        batch_op.drop_constraint(
            "fk_model_assignment_workspace_id_workspace", type_="foreignkey"
        )
        batch_op.create_foreign_key(
            "fk_llm_assignment_workspace_id_workspace",
            "workspace",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )

        batch_op.drop_constraint(
            "fk_model_assignment_model_id_llm_provider_model",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_llm_assignment_model_id_llm_provider_model",
            "llm_provider_model",
            ["model_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    """Downgrade schema: restore the pre-cd-z7wm ``model_assignment`` name.

    Mirrors :func:`upgrade` in reverse so the older round-trip suites
    (cd-cm5 / cd-u84y / cd-4btd / cd-wjpl) keep observing the exact
    table + constraint + index names they pinned.
    """
    # 1. Reverse the constraint renames first — the table still
    #    answers to ``llm_assignment`` until step 3.
    with op.batch_alter_table("llm_assignment", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_llm_assignment_model_id_llm_provider_model",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_model_assignment_model_id_llm_provider_model",
            "llm_provider_model",
            ["model_id"],
            ["id"],
            ondelete="RESTRICT",
        )

        batch_op.drop_constraint(
            "fk_llm_assignment_workspace_id_workspace", type_="foreignkey"
        )
        batch_op.create_foreign_key(
            "fk_model_assignment_workspace_id_workspace",
            "workspace",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )

        batch_op.drop_constraint("pk_llm_assignment", type_="primary")
        batch_op.create_primary_key("pk_model_assignment", ["id"])

    # 2. Drop the renamed composite index so the original name can
    #    land cleanly after the rename below.
    op.drop_index(
        "ix_llm_assignment_workspace_capability_priority",
        table_name="llm_assignment",
    )

    # 3. Reverse the table rename.
    op.rename_table("llm_assignment", "model_assignment")

    # 4. Re-create the original composite index against
    #    ``model_assignment`` — same shape cd-u84y landed.
    op.create_index(
        "ix_model_assignment_workspace_capability_priority",
        "model_assignment",
        ["workspace_id", "capability", "priority"],
        unique=False,
    )
