"""Leave-request CRUD + state-machine guards (cd-31c).

The :class:`~app.adapters.db.time.models.Leave` row tracks a worker's
request for paid / unpaid time off. The v1 state machine is:

    pending -> approved | rejected | cancelled

with the explicit guard that ``cancelled`` is reachable from
``pending`` (always) and from ``approved`` (only while the
``starts_at`` instant is still in the future). ``approved`` and
``rejected`` transitions live in the approval service (cd-8pi, out
of scope here) — this module ships the CRUD and the cancel-own path.

Public surface:

* **DTOs** — :class:`LeaveCreate` (POST body), :class:`LeaveUpdateDates`
  (PATCH body), plus the read projection :class:`LeaveView`. Shape-
  level validation (``starts_at < ends_at``) lives on the DTO so the
  same rule fires for HTTP + Python callers.
* **Service functions** — :func:`create`, :func:`cancel_own`,
  :func:`update_dates`, :func:`list_for_user`, :func:`list_for_workspace`,
  :func:`get`. Every function takes a
  :class:`~app.tenancy.WorkspaceContext` as its first positional
  argument; the ``workspace_id`` is resolved from the context, never
  from the caller's payload (v1 invariant §01).
* **Errors** — :class:`LeaveNotFound`, :class:`LeaveBoundaryInvalid`,
  :class:`LeaveTransitionForbidden`, :class:`LeavePermissionDenied`.
  Each subclasses the stdlib parent the router's error map points at
  (``LookupError`` -> 404, ``ValueError`` -> 409 / 422,
  ``PermissionError`` -> 403).

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3). Every mutation writes
one :mod:`app.audit` row in the same transaction with a redacted
diff payload (``kind`` / ``reason_md`` pass through the audit
writer's redaction seam so PII can't survive into on-disk logs).

**Capabilities.** Writes gate through :func:`app.authz.require`:

* creating a leave for the caller -> ``leaves.create_self``
  (auto-allowed for ``all_workers`` + ``managers`` + ``owners``);
* creating a leave on someone else's behalf (retroactive manager
  entry), cancelling someone else's leave, editing dates on someone
  else's pending leave -> ``leaves.edit_others`` (managers + owners
  by default);
* listing / reading another user's leaves, or listing the entire
  workspace's leave queue -> ``leaves.view_others`` (managers +
  owners by default).

**Timezone snap deferred.** The Beads description references a
``p0.util`` tz-snap helper that would align ``starts_at`` /
``ends_at`` to whole days in the worker's local timezone. That
utility does not exist in this tree (it would live under
:mod:`app.util.time_zone` alongside the geofence / rota helpers if
and when we land it). The v1 shape accepts any UTC-aware datetime
and persists it verbatim; the UI is free to choose the granularity.
A follow-up Beads task will file this once the tz-snap helper lands;
until then the explicit ``UTC-aware datetime`` type on every field
is the boundary contract.

**Manager decisions.** The cd-8pi slice adds advisory conflict
detection plus the immutable approve / reject transition. Conflicts
do not block the decision; downstream assignment and notification
workers react to the emitted ``leave.decided`` event.

See ``docs/specs/05-employees-and-roles.md`` §"Worker self-service",
``docs/specs/09-time-payroll-expenses.md`` §"Leave",
``docs/specs/02-domain-model.md`` §"leave",
``docs/specs/12-rest-api.md`` §"/leaves".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.time.models import (
    _LEAVE_KIND_VALUES,
    _LEAVE_STATUS_VALUES,
    Leave,
    Shift,
)
from app.audit import write_audit
from app.authz import (
    InvalidScope,
    PermissionDenied,
    UnknownActionKey,
    require,
)
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import LeaveDecided
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "LeaveBoundaryInvalid",
    "LeaveConflictsView",
    "LeaveCreate",
    "LeaveDecision",
    "LeaveDecisionRequest",
    "LeaveKind",
    "LeaveKindInvalid",
    "LeaveNotFound",
    "LeavePermissionDenied",
    "LeaveStatus",
    "LeaveTransitionForbidden",
    "LeaveUpdateDates",
    "LeaveView",
    "cancel_own",
    "create_leave",
    "decide_leave",
    "get_conflicts",
    "get_leave",
    "list_for_user",
    "list_for_workspace",
    "update_dates",
]


# ---------------------------------------------------------------------------
# Enums (string literals — keep parity with the DB CHECK constraints)
# ---------------------------------------------------------------------------


LeaveKind = Literal["vacation", "sick", "comp", "other"]
LeaveStatus = Literal["pending", "approved", "rejected", "cancelled"]
LeaveDecision = Literal["approved", "rejected"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LeaveNotFound(LookupError):
    """The requested leave does not exist in the caller's workspace.

    404-equivalent. Raised by :func:`get`, :func:`cancel_own`, and
    :func:`update_dates` when the id is unknown or not visible to
    the caller's workspace context.
    """


class LeaveBoundaryInvalid(ValueError):
    """``ends_at`` is not strictly after ``starts_at``.

    422-equivalent. The DB CHECK on :class:`Leave` enforces
    ``ends_at > starts_at`` at flush time; this domain error lets
    the router surface a clean 422 instead of an opaque integrity
    error when the DTO or a service-level mutation would violate
    the invariant.
    """


class LeaveKindInvalid(ValueError):
    """``kind`` is outside the DB-approved enum set.

    422-equivalent. The DTO's :data:`LeaveKind` ``Literal`` already
    enforces the set on the HTTP boundary; this error exists as a
    defence-in-depth path for Python callers that bypass the DTO
    (``LeaveCreate.model_construct`` / a subclass that loosens
    validators). Kept separate from :class:`LeaveBoundaryInvalid`
    so the router can surface a distinct ``invalid_kind`` code the
    SPA can pattern-match on.
    """


class LeaveTransitionForbidden(ValueError):
    """Requested state transition is not allowed from the current status.

    409-equivalent. Fires when the caller tries to cancel an already-
    cancelled leave, amend dates on an approved leave, cancel an
    approved leave whose start is already in the past, or anything
    else the state machine rejects.
    """


class LeavePermissionDenied(PermissionError):
    """The caller lacks capability for the attempted leave action.

    403-equivalent. The service uses :func:`app.authz.require` with
    ``leaves.create_self`` / ``leaves.edit_others`` /
    ``leaves.view_others`` to enforce the rule; this exception wraps
    the underlying :class:`~app.authz.PermissionDenied` so the
    router can map it to a ``leave``-specific 403 error body.
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


