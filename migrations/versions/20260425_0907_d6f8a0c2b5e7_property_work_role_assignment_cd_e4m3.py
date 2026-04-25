"""property_work_role_assignment cd-e4m3

Revision ID: d6f8a0c2b5e7
Revises: c5e7f9b1d3a4
Create Date: 2026-04-25 09:07:00.000000

Lands the §05 ``property_work_role_assignment`` table — the per-
property pinning of a :class:`UserWorkRole`. Without this row a
:class:`UserWorkRole` is "generalist" — eligible for every property
in the workspace; with one or more rows the user is narrowed to the
listed properties only (§05 "Property work role assignment").

Shape (matches §05 lines 116-139):

* ``id`` ULID PK (plain ``String``, matching the cd-yesa
  harmonisation).
* ``workspace_id`` FK ``workspace.id`` ON DELETE CASCADE NOT NULL —
  denormalised so the ORM tenant filter rides a local column without
  threading a join through ``user_work_role`` on every read. Same
  pattern as ``work_engagement`` / ``user_work_role``.
* ``user_work_role_id`` FK ``user_work_role.id`` ON DELETE CASCADE
  NOT NULL — hard-deleting a user_work_role sweeps every assignment
  row.
* ``property_id`` FK ``property.id`` ON DELETE CASCADE NOT NULL —
  hard-deleting the property sweeps the assignment, matching the
  sibling ``unit`` / ``area`` / ``property_closure`` cascade.
* ``schedule_ruleset_id`` text NULL — **soft reference** to the
  future ``schedule_ruleset`` table (§06 "Schedule ruleset (per-
  property rota)"). NULL = no rota declared; eligibility falls back
  to ``user_weekly_availability`` alone. Plain ``String`` matches
  the ``user_work_role.pay_rule_id`` /
  ``role_grant.scope_property_id`` convention; once the parent
  table lands a follow-up migration may promote this into a real FK
  without disturbing domain callers.
* ``property_pay_rule_id`` FK ``pay_rule.id`` ON DELETE SET NULL
  NULL — per-property rate override. NULL = inherit the
  engagement-level pay rule. ``ON DELETE SET NULL`` so losing the
  pay rule drops the override but keeps the assignment alive (the
  engagement rule re-applies).
* ``created_at`` / ``updated_at`` tstz NOT NULL.
* ``deleted_at`` tstz NULL — soft-delete tombstone.

Indexes:

* Partial UNIQUE ``uq_property_work_role_assignment_role_property_active``
  on ``(user_work_role_id, property_id) WHERE deleted_at IS NULL``
  — identity key. One live row per (user_work_role, property);
  variation in *when* the user works the property is expressed by
  ``schedule_ruleset_slot`` rows under the referenced ruleset, not
  by stacking assignment rows. Tombstoned rows are excluded so an
  archive + re-pin works without colliding with the historical row.
  SQLite 3.8+ and PG both honour the ``WHERE`` predicate; the
  dialect-specific ``sqlite_where`` / ``postgresql_where`` kwargs
  pass through to the DDL emitter on the matching backend (same
  idiom as ``uq_work_engagement_user_workspace_active``).
* ``ix_property_work_role_assignment_workspace_deleted`` on
  ``(workspace_id, deleted_at)`` — "list live assignments for this
  workspace" hot path. Leading ``workspace_id`` carries the tenant
  filter; trailing ``deleted_at`` lets the planner skip tombstones.
* ``ix_property_work_role_assignment_workspace_user_work_role`` on
  ``(workspace_id, user_work_role_id)`` — "every assignment of this
  user_work_role" view (the employees surface).
* ``ix_property_work_role_assignment_workspace_property`` on
  ``(workspace_id, property_id)`` — "every assignment at this
  property" view (the property's workforce panel).

**Domain-enforced invariants** (write-path; not expressed in DDL):

1. ``workspace_id`` must equal the parent ``user_work_role``'s
   ``workspace_id``. Cross-workspace borrowing is already blocked by
   §05 "User work role"; the redundancy is explicit here so a
   future bulk-loader can't slip a row through.
2. ``property_id`` must point at a property that is linked to
   ``workspace_id`` through a live ``property_workspace`` row — a
   workspace cannot pin a role to a property it doesn't operate
   (§02 / §05 ``property_workspace`` ownership). Validated at write
   time by the future API service (cd-za6n).

**Reversibility.** ``downgrade()`` drops the secondary indexes
first (so SQLite's batch rebuild doesn't fight a lingering index on
a renamed table), then the partial UNIQUE at the top level (its
dialect-specific ``WHERE`` kwargs don't survive a batch rebuild
cleanly — same idiom as ``uq_work_engagement_user_workspace_active``
in cd-4saj), then the table itself. The FKs disappear with the
table. No data-loss concern beyond "rolling back drops the
assignment catalogue" — a real rollback should dump the table first.

See ``docs/specs/05-employees-and-roles.md`` §"Property work role
assignment", ``docs/specs/02-domain-model.md`` §"People, work
roles, engagements", and ``docs/specs/06-tasks-and-scheduling.md``
§"Schedule ruleset (per-property rota)".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6f8a0c2b5e7"
down_revision: str | Sequence[str] | None = "c5e7f9b1d3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ``property_work_role_assignment`` — pins a user_work_role to a
    # property. CHECK / FK constraints land with the table so SQLite
    # materialises them in the initial ``CREATE TABLE`` rather than
    # forcing a table-copy via ``batch_alter_table``.
    op.create_table(
        "property_work_role_assignment",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_work_role_id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=False),
        # Soft-ref column — see module-header rationale.
        sa.Column("schedule_ruleset_id", sa.String(), nullable=True),
        sa.Column("property_pay_rule_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_property_work_role_assignment_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_work_role_id"],
            ["user_work_role.id"],
            name=op.f(
                "fk_property_work_role_assignment_user_work_role_id_user_work_role"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_property_work_role_assignment_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["property_pay_rule_id"],
            ["pay_rule.id"],
            name=op.f("fk_property_work_role_assignment_property_pay_rule_id_pay_rule"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_property_work_role_assignment")),
    )
    # Partial UNIQUE — identity key on the live row. Emitted at the
    # top level (not inside ``batch_alter_table``) because the
    # dialect-specific ``WHERE`` kwargs don't survive a batch rebuild
    # cleanly — see the cd-4saj
    # ``uq_work_engagement_user_workspace_active`` op for the same
    # idiom.
    op.create_index(
        "uq_property_work_role_assignment_role_property_active",
        "property_work_role_assignment",
        ["user_work_role_id", "property_id"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    with op.batch_alter_table("property_work_role_assignment", schema=None) as batch_op:
        # "List live assignments for this workspace" hot path. Leading
        # ``workspace_id`` carries the tenant filter; trailing
        # ``deleted_at`` lets the planner skip tombstones.
        batch_op.create_index(
            "ix_property_work_role_assignment_workspace_deleted",
            ["workspace_id", "deleted_at"],
            unique=False,
        )
        # "Every assignment of this user_work_role" — the employees
        # surface walks this index to display per-user property
        # narrowings.
        batch_op.create_index(
            "ix_property_work_role_assignment_workspace_user_work_role",
            ["workspace_id", "user_work_role_id"],
            unique=False,
        )
        # "Every assignment at this property" — the property's
        # workforce panel walks this index to list the workers
        # operating the place.
        batch_op.create_index(
            "ix_property_work_role_assignment_workspace_property",
            ["workspace_id", "property_id"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema.

    Drop the secondary indexes first (so SQLite's batch rebuild
    doesn't fight a lingering index on a renamed table), then the
    partial UNIQUE at the top level (its ``WHERE`` kwargs don't
    survive a batch rebuild cleanly), then the table itself. The FKs
    disappear with the table. No data-loss concern beyond the
    obvious "rolling back drops the assignment catalogue" — a real
    rollback should dump the table first.
    """
    with op.batch_alter_table("property_work_role_assignment", schema=None) as batch_op:
        batch_op.drop_index("ix_property_work_role_assignment_workspace_property")
        batch_op.drop_index("ix_property_work_role_assignment_workspace_user_work_role")
        batch_op.drop_index("ix_property_work_role_assignment_workspace_deleted")
    # The partial UNIQUE was emitted at the top level on upgrade; drop
    # it at the top level too so the dialect-specific ``WHERE`` path
    # matches.
    op.drop_index(
        "uq_property_work_role_assignment_role_property_active",
        table_name="property_work_role_assignment",
    )
    op.drop_table("property_work_role_assignment")
