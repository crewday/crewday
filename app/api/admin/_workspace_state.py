"""Interim verification_state / archived_at storage for ``Workspace``.

The cd-jlms admin surface needs to surface and mutate two fields
that the spec calls for as typed columns on
:class:`~app.adapters.db.workspace.models.Workspace` but which
the v1 ORM slice has not yet materialised:

* ``verification_state`` — one of
  ``unverified|email_verified|human_verified|trusted`` (§02
  "workspaces", §20 glossary). Drives the §15 "Self-serve abuse
  mitigations" gates and the
  ``POST /admin/api/v1/workspaces/{id}/trust`` mutation.
* ``archived_at`` — soft-delete timestamp for
  ``POST /admin/api/v1/workspaces/{id}/archive``.

The full column promotion is tracked as **cd-s8kk** (filed by
the cd-jlms implementer). Until that lands, the admin tree
stores both values inside the existing
:attr:`Workspace.settings_json` column under deterministic keys.
This keeps the spec-shaped admin surface usable without forcing
a schema migration into cd-jlms's atomic landing — the cd-s8kk
migration backfills the typed columns from these keys.

The helpers below are the **only** sanctioned reader / writer
for the interim shape; routing the access through one seam means
cd-s8kk is a single-file swap (replace the dict reads with
column reads) rather than a graph-wide refactor.

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

    Reads :attr:`Workspace.settings_json[VERIFICATION_STATE_KEY]`;
    falls back to :data:`DEFAULT_VERIFICATION_STATE` when the key
    is absent (every workspace seeded before cd-jlms is implicitly
    ``unverified`` per §15). Unknown stored values pass through
    untouched — the consumer (admin list, trust mutation) is
    responsible for rejecting an out-of-band value rather than
    masking it as ``unverified``.
    """
    raw = workspace.settings_json.get(VERIFICATION_STATE_KEY)
    if isinstance(raw, str):
        return raw
    return DEFAULT_VERIFICATION_STATE


def set_verification_state(workspace: Workspace, *, value: str) -> None:
    """Stamp the verification state into ``settings_json``.

    Caller is expected to have validated ``value`` against
    :data:`VERIFICATION_STATES`; this helper does not re-validate
    so the failure surface stays at the route boundary (where the
    response envelope is shaped).

    Reassigns the entire ``settings_json`` dict rather than
    mutating in place — SQLAlchemy's default :class:`JSON` column
    type does not track in-place mutations, so an in-place
    ``[KEY] = value`` would leave the row clean and the change
    would never reach the DB. Building a fresh mapping keeps the
    serialiser honest without the global :func:`MutableDict.as_mutable`
    decoration the wider codebase has not yet adopted.
    """
    # ``settings_json`` defaults to an empty dict at the column
    # level; defensive fallback handles an explicit-NULL row that
    # might leak through the admin tree before cd-s8kk's backfill.
    base = (
        dict(workspace.settings_json)
        if isinstance(workspace.settings_json, dict)
        else {}
    )
    base[VERIFICATION_STATE_KEY] = value
    workspace.settings_json = base


def archived_at_of(workspace: Workspace) -> datetime | None:
    """Return the workspace's archive timestamp, or ``None`` when live.

    Stored as an ISO-8601 string with a UTC offset; parses back
    into a tz-aware :class:`datetime` so callers can format it
    consistently. A malformed stored value is treated as "no
    timestamp known" rather than raising — the admin surface
    must not fail closed on a corrupt settings blob.
    """
    raw = workspace.settings_json.get(ARCHIVED_AT_KEY)
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def set_archived_at(workspace: Workspace, *, when: datetime) -> None:
    """Stamp the archive timestamp into ``settings_json``.

    ``when`` must already carry tzinfo (the route resolves it from
    the system clock at write time). Stored as an ISO-8601 string
    with explicit UTC offset so SQLite + Postgres round-trips
    behave identically.

    Reassigns ``settings_json`` so SQLAlchemy's ``JSON`` column
    type sees the dirty flag — see :func:`set_verification_state`
    for the full rationale.
    """
    if when.tzinfo is None:  # pragma: no cover - defensive
        when = when.replace(tzinfo=UTC)
    base = (
        dict(workspace.settings_json)
        if isinstance(workspace.settings_json, dict)
        else {}
    )
    base[ARCHIVED_AT_KEY] = when.astimezone(UTC).isoformat()
    workspace.settings_json = base


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
