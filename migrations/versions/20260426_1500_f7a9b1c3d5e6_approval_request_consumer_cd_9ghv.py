"""approval_request_consumer_cd_9ghv

Revision ID: f7a9b1c3d5e6
Revises: e6f8a0b2c4d5
Create Date: 2026-04-26 15:00:00.000000

Lands the column promotions the §11 HITL approval consumer (cd-9ghv)
needs on the ``approval_request`` table. The cd-cm5 v1 slice landed
the four core columns the runtime writes (``action_json`` /
``status`` / ``decided_by`` / ``rationale_md`` / ``decided_at``) plus
the bare ``requester_actor_id`` FK; the consumer pipeline needs five
more fields out as first-class columns so the API decision endpoints
+ TTL worker + SSE notifier can index, filter, and update them
without round-tripping through the JSON payload.

Shape:

* ``expires_at TIMESTAMPTZ NULL`` — TTL anchor. §11 "TTL" pins a
  default of 7 days from ``created_at``; a worker tick walks the
  table every 15 min and flips a row past its ``expires_at`` to
  ``status='timed_out'``. Nullable so legacy rows (the cd-cm5
  baseline before this migration) survive without a rewrite — the
  consumer treats ``NULL`` as "no expiry recorded; do not auto-
  expire".

* ``result_json JSON NULL`` — populated on approve when the runtime
  re-dispatches the recorded tool call. §11 "Model" lists this
  alongside ``executed_at``; v1 collapses the two into the existing
  ``status='approved'`` terminal state and stores the result here.
  The /approvals desk renders the executed result so the operator
  can confirm the action landed without leaving the queue.

* ``decision_note_md TEXT NULL`` — the reviewer's free-form note,
  promoted off the existing ``rationale_md`` column. Both columns
  carry the same shape; the new column has the spec name (§11
  "Model"). The runtime + consumer write to ``decision_note_md``
  going forward; ``rationale_md`` stays on the row to keep cd-cm5-
  era rows readable. A future cleanup migration can fold the two
  once every codepath reads from the new column.

* ``inline_channel TEXT NULL`` — the originating chat channel from
  the ``X-Agent-Channel`` request header (§11 "Inline approval
  UX"). Closed enum at the spec level
  (``desk_only | web_owner_sidebar | web_worker_chat |
  offapp_whatsapp``) but stored free-form here because the spec's
  enum body widens additively (the ``offapp_whatsapp`` slot is
  reserved for a future transport — see §11 "Inline approval UX").
  Service-layer validation narrows on read.

* ``for_user_id TEXT NULL`` — FK to ``user.id`` with
  ``ON DELETE SET NULL``. The delegating user the approval
  ultimately belongs to (§11 "Model"). For inline-channel rows the
  SSE ``agent.action.pending`` event filters on this column so the
  card lands on the right user's tabs (and **only** their tabs);
  for desk-only rows the column is null and the desk surface
  shows the row to every workspace owner / manager.

* ``resolved_user_mode TEXT NULL`` — snapshot of the delegating
  user's per-user agent approval mode (``bypass | auto | strict``)
  at request time (§11 "Model"). NULL when the request did not come
  from a delegated agent token. Free-form (no CHECK clamp) for the
  same widening-additively reason ``inline_channel`` carries.

The remaining §11 ``agent_action`` model fields
(``approval_id`` human-shown, ``correlation_id``,
``card_summary`` / ``card_risk`` / ``card_fields_json``,
``gate_source``, ``gate_destination``, ``executed_at``,
``requested_by_token_id``) stay inside ``action_json`` for the cd-
9ghv slice — the consumer reads them through the JSON envelope.
Promoting them to columns is a future cleanup once the desk surface
needs an index over them; the spec's column list is the long-term
target, not a v1 hard requirement.

**Reversibility.** ``downgrade()`` drops the FK + the new columns
in reverse order. Data carried in ``result_json`` /
``decision_note_md`` / ``expires_at`` / ``inline_channel`` /
``for_user_id`` / ``resolved_user_mode`` is lost on rollback —
acceptable for a dev-database rollback. An operator running a real
rollback should accept or reject every pending row first; the
cd-cm5 baseline still works for the four core columns.

See ``docs/specs/02-domain-model.md`` §"approval_request",
``docs/specs/11-llm-and-agents.md`` §"Model" / §"TTL".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a9b1c3d5e6"
down_revision: str | Sequence[str] | None = "e6f8a0b2c4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("approval_request", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "result_json",
                sa.JSON(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "decision_note_md",
                sa.String(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "inline_channel",
                sa.String(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "for_user_id",
                sa.String(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "resolved_user_mode",
                sa.String(),
                nullable=True,
            )
        )
        # FK declared in the same batch so SQLite's table-rebuild
        # picks the constraint up cleanly (a top-level
        # ``op.create_foreign_key`` outside ``batch_alter_table``
        # would emit raw ALTER TABLE that SQLite cannot run on a
        # column it just added).
        batch_op.create_foreign_key(
            "fk_approval_request_for_user",
            "user",
            ["for_user_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the FK + the new columns in reverse order. Data carried
    in any of the new columns is lost — acceptable for a dev-
    database rollback (a production rollback should accept or
    reject every pending row first).
    """
    with op.batch_alter_table("approval_request", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_approval_request_for_user",
            type_="foreignkey",
        )
        batch_op.drop_column("resolved_user_mode")
        batch_op.drop_column("for_user_id")
        batch_op.drop_column("inline_channel")
        batch_op.drop_column("decision_note_md")
        batch_op.drop_column("result_json")
        batch_op.drop_column("expires_at")
