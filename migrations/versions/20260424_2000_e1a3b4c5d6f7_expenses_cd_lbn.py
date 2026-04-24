"""expenses cd-lbn

Revision ID: e1a3b4c5d6f7
Revises: d0f2a3b4c5e6
Create Date: 2026-04-24 20:00:00.000000

Creates the three expense-context tables that back the claim
submit + approve + reimburse flow (see
``docs/specs/02-domain-model.md`` §"Core entities (by document)"
(§09 row) and ``docs/specs/09-time-payroll-expenses.md`` §"Expense
claims"):

* ``expense_claim`` — one reimbursement request per claim, keyed
  by ``(workspace, work_engagement)``. Carries the purchase
  payload (``vendor``, ``purchased_at``, ``currency``,
  ``total_amount_cents``, ``category``, optional ``property_id``,
  Markdown ``note_md``), the ``state`` lifecycle enum
  (``draft | submitted | approved | rejected | reimbursed``), the
  approval / reimbursement snapshots (``exchange_rate_to_default``,
  ``owed_destination_id`` + ``owed_currency`` + ``owed_amount_cents``
  + ``owed_exchange_rate`` + ``owed_rate_source``,
  ``reimbursement_destination_id``), the ``decided_by`` /
  ``decided_at`` / ``decision_note_md`` audit pair, the
  ``llm_autofill_json`` + ``autofill_confidence_overall`` OCR
  payload, and the ``deleted_at`` soft-delete pointer. Money is
  integer cents (BigInteger per §02 §"Money"); cross-currency rates
  use ``Numeric(18, 8)`` to hold EUR-pivot crosses past the two-
  decimal minor-unit precision. CHECK constraints clamp the state +
  category enums, ``LENGTH(currency) = 3`` and
  ``LENGTH(owed_currency) = 3`` (ISO-4217), and
  ``total_amount_cents >= 0`` / ``owed_amount_cents IS NULL OR >=
  0`` / ``autofill_confidence_overall BETWEEN 0 AND 1``.
  ``workspace_id`` FK cascades (sweeping a workspace sweeps its
  expense history, §15 export snapshots first);
  ``work_engagement_id`` FK uses ``RESTRICT`` to preserve the
  payroll-law evidence trail (§09 §"Expense claims", §15 §"Right to
  erasure"). ``decided_by`` / ``owed_destination_id`` /
  ``reimbursement_destination_id`` are plain ``String`` soft-refs —
  the ``payout_destination`` table does not exist yet (see §09
  §"Payout destinations"); the ``work_engagement.pay_destination_id``
  convention covers the same gap. ``property_id`` is a plain
  ``String`` soft-ref matching ``shift.property_id`` /
  ``movement.occurrence_id``. Two hot-path indexes: ``(workspace_id,
  state)`` for the manager "claims awaiting approval" inbox and
  ``(workspace_id, work_engagement_id, submitted_at)`` for the
  worker "my claims, newest first" view.

* ``expense_line`` — one line item per receipt row, keyed by
  ``(workspace, claim)``. Carries ``description``, a fractional
  ``quantity`` (``Numeric(18, 4)``, matches
  ``inventory_item.current_qty``), ``unit_price_cents`` /
  ``line_total_cents`` (both ``BigInteger``, both in the claim's
  currency), the ``source`` enum (``ocr | manual``), an
  ``edited_by_user`` provenance bit (§09 §"LLM accuracy" — the
  ``source`` column stays ``ocr`` even after a user edit), and a
  plain ``String`` soft-ref ``asset_id`` into §21's asset table
  (not yet in the schema). CHECK constraints clamp the enum and
  ``quantity >= 0`` / ``unit_price_cents >= 0`` /
  ``line_total_cents >= 0``. ``workspace_id`` and ``claim_id`` FKs
  both cascade — deleting a claim drops its lines. The
  ``(workspace_id, claim_id)`` index powers the "fetch all lines
  for this claim" read path.
  **App-layer invariants** (not enforced at the DB): the line total
  is ``unit_price_cents * quantity`` rounded half-to-even at the
  claim-currency minor-unit precision, and the sum of live lines
  for a claim equals the claim's ``total_amount_cents``. SQLite's
  CHECK dialect cannot evaluate a Decimal multiply portably and
  the sum-invariant requires currency-aware rounding, so the rules
  are enforced in the domain layer — same pattern as
  ``payslip.net_cents``.

* ``expense_attachment`` — one file per attachment, keyed by
  ``(workspace, claim)``. Carries a content-addressed ``blob_hash``
  (soft-ref into blob storage — the §02 §"Shared tables" ``file``
  table has not landed yet; matches ``evidence.blob_hash`` and
  ``payslip.pdf_blob_hash``), the ``kind`` enum (``receipt |
  invoice | other``), an optional ``pages`` int (populated for
  multi-page PDFs only), and ``created_at``. CHECK constraints
  clamp the enum and ``pages IS NULL OR pages >= 1``.
  ``workspace_id`` and ``claim_id`` FKs both cascade. The
  ``(workspace_id, claim_id)`` index powers the "every attachment
  for this claim" read path.

All three tables are workspace-scoped and registered in
:mod:`app.tenancy.registry` via ``app/adapters/db/expenses/__init__.py``.
The tables are created in dependency order (``expense_claim`` first
because ``expense_line`` and ``expense_attachment`` both FK into
it); ``downgrade()`` drops in reverse.

**Deviation from cd-lbn's prose (NOTE FOR REVIEWER).** The task
body describes a two-table ``Expense`` + ``Receipt`` shape with a
``reimbursed_via`` payment-channel enum, a per-attachment
``ocr_json`` + ``ocr_confidence`` pair, and a ``claimant_user_id``
FK. §02 §"Core entities" and §09 §"Model" record the canonical
three-table shape landed here (``expense_claim`` + ``expense_line``
+ ``expense_attachment``), with OCR scoped to the whole claim
(``llm_autofill_json``), reimbursement routed via
``payout_destination`` rather than a free-text channel enum, and
the claim pinned to a ``work_engagement_id`` so the same person on
different workspaces bills / accrues independently (§02 §"Core
entities" — "all pay-pipeline rows reference ``work_engagement_id``
(not ``user_id`` directly)"). The spec is authoritative per the
Coder brief; this migration follows the spec and defers the
``payout_destination`` / ``file`` / ``asset`` hard-FK promotions to
later migrations (the affected columns land as plain ``String``
soft-refs, matching the ``work_engagement.pay_destination_id``
convention).

**Reversibility.** ``downgrade()`` drops the secondary indexes
first (so SQLite's batch rebuild doesn't fight a lingering index on
a renamed table), then each table in reverse FK order. The CHECK
constraints disappear with their tables. No data-loss concern
beyond the obvious "rolling back drops the expense ledger" — a
real rollback should dump the tables first.

See ``docs/specs/02-domain-model.md`` §"Core entities (by
document)" (§09 row), §"Money", §"Enums"; and
``docs/specs/09-time-payroll-expenses.md`` §"Expense claims".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1a3b4c5d6f7"
down_revision: str | Sequence[str] | None = "d0f2a3b4c5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ``expense_claim`` lands first — ``expense_line`` and
    # ``expense_attachment`` both FK into it.
    op.create_table(
        "expense_claim",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("work_engagement_id", sa.String(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("vendor", sa.String(), nullable=False),
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("total_amount_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "exchange_rate_to_default",
            sa.Numeric(precision=18, scale=8),
            nullable=True,
        ),
        sa.Column("owed_destination_id", sa.String(), nullable=True),
        sa.Column("owed_currency", sa.String(), nullable=True),
        sa.Column("owed_amount_cents", sa.BigInteger(), nullable=True),
        sa.Column(
            "owed_exchange_rate",
            sa.Numeric(precision=18, scale=8),
            nullable=True,
        ),
        sa.Column("owed_rate_source", sa.String(), nullable=True),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=True),
        sa.Column(
            "note_md",
            sa.String(),
            nullable=False,
            server_default="",
        ),
        sa.Column("llm_autofill_json", sa.JSON(), nullable=True),
        sa.Column(
            "autofill_confidence_overall",
            sa.Numeric(precision=3, scale=2),
            nullable=True,
        ),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note_md", sa.String(), nullable=True),
        sa.Column("reimbursement_destination_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "autofill_confidence_overall IS NULL "
            "OR (autofill_confidence_overall >= 0 "
            "AND autofill_confidence_overall <= 1)",
            name=op.f("ck_expense_claim_autofill_confidence_overall_bounds"),
        ),
        sa.CheckConstraint(
            "category IN ('supplies', 'fuel', 'food', 'transport', "
            "'maintenance', 'other')",
            name=op.f("ck_expense_claim_category"),
        ),
        sa.CheckConstraint(
            "LENGTH(currency) = 3",
            name=op.f("ck_expense_claim_currency_length"),
        ),
        sa.CheckConstraint(
            "owed_amount_cents IS NULL OR owed_amount_cents >= 0",
            name=op.f("ck_expense_claim_owed_amount_cents_nonneg"),
        ),
        sa.CheckConstraint(
            "owed_currency IS NULL OR LENGTH(owed_currency) = 3",
            name=op.f("ck_expense_claim_owed_currency_length"),
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'submitted', 'approved', 'rejected', 'reimbursed')",
            name=op.f("ck_expense_claim_state"),
        ),
        sa.CheckConstraint(
            "total_amount_cents >= 0",
            name=op.f("ck_expense_claim_total_amount_cents_nonneg"),
        ),
        sa.ForeignKeyConstraint(
            ["work_engagement_id"],
            ["work_engagement.id"],
            name=op.f("fk_expense_claim_work_engagement_id_work_engagement"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_expense_claim_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expense_claim")),
    )
    with op.batch_alter_table("expense_claim", schema=None) as batch_op:
        batch_op.create_index(
            "ix_expense_claim_workspace_engagement_submitted",
            ["workspace_id", "work_engagement_id", "submitted_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_expense_claim_workspace_state",
            ["workspace_id", "state"],
            unique=False,
        )

    op.create_table(
        "expense_line",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("claim_id", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column(
            "quantity",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
        ),
        sa.Column("unit_price_cents", sa.BigInteger(), nullable=False),
        sa.Column("line_total_cents", sa.BigInteger(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column(
            "edited_by_user",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
        sa.CheckConstraint(
            "line_total_cents >= 0",
            name=op.f("ck_expense_line_line_total_cents_nonneg"),
        ),
        sa.CheckConstraint(
            "quantity >= 0",
            name=op.f("ck_expense_line_quantity_nonneg"),
        ),
        sa.CheckConstraint(
            "source IN ('ocr', 'manual')",
            name=op.f("ck_expense_line_source"),
        ),
        sa.CheckConstraint(
            "unit_price_cents >= 0",
            name=op.f("ck_expense_line_unit_price_cents_nonneg"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["expense_claim.id"],
            name=op.f("fk_expense_line_claim_id_expense_claim"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_expense_line_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expense_line")),
    )
    with op.batch_alter_table("expense_line", schema=None) as batch_op:
        batch_op.create_index(
            "ix_expense_line_workspace_claim",
            ["workspace_id", "claim_id"],
            unique=False,
        )

    op.create_table(
        "expense_attachment",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("claim_id", sa.String(), nullable=False),
        sa.Column("blob_hash", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("pages", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('receipt', 'invoice', 'other')",
            name=op.f("ck_expense_attachment_kind"),
        ),
        sa.CheckConstraint(
            "pages IS NULL OR pages >= 1",
            name=op.f("ck_expense_attachment_pages_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["expense_claim.id"],
            name=op.f("fk_expense_attachment_claim_id_expense_claim"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_expense_attachment_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expense_attachment")),
    )
    with op.batch_alter_table("expense_attachment", schema=None) as batch_op:
        batch_op.create_index(
            "ix_expense_attachment_workspace_claim",
            ["workspace_id", "claim_id"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema.

    Drop indexes first (so SQLite's batch rebuild doesn't fight a
    lingering index on a renamed table), then each table in reverse
    FK dependency order (children before parent). The CHECK
    constraints disappear with their tables.
    """
    with op.batch_alter_table("expense_attachment", schema=None) as batch_op:
        batch_op.drop_index("ix_expense_attachment_workspace_claim")
    op.drop_table("expense_attachment")

    with op.batch_alter_table("expense_line", schema=None) as batch_op:
        batch_op.drop_index("ix_expense_line_workspace_claim")
    op.drop_table("expense_line")

    with op.batch_alter_table("expense_claim", schema=None) as batch_op:
        batch_op.drop_index("ix_expense_claim_workspace_state")
        batch_op.drop_index("ix_expense_claim_workspace_engagement_submitted")
    op.drop_table("expense_claim")