# Caps kept modest to bound audit + DB payload without being
# restrictive in practice. Matches the shape of sibling shift DTOs.
_MAX_REASON_LEN = 20_000
_MAX_ID_LEN = 40


class LeaveCreate(BaseModel):
    """Request body for ``POST /me/leaves`` (and the workspace-level POST).

    ``user_id`` defaults to the caller's ``ctx.actor_id`` when the
    service receives ``None`` — workers self-requesting leave do
    not pass the field. Managers creating a retroactive leave for
    someone else pass an explicit ``user_id``; the service gates
    that path through ``leaves.edit_others``.

    ``starts_at < ends_at`` is enforced on the DTO boundary — the
    same rule fires for HTTP and Python callers.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    kind: LeaveKind
    starts_at: datetime
    ends_at: datetime
    reason_md: str | None = Field(default=None, max_length=_MAX_REASON_LEN)

    @model_validator(mode="after")
    def _reject_nonpositive_window(self) -> LeaveCreate:
        """Enforce ``starts_at < ends_at`` at the DTO layer.

        The DB CHECK constraint enforces the same invariant at flush
        time, but raising here lets the router surface a 422 with a
        tidy validation payload instead of a generic integrity error.
        """
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be strictly greater than starts_at")
        return self


class LeaveUpdateDates(BaseModel):
    """Request body for ``PATCH /me/leaves/{leave_id}``.

    Only the date window is editable via this path. All other
    mutable fields (``status``, ``reason_md``, ``kind``) are
    intentionally excluded — the v1 slice keeps the update surface
    minimal so the state machine's ``pending -> …`` transitions
    don't get tangled with shape mutations.

    Both fields are required: a PATCH that moves the window must
    rewrite both edges atomically. Callers that only want to shift
    one edge pass the other edge unchanged. Allowing a one-sided
    PATCH would require the service to read-then-diff against the
    stored row, and the current row could race with a concurrent
    cancellation; demanding both edges lets the validator fire
    deterministically without a round-trip.
    """

    model_config = ConfigDict(extra="forbid")

    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def _reject_nonpositive_window(self) -> LeaveUpdateDates:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be strictly greater than starts_at")
        return self


@dataclass(frozen=True, slots=True)
class LeaveView:
    """Immutable read projection of a ``leave`` row.

    Returned by every service read + write. A frozen / slotted
    dataclass (not a Pydantic model) because reads carry audit-
    sensitive fields (``decided_by``, ``decided_at``) that are
    managed by the service, not the caller's payload — the same
    reasoning as :class:`~app.domain.time.shifts.ShiftView`.
    """

    id: str
    workspace_id: str
    user_id: str
    kind: LeaveKind
    starts_at: datetime
    ends_at: datetime
    status: LeaveStatus
    reason_md: str | None
    decided_by: str | None
    decided_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LeaveConflictsView:
    """Advisory manager-inbox conflicts for a pending leave request."""

    leave_id: str
    shift_ids: tuple[str, ...]
    occurrence_ids: tuple[str, ...]


class LeaveDecisionRequest(BaseModel):
    """Service-level body for manager approve / reject decisions."""

    model_config = ConfigDict(extra="forbid")

    decision: LeaveDecision
    rationale_md: str | None = Field(default=None, max_length=_MAX_REASON_LEN)


# ---------------------------------------------------------------------------
# Row <-> view projection
# ---------------------------------------------------------------------------


def _ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as a UTC-aware datetime.

    SQLite's ``DateTime(timezone=True)`` column type strips tzinfo on
    read (the dialect has no native TZ support — the timezone flag is
    informational only). Rows round-tripping through a SQLite test
    engine come back naive even though the service wrote a UTC-aware
    value. Postgres preserves the offset faithfully, so this guard is
    a no-op there.

    Mirrors :func:`app.domain.time.shifts._ensure_utc`; a shared
    helper under :mod:`app.util.clock` would be tidier but the
    duplication is two tiny functions — we will extract on the
    third caller.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _narrow_kind(value: str) -> LeaveKind:
    """Narrow a loaded DB string to the :data:`LeaveKind` literal.

    The DB CHECK constraint already rejects anything else; this
    helper exists purely to satisfy mypy's strict-Literal reading
    without a ``cast``. An unexpected value indicates schema drift —
    raise rather than silently downgrade.
    """
    if value == "vacation":
        return "vacation"
    if value == "sick":
        return "sick"
    if value == "comp":
        return "comp"
    if value == "other":
        return "other"
    raise ValueError(f"unknown leave.kind {value!r} on loaded row")


def _narrow_status(value: str) -> LeaveStatus:
    """Narrow a loaded DB string to the :data:`LeaveStatus` literal.

    Sibling helper to :func:`_narrow_kind` — the DB CHECK already
    enforces the set; this is a mypy-narrowing guard, not a runtime
    validator.
    """
    if value == "pending":
        return "pending"
    if value == "approved":
        return "approved"
    if value == "rejected":
        return "rejected"
    if value == "cancelled":
        return "cancelled"
    raise ValueError(f"unknown leave.status {value!r} on loaded row")


def _row_to_view(row: Leave) -> LeaveView:
    """Project a loaded :class:`Leave` row into a read view."""
    return LeaveView(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        kind=_narrow_kind(row.kind),
        starts_at=_ensure_utc(row.starts_at),
        ends_at=_ensure_utc(row.ends_at),
        status=_narrow_status(row.status),
        reason_md=row.reason_md,
        decided_by=row.decided_by,
        decided_at=(
            _ensure_utc(row.decided_at) if row.decided_at is not None else None
        ),
        created_at=_ensure_utc(row.created_at),
    )


def _view_to_diff_dict(view: LeaveView) -> dict[str, Any]:
    """Flatten a :class:`LeaveView` into a JSON-safe dict for audit.

    Stringifies the datetime columns so the audit row's ``diff`` JSON
    payload stays portable (SQLite JSON1 + PG JSONB both accept plain
    strings but reject native :class:`datetime` objects). Mirrors
    :func:`app.domain.time.shifts._view_to_diff_dict`.
    """
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "user_id": view.user_id,
        "kind": view.kind,
        "starts_at": view.starts_at.isoformat(),
        "ends_at": view.ends_at.isoformat(),
        "status": view.status,
        "reason_md": view.reason_md,
        "decided_by": view.decided_by,
        "decided_at": (
            view.decided_at.isoformat() if view.decided_at is not None else None
        ),
        "created_at": view.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Authz helpers
# ---------------------------------------------------------------------------


def _require_capability(
    session: Session,
    ctx: WorkspaceContext,
    *,
    action_key: str,
) -> None:
    """Enforce ``action_key`` on the caller's workspace or raise.

    Wraps :func:`app.authz.require` + translates a caller-bug
    (unknown key / invalid scope) into a :class:`RuntimeError` so the
    router layer can surface it as a 500, separate from the 403 that
    a genuine :class:`~app.authz.PermissionDenied` produces.

    Matches the shape of :func:`app.domain.time.shifts._require_capability`.
    """
    try:
        require(
            session,
            ctx,
            action_key=action_key,
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
        )
    except (UnknownActionKey, InvalidScope) as exc:
        raise RuntimeError(
            f"authz catalog misconfigured for {action_key!r}: {exc!s}"
        ) from exc


def _gate_or_self(
    session: Session,
    ctx: WorkspaceContext,
    *,
    target_user_id: str,
    cross_user_action: str,
) -> None:
    """Require ``cross_user_action`` when targeting someone else; else pass.

    Centralises the "requester-or-manager" rule that every
    :mod:`app.services.leave.service` write shares. Raising
    :class:`LeavePermissionDenied` (not the bare
    :class:`~app.authz.PermissionDenied`) lets the router's error
    map stay narrow — one domain exception type per 403 shape.
    """
    if target_user_id == ctx.actor_id:
        return
    try:
        _require_capability(session, ctx, action_key=cross_user_action)
    except PermissionDenied as exc:
        raise LeavePermissionDenied(str(exc)) from exc


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _load_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    leave_id: str,
    for_update: bool = False,
) -> Leave:
    """Load ``leave_id`` scoped to the caller's workspace.

    The ORM tenant filter already constrains SELECTs to
    ``ctx.workspace_id``; the explicit predicate below is
    defence-in-depth (matches the convention on
    :mod:`app.domain.time.shifts._load_row`).
    """
    stmt = select(Leave).where(
        Leave.id == leave_id,
        Leave.workspace_id == ctx.workspace_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise LeaveNotFound(leave_id)
    return row


def get_leave(
    session: Session,
    ctx: WorkspaceContext,
    *,
    leave_id: str,
) -> LeaveView:
    """Return the leave identified by ``leave_id`` or raise.

    The caller must be the requester or hold ``leaves.view_others``.
    A cross-tenant probe collapses to :class:`LeaveNotFound` (404,
    not 403) per §01 "tenant surface is not enumerable".
    """
    row = _load_row(session, ctx, leave_id=leave_id)
    _gate_or_self(
        session,
        ctx,
        target_user_id=row.user_id,
        cross_user_action="leaves.view_others",
    )
    return _row_to_view(row)


def list_for_user(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str | None = None,
    status: LeaveStatus | None = None,
    limit: int | None = None,
    after_id: str | None = None,
) -> Sequence[LeaveView]:
    """Return every leave for ``user_id`` (default: the caller).

    Listing OTHER users' leaves requires ``leaves.view_others``;
    listing your own is always allowed.

    Results are ordered by ``starts_at`` ascending with ``id`` as a
    tiebreaker inside the same millisecond so the manager timeline
    + self-service "My leaves" page render deterministically.

    Pagination: when ``limit`` is set, returns up to ``limit + 1`` rows
    so the HTTP layer can compute ``has_more`` without a second query.
    ``after_id`` is the id of the last row from the previous page —
    rows are taken strictly after it in the ``(starts_at, id)`` order.
    An ``after_id`` that is not in scope (unknown / wrong workspace /
    different user) yields the empty list per the §12 cursor-isolation
    contract.
    """
    target_user_id = user_id if user_id is not None else ctx.actor_id
    _gate_or_self(
        session,
        ctx,
        target_user_id=target_user_id,
        cross_user_action="leaves.view_others",
    )

    stmt = select(Leave).where(
        Leave.workspace_id == ctx.workspace_id,
        Leave.user_id == target_user_id,
    )
    if status is not None:
        stmt = stmt.where(Leave.status == status)
    if after_id is not None:
        cursor_row = session.get(Leave, after_id)
        if (
            cursor_row is None
            or cursor_row.workspace_id != ctx.workspace_id
            or cursor_row.user_id != target_user_id
        ):
            return []
        stmt = stmt.where(
            (Leave.starts_at > cursor_row.starts_at)
            | ((Leave.starts_at == cursor_row.starts_at) & (Leave.id > cursor_row.id))
        )
    stmt = stmt.order_by(Leave.starts_at.asc(), Leave.id.asc())
    if limit is not None:
        stmt = stmt.limit(limit + 1)
    return [_row_to_view(row) for row in session.scalars(stmt).all()]


def list_for_workspace(
    session: Session,
    ctx: WorkspaceContext,
    *,
    status: LeaveStatus | None = None,
    limit: int | None = None,
    after_id: str | None = None,
) -> Sequence[LeaveView]:
    """Return every leave in the workspace (manager inbox view).

    Always requires ``leaves.view_others`` — this is the cross-user
    queue by design. A worker that calls this for their own leaves
    should use :func:`list_for_user` instead (no capability check
    for the self-case).

    Pagination: when ``limit`` is set, returns up to ``limit + 1`` rows
    so the HTTP layer can compute ``has_more`` without a second query.
    ``after_id`` is the id of the last row from the previous page —
    rows are taken strictly after it in the ``(starts_at, id)`` order.
    An ``after_id`` that is not in scope (unknown / wrong workspace)
    yields the empty list per the §12 cursor-isolation contract.
    """
    try:
        _require_capability(session, ctx, action_key="leaves.view_others")
    except PermissionDenied as exc:
        raise LeavePermissionDenied(str(exc)) from exc

    stmt = select(Leave).where(Leave.workspace_id == ctx.workspace_id)
    if status is not None:
        stmt = stmt.where(Leave.status == status)
    if after_id is not None:
        cursor_row = session.get(Leave, after_id)
        if cursor_row is None or cursor_row.workspace_id != ctx.workspace_id:
            return []
        stmt = stmt.where(
            (Leave.starts_at > cursor_row.starts_at)
            | ((Leave.starts_at == cursor_row.starts_at) & (Leave.id > cursor_row.id))
        )
    stmt = stmt.order_by(Leave.starts_at.asc(), Leave.id.asc())
    if limit is not None:
        stmt = stmt.limit(limit + 1)
    return [_row_to_view(row) for row in session.scalars(stmt).all()]


def get_conflicts(
    session: Session,
    ctx: WorkspaceContext,
    *,
    leave_id: str,
) -> LeaveConflictsView:
    """Return shifts and scheduled task occurrences overlapping a leave.

    This is the manager-inbox advisory surface, so it always requires
    ``leaves.view_others`` even when the target leave happens to belong
    to the caller. Conflicts do not block decisions; they let the
    manager make the call with enough context.
    """
    try:
        _require_capability(session, ctx, action_key="leaves.view_others")
    except PermissionDenied as exc:
        raise LeavePermissionDenied(str(exc)) from exc

    row = _load_row(session, ctx, leave_id=leave_id)
    conflicts = _find_conflicts(session, ctx, leave=row)
    return LeaveConflictsView(
        leave_id=row.id,
        shift_ids=conflicts.shift_ids,
        occurrence_ids=conflicts.occurrence_ids,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_leave(
    session: Session,
    ctx: WorkspaceContext,
    *,
    body: LeaveCreate,
    clock: Clock | None = None,
) -> LeaveView:
    """Create a fresh leave in ``status='pending'`` and return its view.

    When ``body.user_id`` is ``None`` the caller requests leave for
    themselves — the common worker self-service path gated on
    ``leaves.create_self`` (auto-allowed for ``all_workers`` via
    default_allow). When ``body.user_id`` differs from
    ``ctx.actor_id`` the caller is creating a leave on someone
    else's behalf (manager retroactive entry) and must hold
    ``leaves.edit_others``.

    The DTO enforces ``starts_at < ends_at``; a bad window surfaces
    as a 422 at the HTTP layer. The service reasserts the rule via
    :class:`LeaveBoundaryInvalid` for Python callers that bypass
    the DTO (all writes through the HTTP surface go through the
    DTO by construction).
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    target_user_id = body.user_id if body.user_id is not None else ctx.actor_id

    # Re-raise any :class:`PermissionDenied` as the sibling
    # :class:`LeavePermissionDenied` so the router's error map has one
    # domain type per 403 shape — the ``action_key`` changes per branch
    # (``leaves.edit_others`` for cross-user, ``leaves.create_self`` for
    # self) but both collapse to the same HTTP envelope.
    try:
        if target_user_id != ctx.actor_id:
            _require_capability(session, ctx, action_key="leaves.edit_others")
        else:
            _require_capability(session, ctx, action_key="leaves.create_self")
    except PermissionDenied as exc:
        raise LeavePermissionDenied(str(exc)) from exc

    # Guardrail — the DTO's ``Literal`` already enforces this, but a
    # Python caller bypassing the DTO (``model_construct``, a subclass
    # with loosened validators) would otherwise land an out-of-set
    # ``kind`` at the DB CHECK constraint, which raises an opaque
    # integrity error. Raising :class:`LeaveKindInvalid` gives the
    # router a clean 422 envelope distinct from the bad-window path.
    if body.kind not in _LEAVE_KIND_VALUES:
        raise LeaveKindInvalid(
            f"leave.kind={body.kind!r} is not one of {sorted(_LEAVE_KIND_VALUES)!r}"
        )

    # Defence-in-depth: the DTO enforces ``starts_at < ends_at`` at
    # construction time, but a malformed Python caller bypassing the
    # DTO (direct ``LeaveCreate.model_construct``, a subclass with
    # different validators) would otherwise land a zero-or-negative
    # window in the DB CHECK's error surface.
    if body.ends_at <= body.starts_at:
        raise LeaveBoundaryInvalid(
            f"ends_at {body.ends_at.isoformat()!r} is not strictly after "
            f"starts_at {body.starts_at.isoformat()!r}"
        )

    row = Leave(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        user_id=target_user_id,
        kind=body.kind,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        status="pending",
        reason_md=body.reason_md,
        decided_by=None,
        decided_at=None,
        created_at=now,
    )
    session.add(row)
    session.flush()

    view = _row_to_view(row)
    write_audit(
        session,
        ctx,
        entity_kind="leave",
        entity_id=row.id,
        action="leave.created",
        diff={"after": _view_to_diff_dict(view)},
        clock=resolved_clock,
    )
    return view


