"""role_grant binding_org_id cd-2mwu

Revision ID: d6f8a0c2e4f7
Revises: c5e7f9b1d4e6
Create Date: 2026-04-30 04:00:00.000000

Adds the client-portal organization binding on workspace-scoped
client grants. The column implements the §02 / §22 ``binding_org_id``
contract used to scope client portal reads to data billed to the
client's organization.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6f8a0c2e4f7"
down_revision: str | Sequence[str] | None = "c5e7f9b1d4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("role_grant", schema=None) as batch_op:
        batch_op.add_column(sa.Column("binding_org_id", sa.String(), nullable=True))
        batch_op.create_check_constraint(
            "client_binding_org_scope",
            "binding_org_id IS NULL OR "
            "(scope_kind = 'workspace' AND grant_role = 'client' "
            "AND workspace_id IS NOT NULL AND scope_property_id IS NULL)",
        )
        batch_op.create_foreign_key(
            "fk_role_grant_binding_org_id_organization",
            "organization",
            ["binding_org_id", "workspace_id"],
            ["id", "workspace_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "ix_role_grant_binding_org",
            ["workspace_id", "binding_org_id"],
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("role_grant", schema=None) as batch_op:
        batch_op.drop_index("ix_role_grant_binding_org")
        batch_op.drop_constraint(
            "fk_role_grant_binding_org_id_organization",
            type_="foreignkey",
        )
        batch_op.drop_constraint("client_binding_org_scope", type_="check")
        batch_op.drop_column("binding_org_id")
