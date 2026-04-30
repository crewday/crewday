"""Deployment-owner membership lookup.

The bare-host admin surface uses deployment-scope ``role_grant`` rows
for access to ``/admin``. Root authority is separate: membership in
``owners@deployment`` lives in ``deployment_owner`` rows.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import DeploymentOwner
from app.tenancy import tenant_agnostic

__all__ = [
    "add_deployment_owner",
    "deployment_owner_count",
    "deployment_owner_user_ids",
    "is_deployment_owner",
    "remove_deployment_owner",
]


def is_deployment_owner(session: Session, *, user_id: str) -> bool:
    """Return ``True`` iff ``user_id`` belongs to ``owners@deployment``."""
    stmt = select(DeploymentOwner.user_id).where(DeploymentOwner.user_id == user_id)
    with tenant_agnostic():
        return session.scalars(stmt).first() is not None


def deployment_owner_user_ids(session: Session) -> frozenset[str]:
    """Return every active deployment-owner user id."""
    with tenant_agnostic():
        return frozenset(session.scalars(select(DeploymentOwner.user_id)).all())


def deployment_owner_count(session: Session) -> int:
    """Return the number of deployment-owner rows."""
    with tenant_agnostic():
        return int(
            session.scalar(select(func.count()).select_from(DeploymentOwner)) or 0
        )


def add_deployment_owner(
    session: Session,
    *,
    user_id: str,
    added_by_user_id: str | None,
    now: datetime | None = None,
) -> tuple[DeploymentOwner, bool]:
    """Upsert a deployment-owner row.

    Returns ``(row, created)`` so callers can keep idempotent re-adds
    free of duplicate audit rows.
    """
    with tenant_agnostic():
        existing = session.get(DeploymentOwner, user_id)
        if existing is not None:
            return existing, False
        row = DeploymentOwner(
            user_id=user_id,
            added_at=now or datetime.now(UTC),
            added_by_user_id=added_by_user_id,
        )
        session.add(row)
        session.flush()
        return row, True


def remove_deployment_owner(session: Session, *, user_id: str) -> bool:
    """Delete a deployment-owner row, returning whether one existed."""
    with tenant_agnostic():
        row = session.get(DeploymentOwner, user_id)
        if row is None:
            return False
        session.delete(row)
        session.flush()
        return True
