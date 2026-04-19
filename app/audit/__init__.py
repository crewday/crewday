"""Append-only audit log writer.

Every domain mutation calls :func:`write_audit` inside its open
Unit-of-Work. The row lands in the same transaction as the
mutation — commit the UoW and the audit row lands; rollback and
it's gone. The writer never calls ``session.commit()``; the
caller's UoW owns transaction boundaries (§01 "Key runtime
invariants" #3).

See ``docs/specs/01-architecture.md`` §"Key runtime invariants" #3,
``docs/specs/02-domain-model.md`` §"audit_log", and
``docs/specs/15-security-privacy.md`` §"Audit log".
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = ["write_audit"]


def write_audit(
    session: Session,
    ctx: WorkspaceContext,
    *,
    entity_kind: str,
    entity_id: str,
    action: str,
    diff: dict[str, Any] | list[Any] | None = None,
    clock: Clock | None = None,
) -> AuditLog:
    """Append one audit row to the caller's open ``session``.

    The row carries the caller's :class:`~app.tenancy.WorkspaceContext`
    fields verbatim (workspace, actor identity, grant role,
    owner-member flag, correlation id). Persisting happens via
    ``session.add`` only — the function never flushes or commits, so
    the caller's UoW keeps full control of the transaction
    (:class:`~app.adapters.db.session.UnitOfWorkImpl`).

    ``diff`` is JSON-serialisable: a ``dict`` for structured changes,
    a ``list`` for bulk events, ``None`` for shape-free actions
    (``deleted``, ``archived``). ``None`` is persisted as an empty
    dict so downstream readers can rely on the column's non-null
    contract (§02 "audit_log"). The writer performs no pre-flight
    serialisation check — SQLAlchemy's ``JSON`` column raises at
    flush time if the payload is not JSON-compatible, and that
    surface is enough for the current call sites. Callers holding
    ``datetime`` / ``Decimal`` / ``UUID`` values must stringify
    themselves before calling.

    ``clock`` is optional; tests pin ``created_at`` via a
    :class:`~app.util.clock.FrozenClock`.
    """
    now = (clock if clock is not None else SystemClock()).now()
    row = AuditLog(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        actor_id=ctx.actor_id,
        actor_kind=ctx.actor_kind,
        actor_grant_role=ctx.actor_grant_role,
        actor_was_owner_member=ctx.actor_was_owner_member,
        entity_kind=entity_kind,
        entity_id=entity_id,
        action=action,
        # ``{}`` for ``None`` so the NOT NULL contract (§02) holds
        # without forcing every caller to invent a payload.
        diff=diff if diff is not None else {},
        correlation_id=ctx.audit_correlation_id,
        created_at=now,
    )
    session.add(row)
    return row
