"""chat_link_challenge_workspace_id cd-zzplt

Revision ID: cdzzpltchalws
Revises: cd0lnr9winhit
Create Date: 2026-07-04 13:00:00.000000

Adds a denormalised ``workspace_id`` column to ``chat_link_challenge``
so the table is a plain workspace-scoped table like every other
messaging row. Before cd-zzplt the model reached the workspace
boundary only through ``binding_id -> chat_channel_binding.workspace_id``
yet was registered as a *plain* scoped table (``register(...)``); the
ORM tenant filter therefore tried ``chat_link_challenge.workspace_id``
on every query and crashed with ``AttributeError`` under a
:class:`~app.tenancy.WorkspaceContext`. Carrying the column lets the
plain registration work as intended — SELECT/UPDATE/DELETE stay scoped
without a ``tenant_agnostic`` escape.

Backfill: existing rows copy ``workspace_id`` from their parent
``chat_channel_binding`` (joined on ``binding_id``). ``binding_id`` is
NOT NULL and FKs ``chat_channel_binding.id`` with ``ON DELETE CASCADE``,
so every challenge has exactly one live binding to inherit from and no
row is left NULL before the NOT NULL constraint lands.

Recipe (portable SQLite + Postgres):

1. Add ``workspace_id`` nullable — no data yet.
2. Backfill from the binding via a correlated subquery.
3. Alter to NOT NULL and add the ``workspace.id`` FK
   (``ON DELETE CASCADE``, matching the sibling messaging tables) in
   one batch so SQLite recreates the table once.

Reversibility: ``downgrade()`` drops the FK and the column. The
denormalised value is fully recoverable from the binding, so the
rollback is data-lossless.

See ``docs/specs/23-chat-gateway.md`` §"off-app channels",
``docs/specs/02-domain-model.md`` §"chat_link_challenge", and
``app/tenancy/orm_filter.py`` for the plain-scoped-table contract.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cdzzpltchalws"
down_revision: str | Sequence[str] | None = "cd0lnr9winhit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BACKFILL_SQL = (
    "UPDATE chat_link_challenge "
    "SET workspace_id = ("
    "SELECT chat_channel_binding.workspace_id FROM chat_channel_binding "
    "WHERE chat_channel_binding.id = chat_link_challenge.binding_id"
    ")"
)


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Add the column nullable so existing rows survive the ALTER.
    with op.batch_alter_table("chat_link_challenge", schema=None) as batch_op:
        batch_op.add_column(sa.Column("workspace_id", sa.String(), nullable=True))

    # 2. Backfill every row from its parent binding's workspace.
    op.execute(_BACKFILL_SQL)

    # 3. Enforce NOT NULL and wire the FK now that no value is NULL.
    with op.batch_alter_table("chat_link_challenge", schema=None) as batch_op:
        batch_op.alter_column(
            "workspace_id",
            existing_type=sa.String(),
            nullable=False,
        )
        batch_op.create_foreign_key(
            "fk_chat_link_challenge_workspace_id_workspace",
            referent_table="workspace",
            local_cols=["workspace_id"],
            remote_cols=["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the FK and the denormalised column. The value is fully
    recoverable from ``chat_channel_binding`` on a re-upgrade, so the
    rollback loses no information.
    """
    with op.batch_alter_table("chat_link_challenge", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_chat_link_challenge_workspace_id_workspace",
            type_="foreignkey",
        )
        batch_op.drop_column("workspace_id")
