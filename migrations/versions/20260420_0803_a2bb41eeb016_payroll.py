"""payroll

Revision ID: a2bb41eeb016
Revises: 41a434d066d8
Create Date: 2026-04-20 08:03:37.429915

Creates the three payroll-context tables that back the pay-rule +
period-close + payslip-issue flows (see
``docs/specs/02-domain-model.md`` §"pay_rule", §"pay_period",
§"payslip" and ``docs/specs/09-time-payroll-expenses.md`` §"Pay
rules", §"Pay period", §"Payslip"):

* ``pay_rule`` — the hourly rate + overtime / night / weekend
  multipliers for a ``(workspace, user)`` pair over an
  ``effective_from`` / ``effective_to`` window. Money is stored as
  integer cents per hour (BigInteger for BHD-class 3-dp minor
  units); multipliers are ``Numeric(4, 2)`` Decimals for portability
  across SQLite and Postgres. CHECK constraints clamp the v1
  invariants: ``LENGTH(currency) = 3`` (ISO 4217),
  ``base_cents_per_hour >= 0``, each multiplier ``>= 1``.
  ``workspace_id`` FK cascades — sweeping a workspace sweeps its
  payroll history (§15 export snapshots first). ``user_id`` FK uses
  ``RESTRICT`` to preserve labour-law records (§09 §"Labour-law
  compliance"); the normal erasure path is
  ``crewday admin purge --person`` (§15) which anonymises the user
  in place, keeping references valid. ``created_by`` is a plain
  :class:`str` soft-ref — see the module docstring. The
  ``(workspace_id, user_id, effective_from)`` index rides the
  "current rule as of a moment" query the period-close worker runs
  on every close.

* ``pay_period`` — one open-ended window per workspace with a
  three-state lifecycle (``open | locked | paid``) matching the
  canonical ``pay_period_status`` enum in §02. CHECK on
  ``ends_at > starts_at`` blocks zero-or-negative windows. UNIQUE
  ``(workspace_id, starts_at, ends_at)`` enforces the acceptance
  criterion: a workspace cannot have two April 2026 periods.
  ``workspace_id`` FK cascades. ``locked_by`` is a soft-ref
  :class:`str` — same rationale as ``shift.approved_by``. The
  richer §09 per-engagement + divergent-frequency surface lands
  with the period-close domain follow-up.

* ``payslip`` — one computed pay document per ``(pay_period, user)``
  pair. UNIQUE ``(pay_period_id, user_id)`` enforces the v1
  acceptance criterion. ``pay_period_id`` FK cascades — deleting
  an ``open`` period (legal only at that state, domain-enforced)
  sweeps its draft payslips. ``user_id`` FK uses ``RESTRICT``
  (same §09 / §15 rationale as ``pay_rule``). Hours are
  ``Numeric(10, 2)`` Decimals; money is integer cents (BigInteger).
  ``deductions_cents`` is a ``{reason: cents}`` JSON dict
  (defaulted to ``{}`` by the mapped class). CHECK constraints
  clamp ``shift_hours_decimal >= 0``, ``overtime_hours_decimal >=
  0``, ``gross_cents >= 0``. ``net_cents`` is allowed to go
  negative (a cash-advance repayment is legitimate); the
  ``net = gross - sum(deductions)`` rule is an app-layer
  invariant (the closer's snapshot path), since SQLite's JSON
  aggregate functions are not CHECK-safe.

All three tables are workspace-scoped. Tables are created in a
stable deterministic order matching the dependency chain
(``pay_period`` before ``payslip`` because payslip has an FK into
it); ``downgrade()`` drops in reverse.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2bb41eeb016"
down_revision: str | Sequence[str] | None = "41a434d066d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "pay_rule",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("base_cents_per_hour", sa.BigInteger(), nullable=False),
        sa.Column(
            "overtime_multiplier",
            sa.Numeric(precision=4, scale=2),
            nullable=False,
        ),
        sa.Column(
            "night_multiplier",
            sa.Numeric(precision=4, scale=2),
            nullable=False,
        ),
        sa.Column(
            "weekend_multiplier",
            sa.Numeric(precision=4, scale=2),
            nullable=False,
        ),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "LENGTH(currency) = 3",
            name=op.f("ck_pay_rule_currency_length"),
        ),
        sa.CheckConstraint(
            "base_cents_per_hour >= 0",
            name=op.f("ck_pay_rule_base_cents_per_hour_nonneg"),
        ),
        sa.CheckConstraint(
            "night_multiplier >= 1",
            name=op.f("ck_pay_rule_night_multiplier_min"),
        ),
        sa.CheckConstraint(
            "overtime_multiplier >= 1",
            name=op.f("ck_pay_rule_overtime_multiplier_min"),
        ),
        sa.CheckConstraint(
            "weekend_multiplier >= 1",
            name=op.f("ck_pay_rule_weekend_multiplier_min"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_pay_rule_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_pay_rule_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pay_rule")),
    )
    with op.batch_alter_table("pay_rule", schema=None) as batch_op:
        batch_op.create_index(
            "ix_pay_rule_workspace_user_effective_from",
            ["workspace_id", "user_id", "effective_from"],
            unique=False,
        )

    op.create_table(
        "pay_period",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ends_at > starts_at",
            name=op.f("ck_pay_period_ends_after_starts"),
        ),
        sa.CheckConstraint(
            "state IN ('open', 'locked', 'paid')",
            name=op.f("ck_pay_period_state"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_pay_period_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pay_period")),
        sa.UniqueConstraint(
            "workspace_id",
            "starts_at",
            "ends_at",
            name="uq_pay_period_workspace_window",
        ),
    )

    op.create_table(
        "payslip",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("pay_period_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column(
            "shift_hours_decimal",
            sa.Numeric(precision=10, scale=2),
            nullable=False,
        ),
        sa.Column(
            "overtime_hours_decimal",
            sa.Numeric(precision=10, scale=2),
            nullable=False,
        ),
        sa.Column("gross_cents", sa.BigInteger(), nullable=False),
        sa.Column("deductions_cents", sa.JSON(), nullable=False),
        sa.Column("net_cents", sa.BigInteger(), nullable=False),
        sa.Column("pdf_blob_hash", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "gross_cents >= 0",
            name=op.f("ck_payslip_gross_cents_nonneg"),
        ),
        sa.CheckConstraint(
            "overtime_hours_decimal >= 0",
            name=op.f("ck_payslip_overtime_hours_decimal_nonneg"),
        ),
        sa.CheckConstraint(
            "shift_hours_decimal >= 0",
            name=op.f("ck_payslip_shift_hours_decimal_nonneg"),
        ),
        sa.ForeignKeyConstraint(
            ["pay_period_id"],
            ["pay_period.id"],
            name=op.f("fk_payslip_pay_period_id_pay_period"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_payslip_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_payslip_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payslip")),
        sa.UniqueConstraint(
            "pay_period_id",
            "user_id",
            name="uq_payslip_pay_period_user",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("payslip")
    op.drop_table("pay_period")

    with op.batch_alter_table("pay_rule", schema=None) as batch_op:
        batch_op.drop_index("ix_pay_rule_workspace_user_effective_from")
    op.drop_table("pay_rule")