def update_dates(
    session: Session,
    ctx: WorkspaceContext,
    *,
    leave_id: str,
    body: LeaveUpdateDates,
    clock: Clock | None = None,
) -> LeaveView:
    """Rewrite ``starts_at`` / ``ends_at`` on a pending leave.

    State-machine guard: only ``pending`` leaves are editable. A
    leave that is already ``approved`` / ``rejected`` / ``cancelled``
    rejects with :class:`LeaveTransitionForbidden` — an approved
    leave whose dates need to shift must be cancelled and
    re-requested, so the approval audit trail stays coherent.

    Authorisation: requester or ``leaves.edit_others``. A worker
    editing their own pending leave takes the self-path; a manager
    editing someone else's takes the cross-user path.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    row = _load_row(session, ctx, leave_id=leave_id)

    _gate_or_self(
        session,
        ctx,
        target_user_id=row.user_id,
        cross_user_action="leaves.edit_others",
    )

    if row.status != "pending":
        raise LeaveTransitionForbidden(
            f"leave {leave_id!r} is {row.status!r}; only pending leaves "
            "may have their dates edited"
        )

    # Defence-in-depth; the DTO enforces the invariant.
    if body.ends_at <= body.starts_at:
        raise LeaveBoundaryInvalid(
            f"ends_at {body.ends_at.isoformat()!r} is not strictly after "
            f"starts_at {body.starts_at.isoformat()!r}"
        )

    before = _row_to_view(row)
    row.starts_at = body.starts_at
    row.ends_at = body.ends_at
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="leave",
        entity_id=row.id,
        action="leave.updated",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


def cancel_own(
    session: Session,
    ctx: WorkspaceContext,
    *,
    leave_id: str,
    clock: Clock | None = None,
) -> LeaveView:
    """Cancel a leave the caller owns (or a manager cancels on behalf).

    State-machine guards:

    * ``pending`` -> ``cancelled`` — always allowed.
    * ``approved`` -> ``cancelled`` — allowed only if ``starts_at``
      is strictly after ``clock.now()`` (the leave has not started).
      Cancelling a leave that has already begun would lose labour-
      law-compliance data; the manager must instead edit the
      timesheet.
    * Any other source state rejects with
      :class:`LeaveTransitionForbidden`.

    Authorisation: requester or ``leaves.edit_others``. Despite the
    function's name, a manager with ``leaves.edit_others`` can
    cancel someone else's leave — the ``_own`` in the name is a
    worker-centric reading of the state transition, not an authz
    assertion.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(session, ctx, leave_id=leave_id)

    _gate_or_self(
        session,
        ctx,
        target_user_id=row.user_id,
        cross_user_action="leaves.edit_others",
    )

    current_status = _narrow_status(row.status)
    if current_status == "pending":
        pass  # Always cancellable.
    elif current_status == "approved":
        # SQLite strips tzinfo on read; the domain invariant is UTC
        # at rest, so tag the naive value as UTC before comparing
        # against ``now`` (which comes from an aware clock).
        starts_at_utc = _ensure_utc(row.starts_at)
        if starts_at_utc <= now:
            raise LeaveTransitionForbidden(
                f"approved leave {leave_id!r} has already started "
                f"({starts_at_utc.isoformat()!r}); cannot cancel"
            )
    else:
        raise LeaveTransitionForbidden(
            f"leave {leave_id!r} is {row.status!r}; cannot cancel from this state"
        )

    before = _row_to_view(row)
    row.status = "cancelled"
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="leave",
        entity_id=row.id,
        action="leave.cancelled",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


