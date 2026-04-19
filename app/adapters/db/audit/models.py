"""``audit_log`` — append-only immutable per-workspace mutation log.

See ``docs/specs/02-domain-model.md`` §"audit_log",
``docs/specs/15-security-privacy.md`` §"Audit log", and
``docs/specs/01-architecture.md`` §"Key runtime invariants" #3.
Every domain mutation writes one row here in the same transaction as
the mutation — commit the Unit-of-Work and the audit row lands;
rollback and it's gone.

The spec (§02) calls for a richer schema than what is materialised
here (``before_json`` / ``after_json`` split, hash-chain columns,
``via`` / ``token_id`` provenance, …). This cd-ehf slice ships the
minimum append-only surface consumed by cd-bfc's self-review and
the blocked DB-context tasks (cd-1b2, cd-chd, …); the remaining
fields are added by follow-up migrations owned by
``audit_integrity_check`` / ``audit_verify`` (§15 "Tamper
detection") without widening this table's public write contract.

No foreign keys: the spec calls for "soft refs only, for speed"
(§02 entity preamble). The columns are ULID strings and carry their
own semantics; enforcing FKs would force every audit emission to
resolve a real ``users`` / ``workspace`` row even when the referent
has been archived.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = ["AuditLog"]


class AuditLog(Base):
    """Append-only audit row: one per domain mutation.

    The writer (:mod:`app.audit`) is the only allowed producer; the
    table is never updated or deleted by application code. Retention
    rotation (§02 "Operational-log retention defaults",
    ``rotate_audit_log`` worker) archives rows to JSONL.gz and
    deletes the originals; that is the sole supported delete path.
    """

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    actor_id: Mapped[str] = mapped_column(String, nullable=False)
    actor_kind: Mapped[str] = mapped_column(String, nullable=False)
    actor_grant_role: Mapped[str] = mapped_column(String, nullable=False)
    actor_was_owner_member: Mapped[bool] = mapped_column(Boolean, nullable=False)
    entity_kind: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    # ``diff`` carries arbitrary JSON-serialisable payloads the caller
    # supplies — dict for structured changes, list for bulk events,
    # empty dict when the mutation is shape-free (a ``deleted``). The
    # outer ``Any`` is scoped to SQLAlchemy's ``JSON`` column type;
    # the writer's public signature constrains callers to concrete
    # mapping/sequence/``None`` inputs.
    diff: Mapped[Any] = mapped_column(JSON, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Two composite indexes the spec calls for (§02 "audit_log"): a
    # per-workspace timeline feed (list newest-first) and a
    # per-entity lookup (one entity's full history). The naming
    # convention in ``base.py`` only emits ``ix_<col_label>`` for
    # single-column indexes; these composite shapes need an explicit
    # name so alembic autogenerate sees a stable identifier.
    __table_args__ = (
        Index("ix_audit_log_workspace_created", "workspace_id", "created_at"),
        Index(
            "ix_audit_log_workspace_entity",
            "workspace_id",
            "entity_kind",
            "entity_id",
        ),
    )
