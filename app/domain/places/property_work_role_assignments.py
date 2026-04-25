"""Property work-role assignment CRUD service (§05 "Property work role assignment").

The :class:`~app.adapters.db.places.models.PropertyWorkRoleAssignment`
row pins a :class:`~app.adapters.db.workspace.models.UserWorkRole` to a
specific :class:`~app.adapters.db.places.models.Property`. The absence
of any assignment row for a given user_work_role leaves it
**workspace-wide** (a "generalist"); one or more rows narrow eligibility
to those properties only (§05 "Property work role assignment", §02
"property_work_role_assignment").

This module is the only place that inserts, updates, soft-deletes, or
lists rows at the domain layer. The HTTP router in
:mod:`app.api.v1.property_work_role_assignments` is a thin DTO
passthrough.

Public surface:

* **DTOs** — :class:`PropertyWorkRoleAssignmentCreate`,
  :class:`PropertyWorkRoleAssignmentUpdate`,
  :class:`PropertyWorkRoleAssignmentView`. Update is explicit-sparse;
  create takes the full identity body.
* **Service functions** — :func:`list_property_work_role_assignments`
  (cursor-paginated with optional filters),
  :func:`get_property_work_role_assignment`,
  :func:`create_property_work_role_assignment`,
  :func:`update_property_work_role_assignment`,
  :func:`delete_property_work_role_assignment`.
* **Errors** — :class:`PropertyWorkRoleAssignmentNotFound`,
  :class:`PropertyWorkRoleAssignmentInvariantViolated`.

**Domain-enforced invariants** (write-path; not expressed in DDL):

1. ``workspace_id`` must equal the parent ``user_work_role``'s
   ``workspace_id`` — the route never accepts ``workspace_id`` from the
   client; the service derives it from
   :class:`~app.tenancy.WorkspaceContext`. The check rejects
   cross-workspace borrowing of a user_work_role with
   :class:`PropertyWorkRoleAssignmentInvariantViolated`.
2. ``property_id`` must reach the workspace via a live
   ``property_workspace`` row — a workspace cannot pin a role to a
   property it does not operate (§02
   "property_work_role_assignment", invariant 2).
3. ``(user_work_role_id, property_id)`` must be unique among live
   rows — the partial UNIQUE
   ``uq_property_work_role_assignment_role_property_active`` enforces
   this; the service catches the IntegrityError and surfaces it as
   :class:`PropertyWorkRoleAssignmentInvariantViolated` so the HTTP
   layer can return a 409.

**Tenancy.** The ``property_work_role_assignment`` table carries a
denormalised ``workspace_id`` column and is registered as
workspace-scoped, so the ORM tenant filter narrows every SELECT to the
caller's workspace. Each function re-asserts the predicate explicitly
as defence-in-depth.

**Transaction boundary.** The service never calls ``session.commit()``;
the caller's Unit-of-Work owns transaction boundaries. Every mutation
writes one :mod:`app.audit` row in the same transaction.

See ``docs/specs/05-employees-and-roles.md`` §"Property work role
assignment", ``docs/specs/02-domain-model.md``
§"property_work_role_assignment",
``docs/specs/06-tasks-and-scheduling.md`` §"Schedule ruleset
(per-property rota)".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.places.models import PropertyWorkRoleAssignment, PropertyWorkspace
from app.adapters.db.workspace.models import UserWorkRole
from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "PropertyWorkRoleAssignmentCreate",
    "PropertyWorkRoleAssignmentInvariantViolated",
    "PropertyWorkRoleAssignmentNotFound",
    "PropertyWorkRoleAssignmentUpdate",
    "PropertyWorkRoleAssignmentView",
    "create_property_work_role_assignment",
    "delete_property_work_role_assignment",
    "get_property_work_role_assignment",
    "list_property_work_role_assignments",
    "update_property_work_role_assignment",
]


_MAX_ID_LEN = 64


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PropertyWorkRoleAssignmentNotFound(LookupError):
    """The target row is invisible to the caller.

    404-equivalent. Fired when the id is unknown, soft-deleted, or
    lives in a different workspace — all three collapse to the same
    surface per §01 "tenant surface is not enumerable".
    """


class PropertyWorkRoleAssignmentInvariantViolated(ValueError):
    """Write would violate a §05 / §02 invariant.

    422-equivalent. Thrown when:

    * the referenced ``user_work_role`` does not exist in the caller's
      workspace (cross-workspace borrow attempt);
    * the referenced ``property_id`` is not linked to the caller's
      workspace through a live ``property_workspace`` row;
    * a live row already exists for ``(user_work_role_id,
      property_id)`` (partial UNIQUE — collapsed from
      :class:`IntegrityError`). The HTTP router translates this
      duplicate flavour into 409, the rest into 422.
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class PropertyWorkRoleAssignmentCreate(BaseModel):
    """Request body for :func:`create_property_work_role_assignment`.

    Note ``workspace_id`` is **not** in the DTO — it is derived from
    the :class:`~app.tenancy.WorkspaceContext` so a malicious / buggy
    caller cannot pin a role to a workspace they do not operate. The
    cross-workspace check on ``user_work_role_id`` and the
    ``property_workspace`` reachability check on ``property_id`` both
    run inside the service before the row is built.
    """

    model_config = ConfigDict(extra="forbid")

    user_work_role_id: str = Field(..., min_length=1, max_length=_MAX_ID_LEN)
    property_id: str = Field(..., min_length=1, max_length=_MAX_ID_LEN)
    schedule_ruleset_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    property_pay_rule_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)


