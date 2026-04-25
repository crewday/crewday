"""Permission-groups HTTP router — ``/permission_groups`` (spec §12).

Mounted inside the ``/w/<slug>/api/v1`` tree by the app factory. Every
route requires an active :class:`~app.tenancy.WorkspaceContext`. v1
surface:

* ``GET /permission_groups`` — cursor-paginated list of every group
  (system + user-defined) in the caller's workspace. Supports
  ``scope_kind`` query for spec parity (only ``workspace`` lands rows
  in v1; ``organization`` returns empty).
* ``POST /permission_groups`` — create a user-defined group. System
  groups are seeded at workspace creation only; the service rejects
  ``system=True`` writes.
* ``GET /permission_groups/{id}`` — read a single group.
* ``PATCH /permission_groups/{id}`` — rename / update capabilities.
  System groups accept only a ``name`` change (capabilities frozen).
* ``DELETE /permission_groups/{id}`` — delete a user-defined group.
  System groups raise 409 ``system_group_protected``.
* ``GET /permission_groups/{id}/members`` — list explicit members.
* ``POST /permission_groups/{id}/members`` — add a member; idempotent.
* ``DELETE /permission_groups/{id}/members/{user_id}`` — remove a
  member; idempotent. Last-owner guard at the domain layer fires 422
  ``would_orphan_owners_group`` and writes a forensic rejection
  audit row on a fresh UoW.

Action gates per §05:

* ``groups.create`` — POST /permission_groups (default-allow owners +
  managers, root_protected_deny).
* ``groups.edit`` — PATCH + DELETE /permission_groups/{id}.
* ``groups.manage_members`` — POST + DELETE
  /permission_groups/{id}/members.
* ``groups.manage_owners_membership`` — owners-group membership
  writes only (root-only). Layered on top of ``groups.manage_members``
  via a runtime branch in the handler since the action key is per-row,
  not per-route.
* ``scope.view`` — GET listing + read + members listing. Default-allow
  on ``scope.view`` covers every grant role (owners + managers +
  all_workers + all_clients), so anyone with workspace membership can
  introspect the group catalog. Membership rosters are not redacted
  in v1; if that becomes a privacy concern (workers learning who
  else is in the ``managers`` group), tighten the gate here.

See ``docs/specs/02-domain-model.md`` §"permission_group",
``docs/specs/05-employees-and-roles.md`` §"Permissions" and
``docs/specs/12-rest-api.md`` §"Users / work roles / settings".
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.adapters.db.session import make_uow
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.authz import Permission, require
from app.authz.enforce import PermissionDenied
from app.domain.identity.permission_groups import (
    LastOwnerMember,
    PermissionGroupMemberRef,
    PermissionGroupNotFound,
    PermissionGroupRef,
    PermissionGroupSlugTaken,
    SystemGroupProtected,
    UnknownCapability,
    add_member,
    create_group,
    delete_group,
    get_group,
    list_groups,
    list_members,
    remove_member,
    update_group,
    write_member_remove_rejected_audit,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "AddMemberRequest",
    "PermissionGroupCreateRequest",
    "PermissionGroupListResponse",
    "PermissionGroupMemberResponse",
    "PermissionGroupMembersListResponse",
    "PermissionGroupResponse",
    "PermissionGroupUpdateRequest",
    "build_permission_groups_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]

_log = logging.getLogger(__name__)


# Spec §05 lists ``workspace`` and ``organization`` as group scope
# kinds; v1 only stores workspace-scoped rows. Surface the literal so
# the OpenAPI shape is honest about what the listing accepts.
GroupScopeKindLiteral = Literal["workspace", "organization"]


# ---------------------------------------------------------------------------
# Wire-facing shapes
# ---------------------------------------------------------------------------


class PermissionGroupCreateRequest(BaseModel):
    """Request body for ``POST /permission_groups``.

    ``slug`` is the workspace-unique handle; ``name`` is the
    human-readable label rendered in the UI. ``capabilities`` is a
    flat ``{action_key: bool | dict}`` mapping; unknown keys raise
    422 ``unknown_action_key``.
    """

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=160)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class PermissionGroupUpdateRequest(BaseModel):
    """Request body for ``PATCH /permission_groups/{id}``.

    Sparse-explicit: omitted fields stay put. Setting
    ``capabilities`` on a system group is rejected at the domain
    layer with 409 ``system_group_protected``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    capabilities: dict[str, Any] | None = Field(default=None)


