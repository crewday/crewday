"""llm_cd_cm5

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-24 14:00:00.000000

Creates the five workspace-scoped LLM / agent tables the §11 agent
layer needs at the workspace edge:

* ``model_assignment`` — capability → model binding. Unique
  ``(workspace_id, capability)``: a workspace cannot bind two
  different models to the same capability at the same time.
  ``model_id`` is a **soft reference** (plain ``String(26)``, no
  FK) because the deployment-scope ``llm_model`` registry has not
  yet landed. FK: ``workspace_id`` CASCADE.

* ``agent_token`` — delegated agent tokens. ``hash`` stores the
  **sha256** digest of the plaintext token; ``prefix`` carries the
  first 6-8 chars of the plaintext for ``/me/tokens`` listing
  disambiguation. ``scope_json`` uses ``sa.JSON()`` with
  ``server_default='{}'``. ``expires_at`` is NOT NULL (delegated
  tokens always carry a TTL per §03); ``revoked_at`` is NULL while
  the token is live. ``(workspace_id, prefix)`` index for listing /
  revocation lookup. FK: ``workspace_id`` CASCADE,
  ``delegating_user_id`` SET NULL (history survives a user
  hard-delete).

* ``approval_request`` — HITL agent-action approval queue.
  ``action_json`` is a JSON blob (pydantic-validated at the service
  layer). ``status`` CHECK clamps ``pending | approved | rejected
  | timed_out``. ``(workspace_id, status, created_at)`` index
  powers pending-queue pagination. FK: ``workspace_id`` CASCADE;
  ``requester_actor_id`` / ``decided_by`` SET NULL.

* ``llm_usage`` — per-call usage ledger. ``status`` CHECK clamps
  ``ok | error | refused | timeout`` (spec silent on a closed enum
  — the four values partition observable outcomes per the adapter's
  docstring; widening is additive). ``cost_cents`` / ``tokens_in``
  / ``tokens_out`` / ``latency_ms`` are plain integers for
  portability (SQLite has no ``numeric(10,6)``). Two indexes:
  ``(workspace_id, created_at)`` for the feed,
  ``(workspace_id, capability, created_at)`` for per-capability
  breakdowns. FK: ``workspace_id`` CASCADE. ``model_id`` is a soft
  reference (no FK), matching ``model_assignment``.

* ``budget_ledger`` — rolling-period spend ledger. Unique
  ``(workspace_id, period_start, period_end)``: one ledger row per
  period. ``period_end > period_start`` CHECK guards against
  inverted windows. ``cap_cents`` / ``spent_cents`` are plain
  integers (5.0000 USD ↔ 500 cents; exact across SQLite + PG). FK:
  ``workspace_id`` CASCADE.

All five tables are workspace-scoped (registered via
``app/adapters/db/llm/__init__.py``). Portable across SQLite +
Postgres — CHECK bodies only for the enum-like columns, no
server-side enum types. JSON columns use ``sa.JSON`` with
``server_default='{}'`` to match the ``notification.payload_json``
/ ``chat_message.attachments_json`` / ``email_delivery.context_
snapshot_json`` convention from cd-pjm + cd-aqwt.

**Reversibility.** ``downgrade()`` drops ``budget_ledger`` →
``llm_usage`` → ``approval_request`` → ``agent_token`` →
``model_assignment`` (reverse of the upgrade create order). No
inter-table FK across this set, so the drop order only mirrors the
create order for readability; the
``test_schema_fingerprint_matches_on_sqlite_and_pg`` gate keeps
the upgrade → downgrade → upgrade cycle honest.

See ``docs/specs/02-domain-model.md`` §"LLM",
``docs/specs/11-llm-and-agents.md`` §"Workspace usage budget",
§"Agent action approval".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ``model_assignment`` — capability → model binding.
    op.create_table(
        "model_assignment",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("capability", sa.String(), nullable=False),
        # Soft reference — no FK; ``llm_model`` table lands later.
        sa.Column("model_id", sa.String(length=26), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_model_assignment_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_assignment")),
    )
    with op.batch_alter_table("model_assignment", schema=None) as batch_op:
        # Unique: one assignment per ``(workspace_id, capability)``.
        batch_op.create_index(
            "uq_model_assignment_workspace_capability",
            ["workspace_id", "capability"],
            unique=True,
        )

    # ``agent_token`` — delegated agent tokens.
    op.create_table(
        "agent_token",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("delegating_user_id", sa.String(), nullable=True),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("prefix", sa.String(), nullable=False),
        # ``hash`` is the sha256 hex of the plaintext token (64 chars).
        # Unique mirrors the sibling ``api_token.hash`` pattern (§03
        # "Principles") — the auth layer looks up tokens by hash and
        # a collision would be undisambiguatable.
        sa.Column("hash", sa.String(), nullable=False),
        sa.Column(
            "scope_json",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["delegating_user_id"],
            ["user.id"],
            name=op.f("fk_agent_token_delegating_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_agent_token_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_token")),
        sa.UniqueConstraint("hash", name=op.f("uq_agent_token_hash")),
    )
    with op.batch_alter_table("agent_token", schema=None) as batch_op:
        # Listing + revocation lookup hot path. See the model docstring
        # for why this is a non-partial composite — SQLite / PG parity
        # of partial indexes via Alembic is not portable without
        # per-dialect ``_where`` kwargs.
        batch_op.create_index(
            "ix_agent_token_workspace_prefix",
            ["workspace_id", "prefix"],
            unique=False,
        )

    # ``approval_request`` — HITL approval queue.
    op.create_table(
        "approval_request",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("requester_actor_id", sa.String(), nullable=True),
        sa.Column(
            "action_json",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rationale_md", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'timed_out')",
            name=op.f("ck_approval_request_status"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"],
            ["user.id"],
            name=op.f("fk_approval_request_decided_by_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requester_actor_id"],
            ["user.id"],
            name=op.f("fk_approval_request_requester_actor_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_approval_request_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_request")),
    )
    with op.batch_alter_table("approval_request", schema=None) as batch_op:
        # Pending-queue pagination hot path.
        batch_op.create_index(
            "ix_approval_request_workspace_status_created",
            ["workspace_id", "status", "created_at"],
            unique=False,
        )

    # ``llm_usage`` — per-call usage ledger.
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("capability", sa.String(), nullable=False),
        # Soft reference — no FK; matches ``model_assignment``.
        sa.Column("model_id", sa.String(length=26), nullable=False),
        sa.Column(
            "tokens_in",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "tokens_out",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cost_cents",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "latency_ms",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("correlation_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ok', 'error', 'refused', 'timeout')",
            name=op.f("ck_llm_usage_status"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_llm_usage_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_usage")),
    )
    with op.batch_alter_table("llm_usage", schema=None) as batch_op:
        # Feed hot path.
        batch_op.create_index(
            "ix_llm_usage_workspace_created",
            ["workspace_id", "created_at"],
            unique=False,
        )
        # Per-capability breakdown.
        batch_op.create_index(
            "ix_llm_usage_workspace_capability_created",
            ["workspace_id", "capability", "created_at"],
            unique=False,
        )

    # ``budget_ledger`` — rolling-period spend ledger.
    op.create_table(
        "budget_ledger",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "spent_cents",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cap_cents",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "period_end > period_start",
            name=op.f("ck_budget_ledger_period_end_after_start"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_budget_ledger_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_ledger")),
    )
    with op.batch_alter_table("budget_ledger", schema=None) as batch_op:
        # Unique ledger row per ``(workspace_id, period_start,
        # period_end)``.
        batch_op.create_index(
            "uq_budget_ledger_workspace_period",
            ["workspace_id", "period_start", "period_end"],
            unique=True,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("budget_ledger", schema=None) as batch_op:
        batch_op.drop_index("uq_budget_ledger_workspace_period")
    op.drop_table("budget_ledger")

    with op.batch_alter_table("llm_usage", schema=None) as batch_op:
        batch_op.drop_index("ix_llm_usage_workspace_capability_created")
        batch_op.drop_index("ix_llm_usage_workspace_created")
    op.drop_table("llm_usage")

    with op.batch_alter_table("approval_request", schema=None) as batch_op:
        batch_op.drop_index("ix_approval_request_workspace_status_created")
    op.drop_table("approval_request")

    with op.batch_alter_table("agent_token", schema=None) as batch_op:
        batch_op.drop_index("ix_agent_token_workspace_prefix")
    op.drop_table("agent_token")

    with op.batch_alter_table("model_assignment", schema=None) as batch_op:
        batch_op.drop_index("uq_model_assignment_workspace_capability")
    op.drop_table("model_assignment")