class PropertyWorkRoleAssignmentUpdate(BaseModel):
    """Partial update body for :func:`update_property_work_role_assignment`.

    Explicit-sparse — only fields in :attr:`model_fields_set` are
    applied. The identity columns (``user_work_role_id``,
    ``property_id``, ``workspace_id``) are deliberately absent: those
    re-key the row and require a delete + re-create flow that the UI
    surfaces separately. v1 only exposes the two pointer fields as
    mutable, matching the "rates / rota tweak" use cases the spec
    describes (§05 "Property work role assignment").
    """

    model_config = ConfigDict(extra="forbid")

    schedule_ruleset_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    property_pay_rule_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)


@dataclass(frozen=True, slots=True)
class PropertyWorkRoleAssignmentView:
    """Immutable read projection of a ``property_work_role_assignment`` row."""

    id: str
    workspace_id: str
    user_work_role_id: str
    property_id: str
    schedule_ruleset_id: str | None
    property_pay_rule_id: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_view(row: PropertyWorkRoleAssignment) -> PropertyWorkRoleAssignmentView:
    """Project a SQLAlchemy row into :class:`PropertyWorkRoleAssignmentView`."""
    return PropertyWorkRoleAssignmentView(
        id=row.id,
        workspace_id=row.workspace_id,
        user_work_role_id=row.user_work_role_id,
        property_id=row.property_id,
        schedule_ruleset_id=row.schedule_ruleset_id,
        property_pay_rule_id=row.property_pay_rule_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _load_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    assignment_id: str,
    include_deleted: bool = False,
) -> PropertyWorkRoleAssignment:
    """Return the row or raise :class:`PropertyWorkRoleAssignmentNotFound`."""
    stmt = select(PropertyWorkRoleAssignment).where(
        PropertyWorkRoleAssignment.id == assignment_id,
        PropertyWorkRoleAssignment.workspace_id == ctx.workspace_id,
    )
    if not include_deleted:
        stmt = stmt.where(PropertyWorkRoleAssignment.deleted_at.is_(None))
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise PropertyWorkRoleAssignmentNotFound(assignment_id)
    return row


