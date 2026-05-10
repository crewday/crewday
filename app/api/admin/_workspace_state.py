"""Workspace verification_state / archived_at accessors.

The cd-jlms admin surface surfaces and mutates two
:class:`~app.adapters.db.workspace.models.Workspace` lifecycle fields:

* ``verification_state`` — one of
  ``unverified|email_verified|human_verified|trusted`` (§02
  "workspaces", §20 glossary). Drives the §15 "Self-serve abuse
  mitigations" gates and the
  ``POST /admin/api/v1/workspaces/{id}/trust`` mutation.
* ``archived_at`` — soft-delete timestamp for
  ``POST /admin/api/v1/workspaces/{id}/archive``.

cd-s8kk promoted the former ``settings_json`` interim values into
typed columns. The legacy key constants remain exported so the
migration can document and backfill the old storage shape, but runtime
reads and writes go through the columns only.

See ``docs/specs/02-domain-model.md`` §"workspaces" and
``docs/specs/15-security-privacy.md`` §"Verification states".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.adapters.db.workspace.models import Workspace
from app.tenancy import tenant_agnostic

__all__ = [
    "ARCHIVED_AT_KEY",
    "DEFAULT_VERIFICATION_STATE",
    "VERIFICATION_STATES",
    "VERIFICATION_STATE_KEY",
    "VerificationState",
    "archive_workspace_if_needed",
    "archived_at_of",
    "delete_requested_at_of",
    "format_archived_at",
    "purge_after_of",
    "schedule_workspace_deletion_if_needed",
    "set_archived_at",
    "set_verification_state",
    "verification_state_of",
]


# Allowed values for ``verification_state``. Mirrors §20 glossary
# entry ``verification_state`` — kept here as a tuple so the
# ``trust`` mutation can validate transitions and the admin
# workspace-list payload can advertise the enum to consumers.
VerificationState = str
VERIFICATION_STATES: Final[tuple[str, ...]] = (
    "unverified",
    "email_verified",
    "human_verified",
    "trusted",
)
DEFAULT_VERIFICATION_STATE: Final[str] = "unverified"


# Keys inside :attr:`Workspace.settings_json` that hold the interim
# values. Prefixed with ``admin_`` so they cannot collide with the
# §02 "Settings cascade" key namespace (which uses dotted keys
# like ``recovery.kill_switch_enabled``).
VERIFICATION_STATE_KEY: Final[str] = "admin_verification_state"
ARCHIVED_AT_KEY: Final[str] = "admin_archived_at"
WORKSPACE_DELETE_GRACE: Final[timedelta] = timedelta(days=14)


def verification_state_of(workspace: Workspace) -> str:
    """Return the workspace's current verification state.

    The column is NOT NULL after cd-s8kk; the fallback is defensive
    only for in-memory tests that construct a partial object.
    """
    return workspace.verification_state or DEFAULT_VERIFICATION_STATE


def set_verification_state(workspace: Workspace, *, value: str) -> None:
    """Stamp the verification state into the typed column.

    Caller is expected to have validated ``value`` against
    :data:`VERIFICATION_STATES`; this helper does not re-validate
    so the failure surface stays at the route boundary (where the
    response envelope is shaped).
    """
    workspace.verification_state = value


def archived_at_of(workspace: Workspace) -> datetime | None:
    """Return the workspace's archive timestamp, or ``None`` when live.

    SQLite may round-trip tz-aware columns as naive datetimes; treat
    those as UTC so the admin wire format stays stable.
    """
    parsed = workspace.archived_at
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def delete_requested_at_of(workspace: Workspace) -> datetime | None:
    """Return the deletion request timestamp, normalised to aware UTC."""
    parsed = workspace.delete_requested_at
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def purge_after_of(workspace: Workspace) -> datetime | None:
    """Return the scheduled purge deadline, normalised to aware UTC."""
    parsed = workspace.purge_after
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def set_archived_at(workspace: Workspace, *, when: datetime) -> None:
    """Stamp the archive timestamp into the typed column.

    ``when`` must already carry tzinfo (the route resolves it from
    the system clock at write time). The helper normalises to UTC before
    assignment so SQLite + Postgres round-trips behave consistently.
    """
    if when.tzinfo is None:  # pragma: no cover - defensive
        when = when.replace(tzinfo=UTC)
    workspace.archived_at = when.astimezone(UTC)


def archive_workspace_if_needed(
    session: Session, workspace: Workspace, *, when: datetime
) -> tuple[datetime, bool]:
    """Ensure ``workspace.archived_at`` is stamped.

    Returns ``(archived_at, changed)``. Already archived workspaces keep
    their original timestamp so every archive surface stays idempotent.
    The write is conditional at the SQL layer so competing archive calls
    cannot both produce audit rows.
    """
    existing = archived_at_of(workspace)
    if existing is not None:
        return existing, False
    if when.tzinfo is None:  # pragma: no cover - defensive
        when = when.replace(tzinfo=UTC)
    archived = when.astimezone(UTC)
    with tenant_agnostic():
        result = session.execute(
            update(Workspace)
            .where(Workspace.id == workspace.id)
            .where(Workspace.archived_at.is_(None))
            .values(archived_at=archived)
            .execution_options(synchronize_session="fetch")
        )
        if not isinstance(result, CursorResult):  # pragma: no cover - SQLAlchemy DML
            raise TypeError(f"expected CursorResult, got {type(result).__name__}")
        if result.rowcount == 1:
            workspace.archived_at = archived
            return archived, True
        stored = session.scalar(
            select(Workspace.archived_at).where(Workspace.id == workspace.id).limit(1)
        )
    if stored is None:  # pragma: no cover - defensive
        return archived, False
    archived = stored
    if archived.tzinfo is None:
        archived = archived.replace(tzinfo=UTC)
    return archived, False


def schedule_workspace_deletion_if_needed(
    session: Session, workspace: Workspace, *, when: datetime
) -> tuple[datetime, datetime, bool]:
    """Ensure owner-requested deletion is scheduled.

    Returns ``(delete_requested_at, purge_after, changed)``. An existing
    schedule always wins so repeated Delete requests before the deadline
    are idempotent and cannot extend the grace period.
    """
    # code-health: ignore[ccn,nloc] Deletion scheduling is one idempotent repair txn.
    if when.tzinfo is None:  # pragma: no cover - defensive
        when = when.replace(tzinfo=UTC)
    requested_at = when.astimezone(UTC)
    purge_after = requested_at + WORKSPACE_DELETE_GRACE

    existing_requested = delete_requested_at_of(workspace)
    existing_purge_after = purge_after_of(workspace)
    if existing_requested is not None and existing_purge_after is not None:
        return existing_requested, existing_purge_after, False

    if existing_requested is None and existing_purge_after is None:
        with tenant_agnostic():
            result = session.execute(
                update(Workspace)
                .where(Workspace.id == workspace.id)
                .where(Workspace.delete_requested_at.is_(None))
                .where(Workspace.purge_after.is_(None))
                .values(delete_requested_at=requested_at, purge_after=purge_after)
                .execution_options(synchronize_session="fetch")
            )
            if not isinstance(
                result, CursorResult
            ):  # pragma: no cover - SQLAlchemy DML
                raise TypeError(f"expected CursorResult, got {type(result).__name__}")
            if result.rowcount == 1:
                workspace.delete_requested_at = requested_at
                workspace.purge_after = purge_after
                return requested_at, purge_after, True
            stored = session.get(Workspace, workspace.id, populate_existing=True)
        if stored is None:  # pragma: no cover - defensive
            return requested_at, purge_after, False
        existing_requested = delete_requested_at_of(stored)
        existing_purge_after = purge_after_of(stored)
        if existing_requested is not None and existing_purge_after is not None:
            return existing_requested, existing_purge_after, False

    repair_requested = existing_requested or (
        existing_purge_after - WORKSPACE_DELETE_GRACE
        if existing_purge_after is not None
        else requested_at
    )
    repair_purge_after = existing_purge_after or (
        repair_requested + WORKSPACE_DELETE_GRACE
    )
    repair_values: dict[str, datetime] = {}
    if existing_requested is None:
        repair_values["delete_requested_at"] = repair_requested
    if existing_purge_after is None:
        repair_values["purge_after"] = repair_purge_after
    with tenant_agnostic():
        result = session.execute(
            update(Workspace)
            .where(Workspace.id == workspace.id)
            .where(
                *(
                    getattr(Workspace, column_name).is_(None)
                    for column_name in repair_values
                )
            )
            .values(**repair_values)
            .execution_options(synchronize_session="fetch")
        )
        if not isinstance(result, CursorResult):  # pragma: no cover - SQLAlchemy DML
            raise TypeError(f"expected CursorResult, got {type(result).__name__}")
        if result.rowcount == 1:
            if existing_requested is None:
                workspace.delete_requested_at = repair_requested
            if existing_purge_after is None:
                workspace.purge_after = repair_purge_after
            return repair_requested, repair_purge_after, True
        stored = session.get(Workspace, workspace.id, populate_existing=True)
    if stored is None:  # pragma: no cover - defensive
        return repair_requested, repair_purge_after, False
    return (
        delete_requested_at_of(stored) or repair_requested,
        purge_after_of(stored) or repair_purge_after,
        False,
    )


def format_archived_at(workspace: Workspace) -> str | None:
    """Return the archive timestamp as a wire-shaped ISO-8601 string."""
    moment = archived_at_of(workspace)
    if moment is None:
        return None
    return moment.astimezone(UTC).isoformat()


def load_workspace(session: Session, *, workspace_id: str) -> Workspace | None:
    """Tenant-agnostic ``session.get`` for :class:`Workspace`.

    The workspace row itself is not workspace-scoped (it IS the
    tenant), but the ORM tenant filter still injects a predicate
    on tables registered as scoped — :class:`Workspace` is not in
    that registry so ``session.get`` works directly. We wrap the
    read in :func:`tenant_agnostic` for symmetry with the other
    admin-tree reads (every helper here runs on the bare host;
    there is no workspace context to pin) and to insulate against
    a future change that registers ``workspace`` as scoped.
    """
    with tenant_agnostic():
        return session.get(Workspace, workspace_id)
