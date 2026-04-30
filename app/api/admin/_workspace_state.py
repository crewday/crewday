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

from datetime import UTC, datetime
from typing import Final

from sqlalchemy.orm import Session

from app.adapters.db.workspace.models import Workspace
from app.tenancy import tenant_agnostic

__all__ = [
    "ARCHIVED_AT_KEY",
    "DEFAULT_VERIFICATION_STATE",
    "VERIFICATION_STATES",
    "VERIFICATION_STATE_KEY",
    "VerificationState",
    "archived_at_of",
    "format_archived_at",
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


def set_archived_at(workspace: Workspace, *, when: datetime) -> None:
    """Stamp the archive timestamp into the typed column.

    ``when`` must already carry tzinfo (the route resolves it from
    the system clock at write time). The helper normalises to UTC before
    assignment so SQLite + Postgres round-trips behave consistently.
    """
    if when.tzinfo is None:  # pragma: no cover - defensive
        when = when.replace(tzinfo=UTC)
    workspace.archived_at = when.astimezone(UTC)


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