def decide_leave(
    session: Session,
    ctx: WorkspaceContext,
    *,
    leave_id: str,
    body: LeaveDecisionRequest,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> LeaveView:
    """Approve or reject a pending leave request.

    The transition is immutable. A replay of the same terminal decision
    returns the existing row without writing another audit row or
    publishing another event; an attempt to change one terminal decision
    into the other is rejected.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()

    try:
        _require_capability(session, ctx, action_key="leaves.edit_others")
    except PermissionDenied as exc:
        raise LeavePermissionDenied(str(exc)) from exc

    row = _load_row(session, ctx, leave_id=leave_id, for_update=True)
    current_status = _narrow_status(row.status)
    if current_status == body.decision:
        return _row_to_view(row)
    if current_status != "pending":
        raise LeaveTransitionForbidden(
            f"leave {leave_id!r} is {current_status!r}; terminal leave "
            "decisions are immutable"
        )

    conflicts = _find_conflicts(session, ctx, leave=row)
    before = _row_to_view(row)

    row.status = body.decision
    row.decided_by = ctx.actor_id
    row.decided_at = now
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="leave",
        entity_id=row.id,
        action="leave.decided",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
            "rationale_md": body.rationale_md,
        },
        clock=resolved_clock,
    )
    resolved_bus.publish(
        LeaveDecided(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=now,
            leave_id=row.id,
            decision=body.decision,
            decided_by=ctx.actor_id,
            decided_at=now,
            conflicting_shift_ids=conflicts.shift_ids,
            conflicting_occurrence_ids=conflicts.occurrence_ids,
        )
    )
    return after


@dataclass(frozen=True, slots=True)
class _ConflictIds:
    shift_ids: tuple[str, ...]
    occurrence_ids: tuple[str, ...]


def _find_conflicts(
    session: Session,
    ctx: WorkspaceContext,
    *,
    leave: Leave,
) -> _ConflictIds:
    """Return assigned work intervals intersecting ``leave``'s window."""
    shift_rows = session.scalars(
        select(Shift)
        .where(
            Shift.workspace_id == ctx.workspace_id,
            Shift.user_id == leave.user_id,
            Shift.starts_at < leave.ends_at,
            or_(Shift.ends_at.is_(None), Shift.ends_at > leave.starts_at),
        )
        .order_by(Shift.starts_at.asc(), Shift.id.asc())
    ).all()
    occurrence_rows = session.scalars(
        select(Occurrence)
        .where(
            Occurrence.workspace_id == ctx.workspace_id,
            Occurrence.assignee_user_id == leave.user_id,
            Occurrence.state.in_(("scheduled", "pending", "in_progress", "overdue")),
            Occurrence.starts_at < leave.ends_at,
            Occurrence.ends_at > leave.starts_at,
        )
        .order_by(Occurrence.starts_at.asc(), Occurrence.id.asc())
    ).all()
    return _ConflictIds(
        shift_ids=tuple(row.id for row in shift_rows),
        occurrence_ids=tuple(row.id for row in occurrence_rows),
    )


# ---------------------------------------------------------------------------
# Guardrails against drift
# ---------------------------------------------------------------------------


# Pin the assumptions this module makes about the DB enum sets.
# If a future migration widens the kind / status vocabulary, either
# the narrow helpers above break (unknown value on a loaded row) or
# these asserts catch the drift at import time — whichever fires
# first makes the drift explicit.
assert set(_LEAVE_KIND_VALUES) == {"vacation", "sick", "comp", "other"}, (
    "LeaveKind literal diverged from DB CHECK set"
)
assert set(_LEAVE_STATUS_VALUES) == {
    "pending",
    "approved",
    "rejected",
    "cancelled",
}, "LeaveStatus literal diverged from DB CHECK set"
