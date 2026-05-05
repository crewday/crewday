"""identity constraint-name renames cd-0koqx

Revision ID: e8a0b2c4d6f0
Revises: d7f9a1c3e5b8
Create Date: 2026-05-05 10:00:00.000000

Renames identity-table constraints whose model tokens included the
convention prefix, producing doubled CHECK names and potentially doubled
custom UNIQUE names on databases that applied those tokens through a
convention-aware path.

The predicates and UNIQUE column sets are unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from alembic import op
from sqlalchemy import inspect

revision: str = "e8a0b2c4d6f0"
down_revision: str | Sequence[str] | None = "d7f9a1c3e5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class ConstraintRename(NamedTuple):
    table: str
    kind: str
    doubled_name: str
    canonical_name: str
    predicate: str | None = None
    columns: tuple[str, ...] = ()


_RENAMES: tuple[ConstraintRename, ...] = (
    ConstraintRename(
        table="user",
        kind="check",
        doubled_name="ck_user_user_agent_approval_mode",
        canonical_name="ck_user_agent_approval_mode",
        predicate="agent_approval_mode IN ('bypass', 'auto', 'strict')",
    ),
    ConstraintRename(
        table="api_token",
        kind="check",
        doubled_name="ck_api_token_ck_api_token_kind",
        canonical_name="ck_api_token_kind",
        predicate="kind IN ('scoped', 'delegated', 'personal')",
    ),
    ConstraintRename(
        table="api_token",
        kind="check",
        doubled_name="ck_api_token_ck_api_token_kind_shape",
        canonical_name="ck_api_token_kind_shape",
        predicate=(
            "("
            "(kind = 'scoped' AND delegate_for_user_id IS NULL "
            "AND subject_user_id IS NULL AND workspace_id IS NOT NULL)"
            " OR "
            "(kind = 'delegated' AND delegate_for_user_id IS NOT NULL "
            "AND subject_user_id IS NULL AND workspace_id IS NOT NULL)"
            " OR "
            "(kind = 'personal' AND subject_user_id IS NOT NULL "
            "AND delegate_for_user_id IS NULL AND workspace_id IS NULL)"
            ")"
        ),
    ),
    ConstraintRename(
        table="signup_attempt",
        kind="unique",
        doubled_name="uq_signup_attempt_uq_signup_attempt_email_slug",
        canonical_name="uq_signup_attempt_email_slug",
        columns=("email_lower", "desired_slug"),
    ),
    ConstraintRename(
        table="invite",
        kind="check",
        doubled_name="ck_invite_ck_invite_state",
        canonical_name="ck_invite_state",
        predicate="state IN ('pending', 'accepted', 'expired', 'revoked')",
    ),
    ConstraintRename(
        table="invite",
        kind="unique",
        doubled_name="uq_invite_uq_invite_workspace_email_state",
        canonical_name="uq_invite_workspace_email_state",
        columns=("workspace_id", "pending_email_lower", "state"),
    ),
)


def _names(table: str, kind: str) -> set[str]:
    bind = op.get_bind()
    inspector = inspect(bind)
    if kind == "check":
        constraints = inspector.get_check_constraints(table)
    else:
        constraints = inspector.get_unique_constraints(table)
    return {constraint["name"] or "" for constraint in constraints}


def _rename_constraint(rename: ConstraintRename, source: str, target: str) -> None:
    names = _names(rename.table, rename.kind)
    if source not in names:
        return

    with op.batch_alter_table(rename.table, schema=None) as batch_op:
        batch_op.drop_constraint(op.f(source), type_=rename.kind)
        if target in names:
            return
        if rename.kind == "check":
            if rename.predicate is None:
                raise RuntimeError(f"missing predicate for {rename.table}.{source}")
            batch_op.create_check_constraint(op.f(target), rename.predicate)
        else:
            batch_op.create_unique_constraint(op.f(target), list(rename.columns))


def upgrade() -> None:
    """Move doubled-prefix constraint names to their canonical names."""
    for rename in _RENAMES:
        _rename_constraint(rename, rename.doubled_name, rename.canonical_name)


def downgrade() -> None:
    """Restore the doubled-prefix names expected by the previous model tokens."""
    for rename in reversed(_RENAMES):
        _rename_constraint(rename, rename.canonical_name, rename.doubled_name)
