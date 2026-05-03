"""User-leave CRUD + approval state machine (cd-oydd).

The :class:`~app.adapters.db.availability.models.UserLeave` row carries
a worker's request to be unavailable across a date range. Per
``docs/specs/06-tasks-and-scheduling.md`` §"user_leave" and §"Availability
precedence stack", **only approved** leaves block assignment — pending
ones do not affect the candidate pool. Self-submitted leaves land
``approved_at = NULL``; an owner or manager approves or rejects.

This module is the single write seam for the row. The HTTP router in
:mod:`app.api.v1.user_leaves` is a thin DTO passthrough.

Public surface:

* **DTOs** — :class:`UserLeaveCreate` / :class:`UserLeaveUpdate` /
  :class:`UserLeaveView`. Update is explicit-sparse; create takes the
  full body.
* **Service functions** — :func:`list_leaves` (cursor-paginated, with
  filters), :func:`get_leave`, :func:`create_leave`,
  :func:`update_leave`, :func:`approve_leave`, :func:`reject_leave`,
  :func:`delete_leave`.
* **Errors** — :class:`UserLeaveNotFound`,
  :class:`UserLeaveInvariantViolated`, :class:`UserLeavePermissionDenied`,
  :class:`UserLeaveTransitionForbidden`.

**Capabilities.** Writes gate through the injected
:class:`~app.domain.identity.availability_ports.CapabilityChecker`
(SA-backed by
:class:`~app.adapters.db.availability.repositories.SqlAlchemyCapabilityChecker`,
which itself wraps :func:`app.authz.require`):

* ``leaves.create_self`` — self-submit (auto-allowed to all_workers).
* ``leaves.edit_others`` — manager retroactive create / edit / delete
  on someone else's row (managers + owners by default).
* ``leaves.view_others`` — listing or reading other users' rows
  (managers + owners by default).

There is no separate ``leaves.approve`` / ``leaves.manage`` key:
approve / reject collapse to ``leaves.edit_others`` because every
manager who can edit on behalf of a worker can also stamp the
approval. Single capability, two paths, one §05 row each — keeps the
catalog from drifting toward one key per verb.

**Auto-approve on self-submit.** When the caller already holds
``leaves.edit_others`` (catalog default: owners + managers),
:func:`create_leave` stamps ``approved_at`` + ``approved_by`` at
insert time so the row lands directly in "approved" — a manager
scheduling their own time off shouldn't have to walk through their
own approval queue. The check routes through the
:class:`CapabilityChecker` seam so the auto-approve trigger and every
other ``leaves.edit_others`` gate share the same authority
(``actor_grant_role`` is "audit-shape hint, not the authority" per
§02). Workers self-submitting always land pending, even if a
manager later approves on their behalf.

**Reject = soft-delete with reason.** §06 doesn't pin a persistent
``rejected`` state on ``user_leave`` (the schema only carries
``approved_at`` / ``approved_by``). Rather than carve a new column
or overload ``approved_by = NULL`` as a marker, :func:`reject_leave`
soft-deletes the row (stamps ``deleted_at``) and folds the rejection
reason into ``note_md`` if provided. The ``user_leave.rejected``
audit row preserves the full state transition for the worker's
complaints inbox; the soft-deleted row is invisible to the live-list
filter, matching the spec's "pending leaves do not affect assignment;
rejected ones are forever invisible" stance.

**Architecture (cd-2upg).** The module talks to a
:class:`~app.domain.identity.availability_ports.UserLeaveRepository`
Protocol + a
:class:`~app.domain.identity.availability_ports.CapabilityChecker`
Protocol — never to the SQLAlchemy model classes or :mod:`app.authz`
directly. Both seams' SA-backed concretions live in
:mod:`app.adapters.db.availability.repositories`; unit tests inject
fakes or wire the SA pair over an in-memory SQLite session. The
leave repo also threads its open :class:`~sqlalchemy.orm.Session`
through ``repo.session`` so the audit writer
(:func:`app.audit.write_audit`) — which still takes a concrete
``Session`` today — can keep using the same UoW. Mirrors the cd-r5j2
pattern shipped for the sibling
:mod:`app.domain.identity.user_availability_overrides` service.

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3). Every mutation writes
one :mod:`app.audit` row in the same transaction.

**Tenancy.** The ORM tenant filter auto-narrows every SELECT on
``user_leave``; the SA repo re-asserts the
``workspace_id = ctx.workspace_id`` predicate explicitly as
defence-in-depth.

See ``docs/specs/06-tasks-and-scheduling.md`` §"user_leave",
§"Availability precedence stack";
``docs/specs/05-employees-and-roles.md`` §"Action catalog" rows
``leaves.create_self`` / ``leaves.edit_others`` / ``leaves.view_others``;
``docs/specs/12-rest-api.md`` §"Users / work roles / settings".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.audit import write_audit
from app.domain.identity.availability_ports import (
    CapabilityChecker,
    SeamPermissionDenied,
    UserLeaveRepository,
    UserLeaveRow,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "UserLeaveCategory",
    "UserLeaveCreate",
    "UserLeaveInvariantViolated",
    "UserLeaveListFilter",
    "UserLeaveNotFound",
    "UserLeavePermissionDenied",
    "UserLeaveTransitionForbidden",
    "UserLeaveUpdate",
    "UserLeaveView",
    "approve_leave",
    "create_leave",
    "delete_leave",
    "get_leave",
    "list_leaves",
    "reject_leave",
    "update_leave",
]


# ---------------------------------------------------------------------------
# Enums (string literals — keep parity with the DB CHECK constraint)
# ---------------------------------------------------------------------------


# Mirrors the ``user_leave.category`` CHECK in
# :mod:`app.adapters.db.availability.models`. Kept as a Literal so
# mypy sees the closed set; the
# :class:`~tests.unit.adapters.db.test_user_leave.TestCategoryLiteralPinned`
# adapter test pins the literal to the DB tuple so a schema widening
# trips the assert before a request can land an out-of-set value
# (the previous import-time assertion was lifted out of this module
# at cd-2upg because the Protocol seam forbids the
# ``app.domain → app.adapters`` import that fed it).
UserLeaveCategory = Literal["vacation", "sick", "personal", "bereavement", "other"]


_MAX_NOTE_LEN = 20_000
_MAX_ID_LEN = 64


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UserLeaveNotFound(LookupError):
    """The target ``user_leave`` row is invisible to the caller.

    404-equivalent. Fired when the id is unknown, soft-deleted, or
    lives in a different workspace — all three collapse to the same
    surface per §01 "tenant surface is not enumerable".
    """


class UserLeaveInvariantViolated(ValueError):
    """Write would violate a §06 "user_leave" invariant.

    422-equivalent. Thrown when the date window is malformed or the
    payload references columns the schema rejects.
    """


class UserLeavePermissionDenied(PermissionError):
    """Caller lacks the capability for the attempted action.

    403-equivalent. Wraps the underlying
    :class:`~app.domain.identity.availability_ports.SeamPermissionDenied`
    so the router maps a single domain exception to the
    ``user_leave``-specific 403 envelope.
    """


class UserLeaveTransitionForbidden(ValueError):
    """The leave is not in a state the requested transition accepts.

    409-equivalent. Fires on:

    * editing a non-pending leave (``approved_at`` set or row
      tombstoned);
    * approving an already-approved leave;
    * rejecting an already-rejected (soft-deleted) leave.

    Idempotency at the HTTP layer is the router's call — the service
    surfaces every "wrong state" as this single exception so the
    router can decide whether to short-circuit to 200 or surface 409.
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class UserLeaveCreate(BaseModel):
    """Request body for :func:`create_leave`.

    ``user_id`` defaults to the caller (``ctx.actor_id``) when ``None``.
    Workers self-requesting leave omit the field; managers creating a
    retroactive entry on someone else's behalf send it explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    starts_on: date
    ends_on: date
    category: UserLeaveCategory
    note_md: str | None = Field(default=None, max_length=_MAX_NOTE_LEN)

    @model_validator(mode="after")
    def _validate_dates(self) -> UserLeaveCreate:
        """Reject ``ends_on < starts_on``.

        Same-day leaves (``starts_on == ends_on``) are valid — the DB
        CHECK enforces ``ends_on >= starts_on``. The DTO mirrors that
        rule so a 422 lands at the boundary instead of a generic
        IntegrityError at flush time.
        """
        if self.ends_on < self.starts_on:
            raise ValueError("ends_on must be on or after starts_on")
        return self


class UserLeaveUpdate(BaseModel):
    """Partial-update body for :func:`update_leave`.

    Explicit-sparse — only sent fields land. ``user_id`` is
    deliberately frozen because re-keying a row to a different
    ``user_id`` would orphan its audit chain. Callers wanting to
    transfer an approved leave between users should soft-delete and
    re-create.
    """

    model_config = ConfigDict(extra="forbid")

    starts_on: date | None = None
    ends_on: date | None = None
    category: UserLeaveCategory | None = None
    note_md: str | None = Field(default=None, max_length=_MAX_NOTE_LEN)


class UserLeaveListFilter(BaseModel):
    """Cursor-page filter for :func:`list_leaves`.

    Fields default to ``None`` so an unfiltered listing is the empty
    filter. The router translates the §12 ``?approved=true|false``
    query param into ``status`` on the way through; that
    self-documenting alias survives in the wire shape because callers
    routinely think in "approved / pending" rather than the
    DB-internal nullability of ``approved_at``.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    status: Literal["approved", "pending"] | None = None
    starts_after: date | None = None
    ends_before: date | None = None