def _assert_user_work_role_in_workspace(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_work_role_id: str,
) -> None:
    """Raise if ``user_work_role_id`` does not exist in the caller's workspace.

    Cross-workspace borrowing of a user_work_role is forbidden (§02
    "property_work_role_assignment" invariant 1). The tenant filter on
    ``user_work_role`` already narrows the read; the explicit
    ``workspace_id`` predicate is defence-in-depth and matches the
    convention in :mod:`app.domain.identity.user_work_roles`.
    """
    row = session.scalar(
        select(UserWorkRole).where(
            UserWorkRole.id == user_work_role_id,
            UserWorkRole.workspace_id == ctx.workspace_id,
            UserWorkRole.deleted_at.is_(None),
        )
    )
    if row is None:
        raise PropertyWorkRoleAssignmentInvariantViolated(
            f"user_work_role {user_work_role_id!r} does not exist in this workspace"
        )


def _assert_property_in_workspace(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
) -> None:
    """Raise if ``property_id`` is not linked to the caller's workspace.

    Reachability is "live ``property_workspace`` row joining the
    property to ``ctx.workspace_id``". A workspace cannot pin a role
    to a property it does not operate (§02
    "property_work_role_assignment" invariant 2).
    """
    row = session.scalar(
        select(PropertyWorkspace).where(
            PropertyWorkspace.property_id == property_id,
            PropertyWorkspace.workspace_id == ctx.workspace_id,
        )
    )
    if row is None:
        raise PropertyWorkRoleAssignmentInvariantViolated(
            f"property {property_id!r} is not linked to this workspace"
        )