class PermissionGroupResponse(BaseModel):
    """Response shape for group reads + writes."""

    id: str
    slug: str
    name: str
    system: bool
    capabilities: dict[str, Any]
    created_at: datetime


class PermissionGroupListResponse(BaseModel):
    """Collection envelope for ``GET /permission_groups``."""

    data: list[PermissionGroupResponse]
    next_cursor: str | None = None
    has_more: bool = False


class AddMemberRequest(BaseModel):
    """Request body for ``POST /permission_groups/{id}/members``."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1, max_length=64)


class PermissionGroupMemberResponse(BaseModel):
    """Response shape for group member reads + writes."""

    group_id: str
    user_id: str
    added_at: datetime
    added_by_user_id: str | None


class PermissionGroupMembersListResponse(BaseModel):
    """Collection envelope for ``GET /permission_groups/{id}/members``."""

    data: list[PermissionGroupMemberResponse]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ref_to_response(ref: PermissionGroupRef) -> PermissionGroupResponse:
    return PermissionGroupResponse(
        id=ref.id,
        slug=ref.slug,
        name=ref.name,
        system=ref.system,
        capabilities=dict(ref.capabilities),
        created_at=ref.created_at,
    )


def _member_to_response(ref: PermissionGroupMemberRef) -> PermissionGroupMemberResponse:
    return PermissionGroupMemberResponse(
        group_id=ref.group_id,
        user_id=ref.user_id,
        added_at=ref.added_at,
        added_by_user_id=ref.added_by_user_id,
    )


def _http_for_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "permission_group_not_found"},
    )


def _http_for_slug_taken(exc: PermissionGroupSlugTaken) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"error": "permission_group_slug_taken", "message": str(exc)},
    )


def _http_for_system_protected(exc: SystemGroupProtected) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"error": "system_group_protected", "message": str(exc)},
    )


def _http_for_unknown_capability(exc: UnknownCapability) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": "unknown_action_key", "message": str(exc)},
    )


def _http_for_last_owner(exc: LastOwnerMember) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": "would_orphan_owners_group", "message": str(exc)},
    )


def _is_owners_group(session: Session, ctx: WorkspaceContext, *, group_id: str) -> bool:
    """Return ``True`` when ``group_id`` is the workspace's system ``owners`` group.

    The action gate for owners-membership writes is the root-only
    ``groups.manage_owners_membership``; every other group accepts
    the rule-driven ``groups.manage_members``. We resolve the
    distinction inside the handler (the action key depends on the
    targeted *row*, not the route shape).
    """
    try:
        ref = get_group(session, ctx, group_id=group_id)
    except PermissionGroupNotFound:
        return False
    return ref.slug == "owners" and ref.system


def _gate_member_write(
    session: Session, ctx: WorkspaceContext, *, group_id: str
) -> None:
    """Enforce the right action gate for a membership write.

    ``groups.manage_owners_membership`` (root-only) for the system
    ``owners`` group; ``groups.manage_members`` (rule-driven) for
    everything else. Mirrors the spec §05 split.
    """
    if _is_owners_group(session, ctx, group_id=group_id):
        action = "groups.manage_owners_membership"
    else:
        action = "groups.manage_members"
    try:
        require(
            session,
            ctx,
            action_key=action,
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
        )
    except PermissionDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "permission_denied", "action_key": action},
        ) from exc


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


_ScopeKindFilter = Annotated[
    GroupScopeKindLiteral | None,
    Query(
        description=(
            "Narrow the listing to groups of the named scope kind. "
            "v1 stores ``workspace`` rows only; ``organization`` "
            "returns an empty page."
        ),
    ),
]
_ScopeIdFilter = Annotated[
    str | None,
    Query(
        max_length=64,
        description=(
            "Narrow the listing to a specific scope id. Defaults to "
            "the caller's workspace; cross-workspace ids return empty."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_permission_groups_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for permission-group ops."""
    api = APIRouter(prefix="/permission_groups", tags=["identity", "permission_groups"])

    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))
    create_gate = Depends(Permission("groups.create", scope_kind="workspace"))
    edit_gate = Depends(Permission("groups.edit", scope_kind="workspace"))

    @api.get(
        "",
        response_model=PermissionGroupListResponse,
        operation_id="permission_groups.list",
        summary="List permission groups in the caller's workspace",
        dependencies=[view_gate],
        openapi_extra={
            "x-cli": {
                "group": "permission-groups",
                "verb": "list",
                "summary": "List permission groups",
                "mutates": False,
            },
        },
    )
    def list_(
        ctx: _Ctx,
        session: _Db,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
        scope_kind: _ScopeKindFilter = None,
        scope_id: _ScopeIdFilter = None,
    ) -> PermissionGroupListResponse:
        """Cursor-paginated listing of every group in the caller's workspace.

        ``scope_kind=organization`` returns an empty page in v1; the
        organization tree lands in a follow-up. ``scope_id`` other
        than the caller's workspace id returns empty rather than 404
        — the workspace prefix already pins the tenant.
        """
        if scope_kind == "organization":
            # No org-scoped groups in v1; empty page is the honest answer.
            return PermissionGroupListResponse(
                data=[], next_cursor=None, has_more=False
            )
        if scope_id is not None and scope_id != ctx.workspace_id:
            # Cross-workspace scope_id: not enumerable from this tenant.
            return PermissionGroupListResponse(
                data=[], next_cursor=None, has_more=False
            )

        after_id = decode_cursor(cursor)
        refs = list_groups(session, ctx)
        if after_id is not None:
            refs = [r for r in refs if r.id > after_id]
        sliced = refs[: limit + 1]
        page = paginate(
            sliced,
            limit=limit,
            key_getter=lambda r: r.id,
        )
        return PermissionGroupListResponse(
            data=[_ref_to_response(r) for r in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @api.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=PermissionGroupResponse,
        operation_id="permission_groups.create",
        summary="Create a user-defined permission group",
        dependencies=[create_gate],
        openapi_extra={
            "x-cli": {
                "group": "permission-groups",
                "verb": "create",
                "summary": "Create a permission group",
                "mutates": True,
            },
        },
    )
    def create(
        body: PermissionGroupCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> PermissionGroupResponse:
        """Insert a non-system group; (workspace, slug) unique."""
        try:
            ref = create_group(
                session,
                ctx,
                slug=body.slug,
                name=body.name,
                capabilities=body.capabilities,
            )
        except UnknownCapability as exc:
            raise _http_for_unknown_capability(exc) from exc
        except PermissionGroupSlugTaken as exc:
            raise _http_for_slug_taken(exc) from exc
        return _ref_to_response(ref)

    @api.get(
        "/{group_id}",
        response_model=PermissionGroupResponse,
        operation_id="permission_groups.read",
        summary="Read a permission group by id",
        dependencies=[view_gate],
        openapi_extra={
            "x-cli": {
                "group": "permission-groups",
                "verb": "show",
                "summary": "Read a permission group",
                "mutates": False,
            },
        },
    )
    def read(
        group_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> PermissionGroupResponse:
        """Return one group view or 404."""
        try:
            ref = get_group(session, ctx, group_id=group_id)
        except PermissionGroupNotFound as exc:
            raise _http_for_not_found() from exc
        return _ref_to_response(ref)

    @api.patch(
        "/{group_id}",
        response_model=PermissionGroupResponse,
        operation_id="permission_groups.update",
        summary="Update a permission group",
        dependencies=[edit_gate],
        openapi_extra={
            "x-cli": {
                "group": "permission-groups",
                "verb": "update",
                "summary": "Rename / re-capability a permission group",
                "mutates": True,
            },
        },
    )
    def update(
        group_id: str,
        body: PermissionGroupUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> PermissionGroupResponse:
        """Rename + re-capability a group.

        System groups accept only a ``name`` change; mutating
        capabilities raises 409 ``system_group_protected``. Unknown
        capability keys raise 422 ``unknown_action_key``. An empty
        body is a no-op write that still emits an audit row (matches
        the domain service's contract).
        """
        try:
            ref = update_group(
                session,
                ctx,
                group_id=group_id,
                name=body.name,
                capabilities=body.capabilities,
            )
        except PermissionGroupNotFound as exc:
            raise _http_for_not_found() from exc
        except SystemGroupProtected as exc:
            raise _http_for_system_protected(exc) from exc
        except UnknownCapability as exc:
            raise _http_for_unknown_capability(exc) from exc
        return _ref_to_response(ref)

    @api.delete(
        "/{group_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="permission_groups.delete",
        summary="Delete a user-defined permission group",
        dependencies=[edit_gate],
        openapi_extra={
            "x-cli": {
                "group": "permission-groups",
                "verb": "delete",
                "summary": "Delete a user-defined permission group",
                "mutates": True,
            },
        },
    )
    def delete(
        group_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        """Hard-delete a non-system group. Members cascade via FK."""
        try:
            delete_group(session, ctx, group_id=group_id)
        except PermissionGroupNotFound as exc:
            raise _http_for_not_found() from exc
        except SystemGroupProtected as exc:
            raise _http_for_system_protected(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get(
        "/{group_id}/members",
        response_model=PermissionGroupMembersListResponse,
        operation_id="permission_groups.members.list",
        summary="List explicit members of a permission group",
        dependencies=[view_gate],
        openapi_extra={
            "x-cli": {
                "group": "permission-groups",
                "verb": "members-list",
                "summary": "List members of a permission group",
                "mutates": False,
            },
        },
    )
    def list_members_handler(
        group_id: str,
        ctx: _Ctx,
        session: _Db,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> PermissionGroupMembersListResponse:
        """Cursor-paginated listing of explicit member rows.

        Derived groups (``managers``, ``all_workers``, ``all_clients``)
        carry no explicit member rows in v1 (§02 "Derived group
        membership"); calling this on one returns an empty page (the
        domain service walks the same SELECT regardless).
        """
        try:
            members = list_members(session, ctx, group_id=group_id)
        except PermissionGroupNotFound as exc:
            raise _http_for_not_found() from exc
        # Cursor on ``user_id`` (composite PK is ``(group_id, user_id)``;
        # ``user_id`` is the row's natural ordering inside a group).
        after_id = decode_cursor(cursor)
        if after_id is not None:
            members = [m for m in members if m.user_id > after_id]
        sliced = list(members[: limit + 1])
        page = paginate(
            sliced,
            limit=limit,
            key_getter=lambda m: m.user_id,
        )
        return PermissionGroupMembersListResponse(
            data=[_member_to_response(m) for m in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @api.post(
        "/{group_id}/members",
        status_code=status.HTTP_201_CREATED,
        response_model=PermissionGroupMemberResponse,
        operation_id="permission_groups.members.add",
        summary="Add a member to a permission group — idempotent",
        openapi_extra={
            "x-cli": {
                "group": "permission-groups",
                "verb": "members-add",
                "summary": "Add a member to a permission group",
                "mutates": True,
            },
        },
    )
    def add_member_handler(
        group_id: str,
        body: AddMemberRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> PermissionGroupMemberResponse:
        """Insert (or refresh) a (group, user) membership row.

        The action gate (``groups.manage_members`` or
        ``groups.manage_owners_membership``) fires per-row inside the
        handler — see :func:`_gate_member_write`.
        """
        _gate_member_write(session, ctx, group_id=group_id)
        try:
            ref = add_member(session, ctx, group_id=group_id, user_id=body.user_id)
        except PermissionGroupNotFound as exc:
            raise _http_for_not_found() from exc
        return _member_to_response(ref)

    @api.delete(
        "/{group_id}/members/{user_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="permission_groups.members.remove",
        summary="Remove a member from a permission group — idempotent",
        openapi_extra={
            "x-cli": {
                "group": "permission-groups",
                "verb": "members-remove",
                "summary": "Remove a member from a permission group",
                "mutates": True,
            },
        },
    )
    def remove_member_handler(
        group_id: str,
        user_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        """Delete a (group, user) membership row.

        The last-owner guard at the domain layer prevents stripping
        the sole member of the system ``owners`` group; the typed
        exception rolls back the caller's UoW, so the rejection audit
        row is written on a fresh UoW via
        :func:`write_member_remove_rejected_audit`.
        """
        _gate_member_write(session, ctx, group_id=group_id)
        try:
            remove_member(session, ctx, group_id=group_id, user_id=user_id)
        except PermissionGroupNotFound as exc:
            raise _http_for_not_found() from exc
        except LastOwnerMember as exc:
            # Open a fresh UoW so the forensic ``member_remove_rejected``
            # row survives the primary UoW's rollback.
            try:
                with make_uow() as audit_session:
                    assert isinstance(audit_session, Session)
                    write_member_remove_rejected_audit(
                        audit_session,
                        ctx,
                        group_id=group_id,
                        user_id=user_id,
                    )
            except Exception:
                # Rescue audit must never shadow the primary 422; log
                # and continue. The primary UoW already rolled back so
                # the membership stays intact regardless. Mirrors the
                # canon in :mod:`app.api.v1.users` for the workspace-
                # member remove path.
                _log.warning(
                    "permission_groups.member_remove_rejected audit failed; "
                    "primary 422 still surfaced",
                    exc_info=True,
                )
            raise _http_for_last_owner(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return api


# Module-level router for the v1 app factory's eager import. Tests
# that want a fresh instance per case should call
# :func:`build_permission_groups_router` directly to avoid cross-test
# leaks on FastAPI's dependency-override cache.
router = build_permission_groups_router()