@dataclass(frozen=True, slots=True)
class UserLeaveView:
    """Immutable read projection of a ``user_leave`` row.

    Returned by every service read + write. ``approved_at`` is the
    only state column the caller needs to reason about — when set,
    the leave blocks assignment; when null, it's pending.
    ``deleted_at`` is included so the wire response from
    :func:`reject_leave` and :func:`delete_leave` carries the
    tombstone timestamp the UI needs to render the post-action
    state; the live-list path filters tombstones at the repo layer
    so they never leak into a default ``GET /user_leaves`` page.
    """

    id: str
    workspace_id: str
    user_id: str
    starts_on: date
    ends_on: date
    category: UserLeaveCategory
    approved_at: datetime | None
    approved_by: str | None
    note_md: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _narrow_category(value: str) -> UserLeaveCategory:
    """Narrow a loaded DB string to :data:`UserLeaveCategory`.

    The DB CHECK already rejects out-of-set values; this helper exists
    purely to satisfy mypy's Literal narrowing without a ``cast``.
    Schema drift surfaces as a loud :class:`ValueError`.
    """
    if value == "vacation":
        return "vacation"
    if value == "sick":
        return "sick"
    if value == "personal":
        return "personal"
    if value == "bereavement":
        return "bereavement"
    if value == "other":
        return "other"
    raise ValueError(f"unknown user_leave.category {value!r} on loaded row")


