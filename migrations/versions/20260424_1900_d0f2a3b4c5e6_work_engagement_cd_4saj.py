"""work_engagement_cd_4saj

Revision ID: d0f2a3b4c5e6
Revises: c9e1f2a3b4d5
Create Date: 2026-04-24 19:00:00.000000

Lands the §02 ``work_engagement`` table — the per-(user, workspace)
employment relationship that carries the pay pipeline (§09). Fills
in the ``user_workspace.source = 'work_engagement'`` half of the
membership refresh (cd-jpa) and unblocks cd-dv2 (employees service)
— §05 "Work engagement" records "creating the first
``user_work_role`` for a (user, workspace) creates the
``work_engagement`` row if missing", and that invariant cannot be
expressed without the table.

Shape (matches §02 lines 843-873):

* ``id`` ULID PK (plain ``String``, matching the cd-yesa
  harmonisation).
* ``user_id`` text NOT NULL — **soft reference** to ``users.id``,
  matching the sibling ``user_workspace.user_id`` /
  ``user_work_role.user_id`` rationale (no FK until the broader
  tenancy-join refactor lands).
* ``workspace_id`` FK ``workspace.id`` ON DELETE CASCADE NOT NULL —
  denormalised so the ORM tenant filter rides a local column.
* ``engagement_kind`` text NOT NULL — CHECK IN (``payroll``,
  ``contractor``, ``agency_supplied``). Matches §22 "Engagement
  kinds".
* ``supplier_org_id`` text NULL — **soft reference** to the future
  ``organization`` table (no FK yet; see "Missing-prereq note"
  below). Required iff ``engagement_kind = 'agency_supplied'``
  (enforced by CHECK below).
* ``pay_destination_id`` text NULL — **soft reference** to the
  future ``pay_destination`` table. Default payout target for
  payslips / vendor invoices.
* ``reimbursement_destination_id`` text NULL — **soft reference** to
  the future ``pay_destination`` table. Default target for expense
  reimbursements; NULL falls back to ``pay_destination_id`` at
  payout time (§09 "Expense claim").
* ``started_on`` date NOT NULL — engagement start.
* ``archived_on`` date NULL — engagement end; archives the pay
  pipeline, not the user. NULL = active. The partial UNIQUE below
  pivots on this column.
* ``notes_md`` text NOT NULL DEFAULT ``''`` — manager-visible.
  Empty-string default matches the sibling cd-5kv4 (``work_role``)
  and cd-pjm (``messaging``) conventions so SQLite + PG round-trip
  the same literal.
* ``created_at`` tstz NOT NULL.
* ``updated_at`` tstz NOT NULL — the audit-log writer reads this
  column to emit the correct ``after_json`` shape (§02 "audit_log").

Constraints:

* ``ck_work_engagement_engagement_kind`` — CHECK on the three
  allowed enum values.
* ``ck_work_engagement_supplier_org_pairing`` — biconditional CHECK:
  ``supplier_org_id`` is populated iff ``engagement_kind =
  'agency_supplied'``. §02 records this invariant both directions —
  an ``agency_supplied`` row without a supplier is a half-wired
  pipeline; a ``payroll`` / ``contractor`` row carrying a supplier
  reference is a UX bug waiting to happen.
* Partial UNIQUE ``uq_work_engagement_user_workspace_active`` on
  ``(user_id, workspace_id) WHERE archived_on IS NULL`` — §02's
  "at most one active engagement per (user, workspace)" invariant.
  Archived rows co-exist happily. SQLite 3.8+ and PG both support
  partial indexes; the Alembic op uses dialect-specific
  ``sqlite_where`` / ``postgresql_where`` kwargs so the same DDL
  lands on both backends. Schema-fingerprint parity
  (``tests/integration/test_schema_parity.py``) confirms both
  sides agree on the index columns.

Hot-path indexes:

* ``ix_work_engagement_workspace_user`` on ``(workspace_id,
  user_id)`` — "what engagements does this user hold here?" hot
  path.
* ``ix_work_engagement_workspace_archived`` on ``(workspace_id,
  archived_on)`` — "who is currently engaged in this workspace?"
  roster view; trailing ``archived_on`` lets the planner skip
  archived rows.

**Missing-prereq note (IMPORTANT — READ BEFORE PROMOTING).**
The ``organization_id`` and ``pay_destination_id`` FKs the §02 spec
eventually wants are **not** declared here. Those tables do not
exist yet in the schema; declaring real FKs now would explode the
migration. ``supplier_org_id`` / ``pay_destination_id`` /
``reimbursement_destination_id`` therefore land as plain ``String``
columns with no referential integrity, matching the
``user_workspace.user_id`` / ``user_work_role.pay_rule_id`` /
``role_grant.scope_property_id`` conventions. **cd-0ro4** is the
follow-up Beads task that promotes these columns into real FKs once
the parent tables land (``bd show cd-0ro4`` for the full brief).

**Reversibility.** ``downgrade()`` drops the secondary indexes
first, then the partial UNIQUE, then the table itself. The CHECK
constraints disappear with the table. No data-loss concern beyond
the obvious "rolling back drops the engagement catalogue" — a real
rollback should dump the table first.

See ``docs/specs/02-domain-model.md`` §"work_engagement",
``docs/specs/05-employees-and-roles.md`` §"Work engagement",
``docs/specs/22-clients-and-vendors.md`` §"Engagement kinds".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d0f2a3b4c5e6"
down_revision: str | Sequence[str] | None = "c9e1f2a3b4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ``work_engagement`` — per-(user, workspace) employment row.
    # CHECK constraints land with the table so SQLite materialises
    # them in the initial ``CREATE TABLE`` rather than forcing a
    # table-copy via ``batch_alter_table``.
    op.create_table(
        "work_engagement",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("engagement_kind", sa.String(), nullable=False),
        # Soft-ref columns — see module-header "Missing-prereq note".
        sa.Column("supplier_org_id", sa.String(), nullable=True),
        sa.Column("pay_destination_id", sa.String(), nullable=True),
        sa.Column("reimbursement_destination_id", sa.String(), nullable=True),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("archived_on", sa.Date(), nullable=True),
        sa.Column(
            "notes_md",
            sa.String(),
            nullable=False,
            server_default="",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "engagement_kind IN ('payroll', 'contractor', 'agency_supplied')",
            name=op.f("ck_work_engagement_engagement_kind"),
        ),
        # Biconditional CHECK — §02 records "``supplier_org_id``
        # required iff ``engagement_kind = 'agency_supplied'``". Both
        # directions enforced: agency without supplier is a
        # half-wired pipeline, non-agency with supplier is a UX bug
        # waiting to happen.
        sa.CheckConstraint(
            "(engagement_kind = 'agency_supplied' "
            "AND supplier_org_id IS NOT NULL) "
            "OR (engagement_kind != 'agency_supplied' "
            "AND supplier_org_id IS NULL)",
            name=op.f("ck_work_engagement_supplier_org_pairing"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_work_engagement_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_engagement")),
    )
    # Partial UNIQUE — §02's "at most one active engagement per
    # (user, workspace)" invariant. SQLite 3.8+ and PG both honour
    # the ``WHERE`` predicate; the dialect-specific kwargs pass
    # through to the DDL emitter on the matching backend. Emitted
    # at the top level (not inside ``batch_alter_table``) because
    # the kwargs don't survive a batch rebuild cleanly — see the
    # cd-22e ``uq_occurrence_schedule_scheduled_for_local`` op for
    # the same idiom.
    op.create_index(
        "uq_work_engagement_user_workspace_active",
        "work_engagement",
        ["user_id", "workspace_id"],
        unique=True,
        sqlite_where=sa.text("archived_on IS NULL"),
        postgresql_where=sa.text("archived_on IS NULL"),
    )
    with op.batch_alter_table("work_engagement", schema=None) as batch_op:
        # "What engagements does this user hold here?" hot path.
        batch_op.create_index(
            "ix_work_engagement_workspace_user",
            ["workspace_id", "user_id"],
            unique=False,
        )
        # "Who is currently engaged in this workspace?" roster view.
        # Trailing ``archived_on`` lets the planner skip archived
        # rows when the manager filters on ``IS NULL``.
        batch_op.create_index(
            "ix_work_engagement_workspace_archived",
            ["workspace_id", "archived_on"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema.

    Drop the secondary indexes first (so SQLite's batch rebuild
    doesn't fight a lingering index on a renamed table), then the
    partial UNIQUE, then the table. The CHECK constraints disappear
    with the table itself. No data-loss concern beyond "rolling back
    drops the engagement catalogue" — a real rollback should dump
    the table first.
    """
    with op.batch_alter_table("work_engagement", schema=None) as batch_op:
        batch_op.drop_index("ix_work_engagement_workspace_archived")
        batch_op.drop_index("ix_work_engagement_workspace_user")
    # The partial UNIQUE was emitted at the top level on upgrade; drop
    # it at the top level too so the dialect-specific ``WHERE`` path
    # matches.
    op.drop_index(
        "uq_work_engagement_user_workspace_active",
        table_name="work_engagement",
    )
    op.drop_table("work_engagement")
