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
   this; the SA repo catches the IntegrityError and surfaces it as
   :class:`~app.domain.places.ports.DuplicateActiveAssignment`, which
   the service re-raises as
   :class:`PropertyWorkRoleAssignmentInvariantViolated` so the HTTP
   layer can return a 409.

**Tenancy.** The ``property_work_role_assignment`` table carries a
denormalised ``workspace_id`` column and is registered as
workspace-scoped, so the ORM tenant filter narrows every SELECT to the
caller's workspace. The repo re-asserts the predicate explicitly as
defence-in-depth.

**Architecture (cd-kezq).** The module talks to a
:class:`~app.domain.places.ports.PropertyWorkRoleAssignmentRepository`
Protocol — never to the SQLAlchemy model classes directly. The
SA-backed concretion lives at
:class:`app.adapters.db.places.repositories.SqlAlchemyPropertyWorkRoleAssignmentRepository`;
unit tests inject a fake or wire the SA repo over an in-memory
SQLite session. The repo also threads its open
:class:`~sqlalchemy.orm.Session` through ``repo.session`` so the
audit writer (``app.audit.write_audit``) — which still takes a
concrete ``Session`` today — can keep using the same UoW.

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

from app.audit import write_audit
from app.domain.places.ports import (
    AssignmentIntegrityError,
    DuplicateActiveAssignment,
    PropertyWorkRoleAssignmentRepository,
    PropertyWorkRoleAssignmentRow,
)
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
      :class:`~app.domain.places.ports.DuplicateActiveAssignment`).
      The HTTP router translates this duplicate flavour into 409, the
      rest into 422.
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
# Row ↔ view projection
# ---------------------------------------------------------------------------


def _row_to_view(row: PropertyWorkRoleAssignmentRow) -> PropertyWorkRoleAssignmentView:
    """Project a seam-level row into the public view.

    The repo already returned an immutable, frozen value object; we
    re-pack it into the public :class:`PropertyWorkRoleAssignmentView`
    shape so callers keep the dataclass they were already typing
    against.
    """
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_row(
    repo: PropertyWorkRoleAssignmentRepository,
    ctx: WorkspaceContext,
    *,
    assignment_id: str,
    include_deleted: bool = False,
) -> PropertyWorkRoleAssignmentRow:
    """Return the row or raise :class:`PropertyWorkRoleAssignmentNotFound`."""
    row = repo.get(
        workspace_id=ctx.workspace_id,
        assignment_id=assignment_id,
        include_deleted=include_deleted,
    )
    if row is None:
        raise PropertyWorkRoleAssignmentNotFound(assignment_id)
    return row