def _row_to_view(row: UserLeaveRow) -> UserLeaveView:
    """Project a seam-level :class:`UserLeaveRow` into the public view.

    The repo already returned an immutable, frozen value object; we
    re-pack it into the public :class:`UserLeaveView` shape so callers
    keep the dataclass they were already typing against. The seam-
    level row carries ``category`` as a plain :class:`str`; this is
    where the open-typed value is narrowed to the
    :data:`UserLeaveCategory` Literal closed set.
    """
    return UserLeaveView(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        starts_on=row.starts_on,
        ends_on=row.ends_on,
        category=_narrow_category(row.category),
        approved_at=row.approved_at,
        approved_by=row.approved_by,
        note_md=row.note_md,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _view_to_diff_dict(view: UserLeaveView) -> dict[str, Any]:
    """Flatten a view into a JSON-safe audit payload.

    Stringifies dates / datetimes so the audit ``diff`` column (JSON1
    on SQLite, JSONB on Postgres) accepts the payload without a
    custom encoder.
    """
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "user_id": view.user_id,
        "starts_on": view.starts_on.isoformat(),
        "ends_on": view.ends_on.isoformat(),
        "category": view.category,
        "approved_at": (
            view.approved_at.isoformat() if view.approved_at is not None else None
        ),
        "approved_by": view.approved_by,
        "note_md": view.note_md,
    }


def _load_row(
    repo: UserLeaveRepository,
    ctx: WorkspaceContext,
    *,
    leave_id: str,
    include_deleted: bool = False,
) -> UserLeaveRow:
    """Return the row or raise :class:`UserLeaveNotFound`."""
    row = repo.get(
        workspace_id=ctx.workspace_id,
        leave_id=leave_id,
        include_deleted=include_deleted,
    )
    if row is None:
        raise UserLeaveNotFound(leave_id)
    return row


