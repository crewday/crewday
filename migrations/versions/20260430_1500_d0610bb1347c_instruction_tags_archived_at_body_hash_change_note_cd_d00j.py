"""instruction_tags_archived_at_body_hash_change_note cd-d00j

Revision ID: d0610bb1347c
Revises: b7d9f1a3c5e7
Create Date: 2026-04-30 15:00:00.000000

Lands the four columns + two indexes cd-oyq's CRUD service contract
needs on top of cd-bce's v1 instructions slice. Schema-only — no
behaviour ships here; the service layer that exercises these columns
lands with cd-oyq.

Shape:

* ``instruction.tags`` — JSON array of normalised tag strings; default
  ``[]``. Service layer enforces normalisation (cap 20, lowercase,
  dedupe). ``sa.JSON`` keeps the column portable across SQLite +
  Postgres.
* ``instruction.archived_at`` — TIMESTAMP WITH TIME ZONE NULL. Soft-
  delete tombstone mirroring the
  ``messaging.chat_channel.archived_at`` /
  ``billing.organization.archived_at`` / ``user.archived_at``
  patterns elsewhere.
* B-tree composite index ``(workspace_id, archived_at)`` on
  ``instruction`` — leading ``workspace_id`` rides the tenant filter,
  trailing ``archived_at`` lets the ``?include=archived`` listing
  path scan archived rows cheaply.
* ``instruction_version.body_hash`` — SHA-256 hex digest of the
  post-normalised ``body_md``. NOT NULL. Stored as
  :class:`String` to match the existing
  ``ApiToken.hash`` / ``AgentToken.hash`` convention rather than
  introducing a brand-new ``CHAR(64)`` style; the 64-char invariant
  is documented on the column.
* B-tree composite index ``(instruction_id, body_hash)`` on
  ``instruction_version`` — cd-oyq's idempotency check ("does any
  version of this instruction already carry this exact body?") rides
  this directly. NOT unique — uniqueness stays on
  ``(instruction_id, version_num)`` so two rows sharing the same
  hash on the same instruction is legal.
* ``instruction_version.change_note`` — TEXT NULL. Optional human-
  authored revision summary.

Backfill. Every existing ``instruction`` row gets
``tags = []`` and ``archived_at = NULL``; every existing
``instruction_version`` row gets ``change_note = NULL`` and
``body_hash = sha256(body_md)``. The hash backfill iterates in
Python via ``op.get_bind()`` rather than relying on a SQL
``sha256()`` function — SQLite ships without one and a portable
in-Python loop avoids a dialect fork. ``tags`` defaults via the
``server_default='[]'`` add-column path so existing rows carry a
JSON empty list rather than a ``NULL`` that the ORM's
``Mapped[list[str]]`` type would refuse.

Reversibility. ``downgrade()`` drops the indexes then the columns.
Pre-existing tag / archive / change_note / body_hash values are
discarded — acceptable for a dev DB rollback. An operator planning
a real rollback should dump the columns first.

See ``docs/specs/07-instructions-kb.md`` §"instruction" /
§"instruction_revision" and ``docs/specs/02-domain-model.md``
§"instruction" / §"instruction_version".
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d0610bb1347c"
down_revision: str | Sequence[str] | None = "b7d9f1a3c5e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill_body_hashes() -> None:
    """Stamp every existing ``instruction_version`` with sha256(body_md).

    The migration adds ``body_hash`` as ``NOT NULL`` after the rows
    land (see the upgrade flow), so we hash in Python via
    ``op.get_bind()``. SQLite ships without a SQL ``sha256()``
    function; Postgres has one in ``pgcrypto`` but requiring the
    extension on every host is heavier than the loop. ``body_md`` is
    UTF-8 text on both backends.
    """
    instruction_version = sa.table(
        "instruction_version",
        sa.column("id", sa.String()),
        sa.column("body_md", sa.String()),
        sa.column("body_hash", sa.String()),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(instruction_version.c.id, instruction_version.c.body_md)
    ).all()
    for row_id, body_md in rows:
        digest = hashlib.sha256(body_md.encode("utf-8")).hexdigest()
        bind.execute(
            instruction_version.update()
            .where(instruction_version.c.id == row_id)
            .values(body_hash=digest)
        )


def upgrade() -> None:
    """Upgrade schema."""
    # 1. ``instruction`` — add ``tags`` (JSON, default []) +
    #    ``archived_at`` (nullable). ``server_default='[]'`` keeps
    #    existing rows from landing as JSON ``NULL`` (the ORM
    #    ``Mapped[list[str]]`` type would refuse the read).
    with op.batch_alter_table("instruction", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tags",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )
        batch_op.add_column(
            sa.Column(
                "archived_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_instruction_workspace_archived_at",
            ["workspace_id", "archived_at"],
            unique=False,
        )

    # 2. ``instruction_version`` — add ``body_hash`` (eventually NOT
    #    NULL) and ``change_note`` (nullable). Add ``body_hash`` as
    #    nullable first so existing rows can be backfilled in step 3,
    #    then tighten to NOT NULL in step 4.
    with op.batch_alter_table("instruction_version", schema=None) as batch_op:
        batch_op.add_column(sa.Column("body_hash", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("change_note", sa.String(), nullable=True))

    # 3. Backfill ``body_hash`` per row.
    _backfill_body_hashes()

    # 4. Tighten ``body_hash`` to NOT NULL and create the
    #    ``(instruction_id, body_hash)`` index.
    with op.batch_alter_table("instruction_version", schema=None) as batch_op:
        batch_op.alter_column("body_hash", existing_type=sa.String(), nullable=False)
        batch_op.create_index(
            "ix_instruction_version_instruction_body_hash",
            ["instruction_id", "body_hash"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops both indexes then the four columns. Tag / archive /
    change_note / body_hash values are discarded — acceptable for a
    dev rollback. An operator planning a real rollback should dump
    the columns first.
    """
    with op.batch_alter_table("instruction_version", schema=None) as batch_op:
        batch_op.drop_index("ix_instruction_version_instruction_body_hash")
        batch_op.drop_column("change_note")
        batch_op.drop_column("body_hash")

    with op.batch_alter_table("instruction", schema=None) as batch_op:
        batch_op.drop_index("ix_instruction_workspace_archived_at")
        batch_op.drop_column("archived_at")
        batch_op.drop_column("tags")