def _is_role_property_unique_violation(exc: IntegrityError) -> bool:
    """Return ``True`` iff ``exc`` is the partial UNIQUE on (role, property).

    Tightens the integrity-error classification so a stray PK
    collision (vanishingly unlikely with ULIDs but still distinct
    on the wire) is not mis-tagged as a duplicate-active row.
    Postgres surfaces the index name; SQLite the column tuple. We
    accept either signature.
    """
    message = str(exc).lower()
    if "uq_property_work_role_assignment_role_property_active" in message:
        return True
    # SQLite text shape: ``UNIQUE constraint failed:
    # property_work_role_assignment.user_work_role_id,
    # property_work_role_assignment.property_id``.
    return (
        "unique constraint" in message
        and "user_work_role_id" in message
        and "property_id" in message
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_property_work_role_assignments(
    session: Session,
    ctx: WorkspaceContext,
    *,
    limit: int,
    after_id: str | None = None,
    property_id: str | None = None,
    user_work_role_id: str | None = None,
    include_deleted: bool = False,
) -> Sequence[PropertyWorkRoleAssignmentView]:
    """Return up to ``limit + 1`` views for the caller's workspace.

    The service returns ``limit + 1`` so the router's
    :func:`~app.api.pagination.paginate` helper can compute
    ``has_more`` without a second query. Rows ordered by ``id ASC``
    (ULID → time-ordered) so the forward cursor is deterministic.

    ``property_id`` and ``user_work_role_id`` narrow the listing
    independently — the spec §12 "Users / work roles / settings"
    surface accepts both filters on the same call.
    """
    stmt = select(PropertyWorkRoleAssignment).where(
        PropertyWorkRoleAssignment.workspace_id == ctx.workspace_id,
    )
    if not include_deleted:
        stmt = stmt.where(PropertyWorkRoleAssignment.deleted_at.is_(None))
    if property_id is not None:
        stmt = stmt.where(PropertyWorkRoleAssignment.property_id == property_id)
    if user_work_role_id is not None:
        stmt = stmt.where(
            PropertyWorkRoleAssignment.user_work_role_id == user_work_role_id
        )
    if after_id is not None:
        stmt = stmt.where(PropertyWorkRoleAssignment.id > after_id)
    stmt = stmt.order_by(PropertyWorkRoleAssignment.id.asc()).limit(limit + 1)
    rows = session.scalars(stmt).all()
    return [_row_to_view(r) for r in rows]


def get_property_work_role_assignment(
    session: Session,
    ctx: WorkspaceContext,
    *,
    assignment_id: str,
) -> PropertyWorkRoleAssignmentView:
    """Return a single view or raise on miss."""
    return _row_to_view(
        _load_row(session, ctx, assignment_id=assignment_id),
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_property_work_role_assignment(
    session: Session,
    ctx: WorkspaceContext,
    *,
    body: PropertyWorkRoleAssignmentCreate,
    clock: Clock | None = None,
) -> PropertyWorkRoleAssignmentView:
    """Insert a new property_work_role_assignment row.

    Runs both §02 / §05 invariants (user_work_role belongs to
    workspace; property reachable via ``property_workspace``) before
    attempting the flush. An :class:`IntegrityError` on the partial
    UNIQUE ``uq_property_work_role_assignment_role_property_active``
    is collapsed into
    :class:`PropertyWorkRoleAssignmentInvariantViolated` so the HTTP
    layer can map it to 409.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    _assert_user_work_role_in_workspace(
        session, ctx, user_work_role_id=body.user_work_role_id
    )
    _assert_property_in_workspace(session, ctx, property_id=body.property_id)

    # Pre-flight the partial-UNIQUE check so the duplicate-row case
    # surfaces with the canonical "already exists" message *before*
    # any IntegrityError from another constraint (e.g. the FK on
    # ``property_pay_rule_id``) muddies the surface. The flush-time
    # ``IntegrityError`` catch below stays as defence-in-depth against
    # a parallel insert race; it surfaces with the same message so the
    # HTTP layer's substring check keeps mapping to 409.
    existing_live = session.scalar(
        select(PropertyWorkRoleAssignment).where(
            PropertyWorkRoleAssignment.workspace_id == ctx.workspace_id,
            PropertyWorkRoleAssignment.user_work_role_id == body.user_work_role_id,
            PropertyWorkRoleAssignment.property_id == body.property_id,
            PropertyWorkRoleAssignment.deleted_at.is_(None),
        )
    )
    if existing_live is not None:
        raise PropertyWorkRoleAssignmentInvariantViolated(
            f"property_work_role_assignment for "
            f"user_work_role={body.user_work_role_id!r} "
            f"property={body.property_id!r} already exists"
        )

    row_id = new_ulid(clock=clock)
    row = PropertyWorkRoleAssignment(
        id=row_id,
        workspace_id=ctx.workspace_id,
        user_work_role_id=body.user_work_role_id,
        property_id=body.property_id,
        schedule_ruleset_id=body.schedule_ruleset_id,
        property_pay_rule_id=body.property_pay_rule_id,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        # Two flavours of integrity violation can land here:
        #
        # 1. The partial UNIQUE
        #    ``uq_property_work_role_assignment_role_property_active``
        #    fires if a parallel insert beat us between the pre-flight
        #    SELECT above and the flush. The HTTP layer keys on the
        #    ``"already exists"`` substring → 409.
        # 2. The real FK on ``property_pay_rule_id`` (cascading from a
        #    ``pay_rule`` row) fires when the caller supplies a pointer
        #    that does not exist. We surface the FK miss verbatim so
        #    the HTTP layer maps it to a generic 422 invariant
        #    violation.
        #
        # Distinguishing the two across SQLite + Postgres is
        # driver-dependent. Postgres surfaces the constraint name
        # (``uq_property_work_role_assignment_role_property_active``);
        # SQLite surfaces only the column tuple
        # (``user_work_role_id, property_id``). Matching on either
        # signature avoids the false-positive a bare
        # ``"unique constraint"`` substring would have on a PK
        # collision (extremely unlikely with ULIDs, but the matcher
        # should still be tight). The tests assert the wire mapping
        # rather than the SQLite text.
        if _is_role_property_unique_violation(exc):
            raise PropertyWorkRoleAssignmentInvariantViolated(
                f"property_work_role_assignment for "
                f"user_work_role={body.user_work_role_id!r} "
                f"property={body.property_id!r} already exists"
            ) from exc
        raise PropertyWorkRoleAssignmentInvariantViolated(
            f"property_work_role_assignment write rejected by the database "
            f"({exc.orig}); check that property_pay_rule_id and "
            f"schedule_ruleset_id reference live rows"
        ) from exc

    write_audit(
        session,
        ctx,
        entity_kind="property_work_role_assignment",
        entity_id=row_id,
        action="property_work_role_assignment.created",
        diff={
            "user_work_role_id": body.user_work_role_id,
            "property_id": body.property_id,
            "schedule_ruleset_id": body.schedule_ruleset_id,
            "property_pay_rule_id": body.property_pay_rule_id,
        },
        clock=resolved_clock,
    )
    return _row_to_view(row)


def update_property_work_role_assignment(
    session: Session,
    ctx: WorkspaceContext,
    *,
    assignment_id: str,
    body: PropertyWorkRoleAssignmentUpdate,
    clock: Clock | None = None,
) -> PropertyWorkRoleAssignmentView:
    """Partial update of ``schedule_ruleset_id`` + ``property_pay_rule_id``.

    Only fields in :attr:`body.model_fields_set` are touched. A
    zero-delta call (every sent field matches the current value) skips
    the audit write — matches the user_work_roles convention.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    row = _load_row(session, ctx, assignment_id=assignment_id)

    sent = body.model_fields_set
    if not sent:
        return _row_to_view(row)

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    if (
        "schedule_ruleset_id" in sent
        and body.schedule_ruleset_id != row.schedule_ruleset_id
    ):
        before["schedule_ruleset_id"] = row.schedule_ruleset_id
        after["schedule_ruleset_id"] = body.schedule_ruleset_id
        row.schedule_ruleset_id = body.schedule_ruleset_id

    if (
        "property_pay_rule_id" in sent
        and body.property_pay_rule_id != row.property_pay_rule_id
    ):
        before["property_pay_rule_id"] = row.property_pay_rule_id
        after["property_pay_rule_id"] = body.property_pay_rule_id
        row.property_pay_rule_id = body.property_pay_rule_id

    if not after:
        return _row_to_view(row)

    row.updated_at = now
    try:
        session.flush()
    except IntegrityError as exc:
        # The only realistic flush-time failure here is the FK on
        # ``property_pay_rule_id`` — a caller pointing at a pay_rule
        # row that does not exist or was hard-deleted. Surface it as
        # a 422 invariant violation so the HTTP layer doesn't leak a
        # 500. The partial UNIQUE cannot fire on update because the
        # identity columns ``user_work_role_id`` + ``property_id``
        # are frozen at the DTO boundary.
        session.rollback()
        raise PropertyWorkRoleAssignmentInvariantViolated(
            f"property_work_role_assignment update rejected by the database "
            f"({exc.orig}); check that property_pay_rule_id references a "
            f"live pay_rule row"
        ) from exc

    write_audit(
        session,
        ctx,
        entity_kind="property_work_role_assignment",
        entity_id=row.id,
        action="property_work_role_assignment.updated",
        diff={"before": before, "after": after},
        clock=resolved_clock,
    )
    return _row_to_view(row)


def delete_property_work_role_assignment(
    session: Session,
    ctx: WorkspaceContext,
    *,
    assignment_id: str,
    clock: Clock | None = None,
) -> PropertyWorkRoleAssignmentView:
    """Soft-delete a property_work_role_assignment row.

    Stamps ``deleted_at``; the partial UNIQUE on
    ``(user_work_role_id, property_id) WHERE deleted_at IS NULL``
    excludes tombstoned rows so a re-pin after an archive mints a
    fresh row without colliding with the historical one.

    A repeated call on an already-deleted row raises
    :class:`PropertyWorkRoleAssignmentNotFound` — the row is invisible
    to the default tenancy-scoped lookup. Matches the user_work_roles
    convention: the §12 ``DELETE`` endpoint returns 204 / 404 and
    never echoes "already gone" as a distinct surface.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    row = _load_row(session, ctx, assignment_id=assignment_id)

    row.deleted_at = now
    row.updated_at = now
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="property_work_role_assignment",
        entity_id=row.id,
        action="property_work_role_assignment.deleted",
        diff={
            "user_work_role_id": row.user_work_role_id,
            "property_id": row.property_id,
        },
        clock=resolved_clock,
    )
    return _row_to_view(row)