# ---------------------------------------------------------------------------
# Authz helpers
# ---------------------------------------------------------------------------


def _gate_or_self(
    checker: CapabilityChecker,
    ctx: WorkspaceContext,
    *,
    target_user_id: str,
    cross_user_action: str,
) -> None:
    """Pass for the self-target case; require ``cross_user_action`` otherwise.

    Centralises the "requester-or-manager" rule shared by every
    cross-user write in this service. Raising
    :class:`UserLeavePermissionDenied` (not the bare seam
    :class:`SeamPermissionDenied`) lets the router's error map stay
    narrow — one domain exception type per 403 shape.
    """
    if target_user_id == ctx.actor_id:
        return
    try:
        checker.require(cross_user_action)
    except SeamPermissionDenied as exc:
        raise UserLeavePermissionDenied(str(exc)) from exc


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_leaves(
    repo: UserLeaveRepository,
    checker: CapabilityChecker,
    ctx: WorkspaceContext,
    *,
    filters: UserLeaveListFilter | None = None,
    limit: int,
    after_id: str | None = None,
) -> Sequence[UserLeaveView]:
    """Cursor-paginated listing of live ``user_leave`` rows.

    Returns up to ``limit + 1`` rows so the router's
    :func:`~app.api.pagination.paginate` helper can compute
    ``has_more`` without a second query. Rows ordered by ``id ASC``
    (ULID → time-ordered) so the forward cursor is deterministic.

    Authorisation:

    * Listing without a ``user_id`` filter is the manager inbox view
      and requires ``leaves.view_others``.
    * Listing with ``user_id == ctx.actor_id`` is always allowed.
    * Listing with a different ``user_id`` requires
      ``leaves.view_others``.

    Filters:

    * ``status='approved'`` → ``approved_at IS NOT NULL``.
    * ``status='pending'`` → ``approved_at IS NULL``.
    * ``starts_after`` / ``ends_before`` narrow the date window —
      ``starts_after`` filters rows with ``starts_on >= starts_after``,
      ``ends_before`` filters rows with ``ends_on <= ends_before``.
      Combine both for a strict containment query, or use one for an
      open-ended slice.
    """
    resolved = filters if filters is not None else UserLeaveListFilter()

    target_user_id = resolved.user_id
    if target_user_id is None:
        # Manager inbox — no per-user filter means cross-user surface.
        try:
            checker.require("leaves.view_others")
        except SeamPermissionDenied as exc:
            raise UserLeavePermissionDenied(str(exc)) from exc
    else:
        _gate_or_self(
            checker,
            ctx,
            target_user_id=target_user_id,
            cross_user_action="leaves.view_others",
        )

    rows = repo.list(
        workspace_id=ctx.workspace_id,
        limit=limit,
        after_id=after_id,
        user_id=target_user_id,
        status=resolved.status,
        starts_after=resolved.starts_after,
        ends_before=resolved.ends_before,
    )
    return [_row_to_view(r) for r in rows]