def _assert_user_work_role_in_workspace(
    repo: PropertyWorkRoleAssignmentRepository,
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
    if not repo.user_work_role_exists_in_workspace(
        workspace_id=ctx.workspace_id,
        user_work_role_id=user_work_role_id,
    ):
        raise PropertyWorkRoleAssignmentInvariantViolated(
            f"user_work_role {user_work_role_id!r} does not exist in this workspace"
        )


def _assert_property_in_workspace(
    repo: PropertyWorkRoleAssignmentRepository,
    ctx: WorkspaceContext,
    *,
    property_id: str,
) -> None:
    """Raise if ``property_id`` is not linked to the caller's workspace.

    Reachability is "live ``property_workspace`` row joining the
    property to ``ctx.workspace_id``" — where "live" means
    ``status = 'active'`` (§02 "property_workspace.status"); rows
    still in the ``invited`` pre-acceptance state do not count, as
    the workspace has not yet taken operational control. A workspace
    cannot pin a role to a property it does not operate (§02
    "property_work_role_assignment" invariant 2).
    """
    if not repo.property_in_workspace(
        workspace_id=ctx.workspace_id,
        property_id=property_id,
    ):
        raise PropertyWorkRoleAssignmentInvariantViolated(
            f"property {property_id!r} is not linked to this workspace"
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_property_work_role_assignments(
    repo: PropertyWorkRoleAssignmentRepository,
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
    rows = repo.list(
        workspace_id=ctx.workspace_id,
        limit=limit,
        after_id=after_id,
        property_id=property_id,
        user_work_role_id=user_work_role_id,
        include_deleted=include_deleted,
    )
    return [_row_to_view(r) for r in rows]


def get_property_work_role_assignment(
    repo: PropertyWorkRoleAssignmentRepository,
    ctx: WorkspaceContext,
    *,
    assignment_id: str,
) -> PropertyWorkRoleAssignmentView:
    """Return a single view or raise on miss."""
    return _row_to_view(
        _load_row(repo, ctx, assignment_id=assignment_id),
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_property_work_role_assignment(
    repo: PropertyWorkRoleAssignmentRepository,
    ctx: WorkspaceContext,
    *,
    body: PropertyWorkRoleAssignmentCreate,
    clock: Clock | None = None,
) -> PropertyWorkRoleAssignmentView:
    """Insert a new property_work_role_assignment row.

    Runs both §02 / §05 invariants (user_work_role belongs to
    workspace; property reachable via ``property_workspace``) before
    attempting the flush. A
    :class:`~app.domain.places.ports.DuplicateActiveAssignment` raised
    by the repo on the partial UNIQUE
    ``uq_property_work_role_assignment_role_property_active`` is
    collapsed into :class:`PropertyWorkRoleAssignmentInvariantViolated`
    so the HTTP layer can map it to 409. Other integrity violations
    (FK miss on ``property_pay_rule_id``) surface via
    :class:`~app.domain.places.ports.AssignmentIntegrityError` and map
    to a non-duplicate invariant message → 422.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    _assert_user_work_role_in_workspace(
        repo, ctx, user_work_role_id=body.user_work_role_id
    )
    _assert_property_in_workspace(repo, ctx, property_id=body.property_id)

    # Pre-flight the partial-UNIQUE check so the duplicate-row case
    # surfaces with the canonical "already exists" message *before*
    # any IntegrityError from another constraint (e.g. the FK on
    # ``property_pay_rule_id``) muddies the surface. The flush-time
    # :class:`DuplicateActiveAssignment` catch below stays as
    # defence-in-depth against a parallel insert race; it surfaces
    # with the same message so the HTTP layer's substring check keeps
    # mapping to 409.
    existing_live = repo.find_active_for_role_property(
        workspace_id=ctx.workspace_id,
        user_work_role_id=body.user_work_role_id,
        property_id=body.property_id,
    )
    if existing_live is not None:
        raise PropertyWorkRoleAssignmentInvariantViolated(
            f"property_work_role_assignment for "
            f"user_work_role={body.user_work_role_id!r} "
            f"property={body.property_id!r} already exists"
        )

    assignment_id = new_ulid(clock=clock)
    try:
        row = repo.insert(
            assignment_id=assignment_id,
            workspace_id=ctx.workspace_id,
            user_work_role_id=body.user_work_role_id,
            property_id=body.property_id,
            schedule_ruleset_id=body.schedule_ruleset_id,
            property_pay_rule_id=body.property_pay_rule_id,
            now=now,
        )
    except DuplicateActiveAssignment as exc:
        # Parallel insert race that beat the pre-flight SELECT — re-raise
        # with the canonical message the HTTP layer keys on for 409.
        raise PropertyWorkRoleAssignmentInvariantViolated(
            f"property_work_role_assignment for "
            f"user_work_role={body.user_work_role_id!r} "
            f"property={body.property_id!r} already exists"
        ) from exc
    except AssignmentIntegrityError as exc:
        # Realistic flavour: FK miss on ``property_pay_rule_id``. The
        # message is non-duplicate so the HTTP layer maps it to 422
        # (not 409). We surface the driver text so the operator sees
        # which constraint actually fired.
        raise PropertyWorkRoleAssignmentInvariantViolated(
            f"property_work_role_assignment write rejected by the database "
            f"({exc.db_message}); check that property_pay_rule_id and "
            f"schedule_ruleset_id reference live rows"
        ) from exc

    write_audit(
        repo.session,
        ctx,
        entity_kind="property_work_role_assignment",
        entity_id=row.id,
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
    repo: PropertyWorkRoleAssignmentRepository,
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

    row = _load_row(repo, ctx, assignment_id=assignment_id)

    sent = body.model_fields_set
    if not sent:
        return _row_to_view(row)

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    new_schedule_ruleset_id = row.schedule_ruleset_id
    new_property_pay_rule_id = row.property_pay_rule_id

    if (
        "schedule_ruleset_id" in sent
        and body.schedule_ruleset_id != row.schedule_ruleset_id
    ):
        before["schedule_ruleset_id"] = row.schedule_ruleset_id
        after["schedule_ruleset_id"] = body.schedule_ruleset_id
        new_schedule_ruleset_id = body.schedule_ruleset_id

    if (
        "property_pay_rule_id" in sent
        and body.property_pay_rule_id != row.property_pay_rule_id
    ):
        before["property_pay_rule_id"] = row.property_pay_rule_id
        after["property_pay_rule_id"] = body.property_pay_rule_id
        new_property_pay_rule_id = body.property_pay_rule_id

    if not after:
        return _row_to_view(row)

    try:
        updated = repo.update_pointers(
            workspace_id=ctx.workspace_id,
            assignment_id=assignment_id,
            schedule_ruleset_id=new_schedule_ruleset_id,
            property_pay_rule_id=new_property_pay_rule_id,
            now=now,
        )
    except AssignmentIntegrityError as exc:
        # The only realistic flush-time failure here is the FK on
        # ``property_pay_rule_id`` — a caller pointing at a pay_rule
        # row that does not exist or was hard-deleted. Surface as a
        # 422 invariant violation so the HTTP layer doesn't leak a
        # 500. The partial UNIQUE cannot fire on update because the
        # identity columns ``user_work_role_id`` + ``property_id``
        # are frozen at the DTO boundary.
        raise PropertyWorkRoleAssignmentInvariantViolated(
            f"property_work_role_assignment update rejected by the database "
            f"({exc.db_message}); check that property_pay_rule_id references a "
            f"live pay_rule row"
        ) from exc

    write_audit(
        repo.session,
        ctx,
        entity_kind="property_work_role_assignment",
        entity_id=updated.id,
        action="property_work_role_assignment.updated",
        diff={"before": before, "after": after},
        clock=resolved_clock,
    )
    return _row_to_view(updated)


def delete_property_work_role_assignment(
    repo: PropertyWorkRoleAssignmentRepository,
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

    # Defensive existence check so the SA repo's ``soft_delete`` can
    # assume the row is live — keeps the seam shape simple (no
    # NotFound path inside the adapter).
    _load_row(repo, ctx, assignment_id=assignment_id)
    row = repo.soft_delete(
        workspace_id=ctx.workspace_id,
        assignment_id=assignment_id,
        now=now,
    )

    write_audit(
        repo.session,
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
