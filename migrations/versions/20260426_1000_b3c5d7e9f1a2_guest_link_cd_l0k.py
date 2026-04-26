"""guest_link

Revision ID: b3c5d7e9f1a2
Revises: a2b4c6d8e0f1
Create Date: 2026-04-26 10:00:00.000000

Creates the ``guest_link`` table that backs the public, no-login guest
welcome page (cd-l0k). One row per minted token; the page resolves a
token by signature + TTL + non-revoked check and renders the
welcome bundle for the linked stay.

Schema highlights (mirrors §02 "guest_link" / §04 "Guest welcome
link"):

* ``id`` ULID PK.
* ``workspace_id`` — denormalised tenancy column. The table is
  registered as workspace-scoped through
  :mod:`app.adapters.db.stays.__init__` so the ORM tenant filter
  auto-injects a ``workspace_id`` predicate.
* ``stay_id`` — FK to ``reservation.id`` ``ON DELETE CASCADE`` (the
  v1 slice's stay table is named ``reservation``; cd-1ai promotes
  it to the spec's ``stay`` shape).
* ``token`` — the signed ``itsdangerous`` blob; UNIQUE so a
  duplicate-mint races to a constraint violation rather than a
  silent duplicate row.
* ``expires_at`` — TTL the resolver enforces against the clock.
* ``revoked_at`` — nullable; non-null marks the link revoked.
* ``access_log_json`` — append-only ring buffer of the last 10
  accesses (hashed IP prefix + UA family + timestamp).
* ``created_at`` — mint time.

Also widens ``reservation`` with a ``guest_link_id`` column. The
column is intentionally a **soft ref** (no FK constraint declared)
to avoid the circular FK between ``guest_link.stay_id`` and
``reservation.guest_link_id`` — a hard cycle would force
SQLAlchemy's insert-ordering through ``use_alter=True`` +
``post_update=True``, which (per the
``Instruction.current_version_id`` precedent in
``app/adapters/db/instructions/__init__.py``) is more friction
than correctness gain.

The domain layer is the source of truth for the back-pointer's
hygiene:

* :func:`app.domain.stays.guest_link_service.mint_link` stamps
  the column with the freshly-minted link's id so the manager UI
  can find "the active link for this stay" with one row read.
* :func:`app.domain.stays.guest_link_service.revoke_link` clears
  the column iff it still points at the revoked row (compare-and-
  clear) — that way revoking an older sibling link doesn't strip
  a newer mint's pointer.
* Deleting a stay sweeps every ``guest_link`` row through the
  forward FK's ``ON DELETE CASCADE``; the soft back-pointer
  vanishes with the parent row, so no orphan cleanup is needed.

**Reversibility.** ``downgrade()`` drops the ``reservation``
column and the ``guest_link`` table in reverse order. No data
backfill on downgrade — a guest_link row is reproducible from a
fresh ``mint_link`` call so the rollback is non-destructive in
practice.

See ``docs/specs/02-domain-model.md`` §"guest_link",
``docs/specs/04-properties-and-stays.md`` §"Guest welcome link",
``docs/specs/03-auth-and-tokens.md`` §"Magic link format" for the
shared signed-token shape.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3c5d7e9f1a2"
down_revision: str | Sequence[str] | None = "a2b4c6d8e0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "guest_link",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("stay_id", sa.String(), nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        # Append-only list of ``{ip_prefix_sha256, ua_family, at}``
        # entries; capped at the last 10 by the domain service. JSON
        # rather than a sibling table keeps the read-on-render path a
        # single SELECT (the page is no-cookie, no-JS — render time
        # is the entire critical path).
        sa.Column("access_log_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["stay_id"],
            ["reservation.id"],
            name=op.f("fk_guest_link_stay_id_reservation"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_guest_link_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guest_link")),
        sa.UniqueConstraint("token", name=op.f("uq_guest_link_token")),
    )
    with op.batch_alter_table("guest_link", schema=None) as batch_op:
        # Hot path: "list links for a stay" (manager UI) — leading
        # ``workspace_id`` carries the tenant filter; ``stay_id``
        # narrows to the parent reservation.
        batch_op.create_index(
            "ix_guest_link_workspace_stay",
            ["workspace_id", "stay_id"],
            unique=False,
        )

    # Add the back-pointer on ``reservation`` as a soft ref (no FK
    # constraint). See the module docstring for why a hard FK is
    # avoided.
    with op.batch_alter_table("reservation", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("guest_link_id", sa.String(), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("reservation", schema=None) as batch_op:
        batch_op.drop_column("guest_link_id")

    with op.batch_alter_table("guest_link", schema=None) as batch_op:
        batch_op.drop_index("ix_guest_link_workspace_stay")
    op.drop_table("guest_link")