def get_leave(
    repo: UserLeaveRepository,
    checker: CapabilityChecker,
    ctx: WorkspaceContext,
    *,
    leave_id: str,
) -> UserLeaveView:
    """Return a single :class:`UserLeaveView` or raise on miss.

    Authorisation: requester or ``leaves.view_others``. A cross-tenant
    probe collapses to :class:`UserLeaveNotFound` (404, not 403) per
    §01 "tenant surface is not enumerable" — the tenant filter has
    already narrowed the SELECT so a foreign-workspace row never
    surfaces here.
    """
    row = _load_row(repo, ctx, leave_id=leave_id)
    _gate_or_self(
        checker,
        ctx,
        target_user_id=row.user_id,
        cross_user_action="leaves.view_others",
    )
    return _row_to_view(row)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_leave(
    repo: UserLeaveRepository,
    checker: CapabilityChecker,
    ctx: WorkspaceContext,
    *,
    body: UserLeaveCreate,
    force_pending: bool = False,
    clock: Clock | None = None,
) -> UserLeaveView:
    """Insert a new ``user_leave`` row.

    When ``body.user_id`` is ``None`` the caller is requesting leave
    for themselves — gated on ``leaves.create_self``. When it differs
    from ``ctx.actor_id`` the caller is creating on behalf of someone
    else (manager retroactive entry), gated on ``leaves.edit_others``.

    **Auto-approve.** When the caller is an owner or holds a
    ``manager`` grant, the row lands with ``approved_at = now`` and
    ``approved_by = ctx.actor_id``. A worker's self-submission lands
    pending; a manager creating on someone else's behalf also lands
    auto-approved (a manager retroactive entry is implicitly an
    approval — anything else would force the manager to approve their
    own decision, which the worker would never see otherwise).

    **``force_pending``.** Set by the ``POST /me/leaves`` self-service
    shortcut (cd-6uij): per ``docs/specs/12-rest-api.md``
    §"Self-service shortcuts" that surface always lands the row
    pending, even when the caller would otherwise auto-approve. A
    manager wanting to retroactively log+approve their own leave
    uses the generic ``POST /user_leaves`` endpoint (which keeps
    the auto-approve default).
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    target_user_id = body.user_id if body.user_id is not None else ctx.actor_id

    try:
        if target_user_id != ctx.actor_id:
            checker.require("leaves.edit_others")
        else:
            checker.require("leaves.create_self")
    except SeamPermissionDenied as exc:
        raise UserLeavePermissionDenied(str(exc)) from exc

    # Defence-in-depth: the DTO already enforces this, but a Python
    # caller bypassing the DTO (``model_construct``) would otherwise
    # land an invalid window at the DB CHECK with an opaque
    # IntegrityError.
    if body.ends_on < body.starts_on:
        raise UserLeaveInvariantViolated(
            f"ends_on {body.ends_on.isoformat()!r} must be on or after "
            f"starts_on {body.starts_on.isoformat()!r}"
        )

    auto_approve = (not force_pending) and checker.has("leaves.edit_others")
    approved_at: datetime | None = now if auto_approve else None
    approved_by: str | None = ctx.actor_id if auto_approve else None

    row_id = new_ulid(clock=clock)
    row = repo.insert(
        leave_id=row_id,
        workspace_id=ctx.workspace_id,
        user_id=target_user_id,
        starts_on=body.starts_on,
        ends_on=body.ends_on,
        category=body.category,
        note_md=body.note_md,
        approved_at=approved_at,
        approved_by=approved_by,
        now=now,
    )

    view = _row_to_view(row)
    write_audit(
        repo.session,
        ctx,
        entity_kind="user_leave",
        entity_id=row.id,
        action="user_leave.created",
        diff={"after": _view_to_diff_dict(view), "auto_approved": auto_approve},
        clock=resolved_clock,
    )
    return view


def update_leave(
    repo: UserLeaveRepository,
    checker: CapabilityChecker,
    ctx: WorkspaceContext,
    *,
    leave_id: str,
    body: UserLeaveUpdate,
    clock: Clock | None = None,
) -> UserLeaveView:
    """Partial update of a still-pending leave.

    State-machine guard: only pending (``approved_at IS NULL``) rows
    are editable. An approved leave whose dates need to shift must be
    rejected (or deleted) and re-submitted, so the assignment audit
    trail stays coherent — silently mutating an approved leave would
    flip the candidate pool retroactively.

    Authorisation: requester or ``leaves.edit_others``. A manager
    editing someone else's pending leave takes the cross-user path;
    a worker editing their own pending leave takes the self-path.

    A zero-delta call (every sent field matches the current value)
    skips the audit write — matches the convention from
    :mod:`app.domain.identity.user_work_roles`.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(repo, ctx, leave_id=leave_id)

    _gate_or_self(
        checker,
        ctx,
        target_user_id=row.user_id,
        cross_user_action="leaves.edit_others",
    )

    if row.approved_at is not None:
        raise UserLeaveTransitionForbidden(
            f"leave {leave_id!r} is already approved; only pending leaves "
            "may have their fields edited"
        )

    sent = body.model_fields_set
    if not sent:
        return _row_to_view(row)

    # Compute the post-update window before invoking the repo so the
    # DTO's ``starts_on`` / ``ends_on`` validator (which only sees
    # both edges when both are sent) extends to the row state when
    # only one edge is sent. A field that's in ``model_fields_set``
    # but evaluates to ``None`` is treated as "not actually mutated"
    # — the DTO declares ``date | None`` purely so Pydantic can
    # honour the explicit-sparse contract; a JSON ``null`` value on
    # an edge would be a client bug, but treating it as a no-op
    # keeps the validator deterministic without raising on shape.
    new_starts = (
        body.starts_on
        if "starts_on" in sent and body.starts_on is not None
        else row.starts_on
    )
    new_ends = (
        body.ends_on if "ends_on" in sent and body.ends_on is not None else row.ends_on
    )
    if new_ends < new_starts:
        raise UserLeaveInvariantViolated(
            f"ends_on {new_ends.isoformat()!r} must be on or after "
            f"starts_on {new_starts.isoformat()!r}"
        )

    # Detect deltas before invoking the repo — a zero-delta PATCH
    # skips both the SA write and the audit row. ``starts_on`` /
    # ``ends_on`` / ``category`` are non-nullable; a sent ``None``
    # is treated as "unchanged". For the nullable ``note_md`` we
    # distinguish "send JSON null to clear" from "field omitted";
    # the repo's ``clear_note_md`` flag carries it through to the SQL.
    starts_changed = (
        "starts_on" in sent
        and body.starts_on is not None
        and body.starts_on != row.starts_on
    )
    ends_changed = (
        "ends_on" in sent and body.ends_on is not None and body.ends_on != row.ends_on
    )
    category_changed = (
        "category" in sent
        and body.category is not None
        and body.category != row.category
    )
    note_clear = "note_md" in sent and body.note_md is None
    note_set = (
        "note_md" in sent and body.note_md is not None and body.note_md != row.note_md
    )

    # ``clear`` flag only meaningful when the row currently holds a
    # value — otherwise a "clear None over None" is not a delta.
    note_clear_effective = note_clear and row.note_md is not None

    changed = (
        starts_changed
        or ends_changed
        or category_changed
        or note_clear_effective
        or note_set
    )

    before = _row_to_view(row)
    if not changed:
        return before

    after_row = repo.update_fields(
        workspace_id=ctx.workspace_id,
        leave_id=leave_id,
        starts_on=body.starts_on if starts_changed else None,
        ends_on=body.ends_on if ends_changed else None,
        category=body.category if category_changed else None,
        note_md=body.note_md if note_set else None,
        clear_note_md=note_clear_effective,
        now=now,
    )
    after = _row_to_view(after_row)

    write_audit(
        repo.session,
        ctx,
        entity_kind="user_leave",
        entity_id=after.id,
        action="user_leave.updated",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


def approve_leave(
    repo: UserLeaveRepository,
    checker: CapabilityChecker,
    ctx: WorkspaceContext,
    *,
    leave_id: str,
    clock: Clock | None = None,
) -> UserLeaveView:
    """Stamp ``approved_at`` + ``approved_by`` on a pending leave.

    Always requires ``leaves.edit_others`` — a worker cannot approve
    their own request through this surface (auto-approve at create
    time is the supported "manager schedules their own leave" path).

    State-machine guards:

    * pending → approved: the happy path.
    * already-approved: :class:`UserLeaveTransitionForbidden` (409).
      Idempotent re-approval would be a footgun — the audit trail
      would lose the second-approver signature.
    * soft-deleted (rejected): :class:`UserLeaveNotFound` because
      :func:`_load_row` filters tombstones; the rejected row cannot
      be un-rejected through this path.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(repo, ctx, leave_id=leave_id)

    try:
        checker.require("leaves.edit_others")
    except SeamPermissionDenied as exc:
        raise UserLeavePermissionDenied(str(exc)) from exc

    if row.approved_at is not None:
        raise UserLeaveTransitionForbidden(f"leave {leave_id!r} is already approved")

    before = _row_to_view(row)
    after_row = repo.stamp_approved(
        workspace_id=ctx.workspace_id,
        leave_id=leave_id,
        approved_by=ctx.actor_id,
        now=now,
    )
    after = _row_to_view(after_row)

    write_audit(
        repo.session,
        ctx,
        entity_kind="user_leave",
        entity_id=after.id,
        action="user_leave.approved",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


def reject_leave(
    repo: UserLeaveRepository,
    checker: CapabilityChecker,
    ctx: WorkspaceContext,
    *,
    leave_id: str,
    reason_md: str | None = None,
    clock: Clock | None = None,
) -> UserLeaveView:
    """Reject a pending leave by soft-deleting the row.

    §06 "user_leave" doesn't carve a ``rejected`` column on the row;
    instead, the pragmatic v1 shape soft-deletes the row (stamps
    ``deleted_at``) and folds the rejection ``reason_md`` into the
    row's ``note_md`` so the worker's complaints inbox keeps the
    explanation. The ``user_leave.rejected`` audit row preserves the
    full state transition.

    Always requires ``leaves.edit_others``.

    State-machine guards:

    * pending → rejected: the happy path.
    * already-approved → rejected is **not** allowed via this path;
      the manager must :func:`delete_leave` the approved row, which
      writes a different audit action so the candidate-pool change
      is greppable. Surfaces :class:`UserLeaveTransitionForbidden`.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(repo, ctx, leave_id=leave_id)

    try:
        checker.require("leaves.edit_others")
    except SeamPermissionDenied as exc:
        raise UserLeavePermissionDenied(str(exc)) from exc

    if row.approved_at is not None:
        raise UserLeaveTransitionForbidden(
            f"leave {leave_id!r} is already approved; cannot reject — "
            "delete the row instead"
        )

    before = _row_to_view(row)

    folded_note: str | None = None
    if reason_md is not None and reason_md.strip():
        # Concatenate rather than overwrite so the worker's original
        # request stays visible alongside the rejection rationale.
        # An empty / whitespace-only reason is treated as no reason.
        prefix = f"{row.note_md}\n\n" if row.note_md else ""
        folded_note = f"{prefix}Rejected: {reason_md}"

    after_row = repo.soft_delete(
        workspace_id=ctx.workspace_id,
        leave_id=leave_id,
        note_md=folded_note,
        now=now,
    )
    after = _row_to_view(after_row)

    write_audit(
        repo.session,
        ctx,
        entity_kind="user_leave",
        entity_id=after.id,
        action="user_leave.rejected",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
            "reason_md": reason_md,
        },
        clock=resolved_clock,
    )
    return after


def delete_leave(
    repo: UserLeaveRepository,
    checker: CapabilityChecker,
    ctx: WorkspaceContext,
    *,
    leave_id: str,
    clock: Clock | None = None,
) -> UserLeaveView:
    """Soft-delete a leave row (the worker's "withdraw request" path).

    Authorisation: requester or ``leaves.edit_others``. Despite the
    name, this is the canonical "withdraw / cancel" path — workers
    use it to take back a pending request, managers use it to revoke
    an approved row that should no longer block assignment.

    Idempotent at the row level: a repeated call surfaces
    :class:`UserLeaveNotFound` because :func:`_load_row` filters
    tombstones. The HTTP DELETE returns 204 either way (the router
    swallows the second 404 → 204 mapping if the spec wants
    idempotent semantics; today it surfaces 404 to flag a
    double-click).
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(repo, ctx, leave_id=leave_id)

    _gate_or_self(
        checker,
        ctx,
        target_user_id=row.user_id,
        cross_user_action="leaves.edit_others",
    )

    before = _row_to_view(row)
    after_row = repo.soft_delete(
        workspace_id=ctx.workspace_id,
        leave_id=leave_id,
        # No note_md mutation on the canonical withdraw path — the
        # worker's original explanation survives intact.
        note_md=None,
        now=now,
    )
    after = _row_to_view(after_row)

    write_audit(
        repo.session,
        ctx,
        entity_kind="user_leave",
        entity_id=after.id,
        action="user_leave.deleted",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


# ---------------------------------------------------------------------------
# Guardrail against drift
# ---------------------------------------------------------------------------


# Pin against schema drift lives in
# :mod:`tests.unit.adapters.db.test_user_leave` post-cd-2upg — the
# domain layer can no longer import the adapter's private tuple now
# that the Protocol seam forbids the ``app.domain → app.adapters``
# edge. The test imports both sides (which is allowed in the test
# layer) and asserts the literal mirrors the DB CHECK set, fulfilling
# the same "trip on a schema widening" guarantee the previous
# module-import assert provided.
