"""Deployment-admin team management routes.

Mounts under ``/admin/api/v1`` (§12 "Admin surface"):

* ``GET /admins`` — every active deployment ``role_grant`` row.
* ``POST /admins`` — owners-only grant of deployment-admin to a
  user (by id or email).
* ``POST /admins/{id}/revoke`` — owners-only revoke of a
  deployment-admin grant.
* ``GET /admins/groups`` — owners + managers deployment groups
  with their members.
* ``POST /admins/groups/owners/members`` — add a user to the
  deployment owners group. Owners-only.
* ``POST /admins/groups/owners/members/{user_id}/revoke`` —
  remove a user from the deployment owners group. Owners-only.

Listing routes are read-only; ``GET /admins`` ships with the
same shape as :class:`app.api.admin.me.AdminTeamMemberResponse`
(re-used here so the SPA's admin team page consumes one wire
type whether it lands on ``/admin/api/v1/me/admins`` or
``/admin/api/v1/admins``).

See ``docs/specs/12-rest-api.md`` §"Admin surface" §"Admin team".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import DeploymentOwner, RoleGrant
from app.adapters.db.identity.models import User, canonicalise_email
from app.api.admin._audit import audit_admin
from app.api.admin._owners import ensure_deployment_owner
from app.api.admin.deps import current_deployment_admin_principal
from app.api.admin.me import AdminTeamMemberResponse
from app.api.deps import db_session
from app.api.transport import admin_sse
from app.authz.deployment_owners import (
    add_deployment_owner,
    deployment_owner_count,
    deployment_owner_user_ids,
    remove_deployment_owner,
)
from app.tenancy import DeploymentContext, tenant_agnostic
from app.util.ulid import new_ulid

__all__ = [
    "AdminGrantRequest",
    "AdminGrantResponse",
    "AdminListResponse",
    "AdminRevokeResponse",
    "GroupMemberRequest",
    "OwnersGroupResponse",
    "build_admin_admins_router",
]


_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Response + request models
# ---------------------------------------------------------------------------


class AdminListResponse(BaseModel):
    """Body of ``GET /admin/api/v1/admins``.

    Same row shape as ``GET /admin/api/v1/me/admins`` so the SPA
    consumes one type. The wrapper adds no envelope fields here;
    a future cursor-paginated shape would extend without breaking
    existing consumers.
    """

    admins: list[AdminTeamMemberResponse]


class AdminGrantRequest(BaseModel):
    """Request body for ``POST /admin/api/v1/admins``.

    The grantor identifies the new admin by either ``user_id``
    (canonical lookup) or ``email`` (operator-friendly lookup).
    Exactly one of the two must be present — both unset is a
    422 ``missing_target``; both set is a 422 ``ambiguous_target``.
    """

    user_id: str | None = Field(default=None, max_length=64)
    # Plain ``str`` rather than :class:`pydantic.EmailStr` for the
    # same reason the magic-link / recovery routers use it: the
    # domain layer already canonicalises with
    # :func:`canonicalise_email` before any DB lookup, so pulling
    # in ``email-validator`` just to re-validate here would
    # duplicate the contract for no DB-shape gain.
    email: str | None = Field(default=None, max_length=320)


class AdminGrantResponse(BaseModel):
    """Body of ``POST /admin/api/v1/admins``.

    Echoes the freshly-minted (or already-existing) admin row
    in the same shape as the listing endpoint, so the SPA's
    optimistic cache can splice it without a re-fetch.
    """

    admin: AdminTeamMemberResponse


class AdminRevokeResponse(BaseModel):
    """Body of ``POST /admin/api/v1/admins/{id}/revoke``.

    Returns the revoked grant id so the SPA's optimistic cache
    can splice the row out without a re-fetch. Idempotent
    re-revoke returns the same id.
    """

    revoked_id: str


class GroupMemberRequest(BaseModel):
    """Request body for the owners-group add route.

    Same shape as :class:`AdminGrantRequest` — the operator
    identifies the new owner by either ``user_id`` or ``email``.
    Owner-only.
    """

    user_id: str | None = Field(default=None, max_length=64)
    # Same plain-``str`` rationale as :class:`AdminGrantRequest.email`.
    email: str | None = Field(default=None, max_length=320)


class GroupMemberInfo(BaseModel):
    """One member entry on ``GET /admin/api/v1/admins/groups``.

    Mirrors the SPA's :interface:`AdminTeamMember` shape but
    drops the grant-row fields. Owners carry ``deployment_owner``
    timestamps; managers are derived from deployment ``role_grant``
    rows and projected into the same shape.
    """

    user_id: str
    display_name: str
    email: str
    added_at: str
    added_by: str


class GroupResponse(BaseModel):
    """One group entry on ``GET /admin/api/v1/admins/groups``."""

    slug: str
    name: str
    members: list[GroupMemberInfo]


class GroupsListResponse(BaseModel):
    """Body of ``GET /admin/api/v1/admins/groups``.

    Owners are explicit ``deployment_owner`` rows; managers are
    derived from active deployment admin grants.
    """

    groups: list[GroupResponse]


class OwnersGroupResponse(BaseModel):
    """Body of the owners-group add / revoke routes.

    Echoes the freshly-changed owners member list.
    """

    members: list[GroupMemberInfo]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ERROR_MISSING_TARGET = "missing_target"
_ERROR_AMBIGUOUS_TARGET = "ambiguous_target"
_ERROR_USER_NOT_FOUND = "user_not_found"
_ERROR_LAST_OWNER = "last_owner"


def _problem(error: str, *, message: str, http_status: int = 422) -> HTTPException:
    """Build the canonical typed-error envelope.

    Spec §12 "Errors" envelopes — 422 for client-side validation,
    404 for "not found" lookups. Keeps the error code in the body
    so the SPA gates on a single field.
    """
    return HTTPException(
        status_code=http_status,
        detail={"error": error, "message": message},
    )


def _resolve_target_user(
    session: Session, *, request_body: AdminGrantRequest | GroupMemberRequest
) -> User:
    """Look up the target user by id or email.

    Validates the "exactly one of user_id / email" invariant and
    raises typed 422s for the violations. A successful lookup
    returns the live :class:`User` row; a missing user raises
    404 ``user_not_found`` so an operator who fat-fingered an
    email sees a friendlier shape than the surface-invisible
    404 the admin auth dep emits.
    """
    if request_body.user_id is None and request_body.email is None:
        raise _problem(
            _ERROR_MISSING_TARGET,
            message="must specify either user_id or email",
        )
    if request_body.user_id is not None and request_body.email is not None:
        raise _problem(
            _ERROR_AMBIGUOUS_TARGET,
            message="specify exactly one of user_id or email",
        )
    with tenant_agnostic():
        user: User | None = None
        if request_body.user_id is not None:
            user = session.get(User, request_body.user_id)
        else:
            assert request_body.email is not None
            email_lower = canonicalise_email(request_body.email)
            user = session.scalars(
                select(User).where(User.email_lower == email_lower)
            ).first()
    if user is None:
        raise _problem(
            _ERROR_USER_NOT_FOUND,
            message="no user matches the supplied identifier",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    if user.archived_at is not None:
        # An archived user cannot be granted new authority. Surface as
        # ``user_not_found`` so the response shape matches the missing
        # case — the admin tree never advertises tombstoned identities.
        raise _problem(
            _ERROR_USER_NOT_FOUND,
            message="no user matches the supplied identifier",
            http_status=status.HTTP_404_NOT_FOUND,
        )
    return user


def _format_granted_at(grant: RoleGrant) -> str:
    """ISO-8601 UTC for ``grant.created_at``. Matches :mod:`app.api.admin.me`."""
    moment = grant.created_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat()


def _project_member(
    grant: RoleGrant,
    user: User,
    *,
    owner_user_ids: frozenset[str] | None = None,
) -> AdminTeamMemberResponse:
    """Build the wire-shaped admin row from a (grant, user) pair."""
    return AdminTeamMemberResponse(
        id=grant.id,
        user_id=user.id,
        display_name=user.display_name,
        email=user.email,
        is_owner=owner_user_ids is not None and user.id in owner_user_ids,
        granted_at=_format_granted_at(grant),
        granted_by=grant.created_by_user_id or "system",
    )


def _format_added_at(value: datetime) -> str:
    """ISO-8601 UTC for group-member timestamps."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _owner_members(session: Session) -> list[GroupMemberInfo]:
    """Return explicit ``owners@deployment`` members."""
    with tenant_agnostic():
        rows = session.execute(
            select(DeploymentOwner, User)
            .join(User, User.id == DeploymentOwner.user_id)
            .order_by(DeploymentOwner.added_at.asc(), DeploymentOwner.user_id.asc())
        ).all()
    return [
        GroupMemberInfo(
            user_id=user.id,
            display_name=user.display_name,
            email=user.email,
            added_at=_format_added_at(owner.added_at),
            added_by=owner.added_by_user_id or "system",
        )
        for owner, user in rows
    ]


def _manager_members(session: Session) -> list[GroupMemberInfo]:
    """Return deployment managers derived from live deployment admin grants."""
    with tenant_agnostic():
        rows = session.execute(
            select(RoleGrant, User)
            .join(User, User.id == RoleGrant.user_id)
            .where(RoleGrant.scope_kind == "deployment")
            # cd-x1xh: live grants only — soft-retired admin grants
            # stay in the table for audit but no longer surface in
            # the manager-members roster.
            .where(RoleGrant.revoked_at.is_(None))
            .order_by(RoleGrant.created_at.asc(), RoleGrant.id.asc())
        ).all()
    return [
        GroupMemberInfo(
            user_id=user.id,
            display_name=user.display_name,
            email=user.email,
            added_at=_format_granted_at(grant),
            added_by=grant.created_by_user_id or "system",
        )
        for grant, user in rows
    ]


def _existing_grant(session: Session, *, user_id: str) -> RoleGrant | None:
    """Return the user's active deployment grant, if any.

    The ``role_grant`` table carries a partial UNIQUE on
    ``(user_id, grant_role) WHERE scope_kind='deployment' AND
    revoked_at IS NULL`` (cd-x1xh) so at most one **live** row
    matches — the lookup pins on ``revoked_at IS NULL`` and uses
    ``.first()``.
    """
    with tenant_agnostic():
        return session.scalars(
            select(RoleGrant)
            .where(RoleGrant.scope_kind == "deployment")
            .where(RoleGrant.user_id == user_id)
            .where(RoleGrant.revoked_at.is_(None))
            .limit(1)
        ).first()


def _not_found() -> HTTPException:
    """Canonical 404 envelope — same shape as the admin auth dep emits."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found"},
    )


def build_admin_admins_router() -> APIRouter:
    """Return the router carrying the admin-team admin routes."""
    router = APIRouter(tags=["admin"])

    @router.get(
        "/admins",
        response_model=AdminListResponse,
        operation_id="admin.admins.list",
        summary="List every deployment admin",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "admins-list-all",
                "summary": "List every deployment admin",
                "mutates": False,
            },
        },
    )
    def list_admins(
        _ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
    ) -> AdminListResponse:
        """Return every ``role_grant`` with ``scope_kind='deployment'``.

        Same projection as :func:`app.api.admin.me.list_admins` so
        the SPA reads one shape regardless of which URL it polls.
        Listing alone here (no ``groups`` field) — the dedicated
        ``/admins/groups`` route exposes the group rosters.
        """
        with tenant_agnostic():
            rows = session.execute(
                select(RoleGrant, User)
                .join(User, User.id == RoleGrant.user_id)
                .where(RoleGrant.scope_kind == "deployment")
                # cd-x1xh: live grants only — soft-retired admin
                # grants stay in the table for audit but no longer
                # surface on the admin list.
                .where(RoleGrant.revoked_at.is_(None))
                .order_by(RoleGrant.created_at.asc(), RoleGrant.id.asc())
            ).all()
        owner_user_ids = deployment_owner_user_ids(session)
        admins = [
            _project_member(grant, user, owner_user_ids=owner_user_ids)
            for grant, user in rows
        ]
        return AdminListResponse(admins=admins)

    @router.post(
        "/admins",
        response_model=AdminGrantResponse,
        operation_id="admin.admins.grant",
        summary="Grant deployment-admin to a user",
        status_code=status.HTTP_200_OK,
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "admins-grant",
                "summary": "Grant deployment-admin to a user",
                "mutates": True,
            },
        },
    )
    def grant_admin(
        payload: AdminGrantRequest,
        ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        request: Request,
    ) -> AdminGrantResponse:
        """Mint a deployment-admin grant, idempotent on repeat.

        The partial UNIQUE on ``role_grant`` (``user_id, grant_role``
        scoped to ``scope_kind='deployment'``) means a double-call
        with the same target hits the ``existing_grant`` branch
        and returns the original row without a fresh insert. The
        audit row only fires on the first mint — re-grants don't
        emit duplicate ``admin.granted`` rows.

        Self-grant is allowed: a deployment admin promoting an
        agent token's underlying human is a reasonable workflow.
        The audit row attributes the mint to ``ctx.user_id``.
        """
        ensure_deployment_owner(session, ctx=ctx)
        target_user = _resolve_target_user(session, request_body=payload)
        existing = _existing_grant(session, user_id=target_user.id)
        if existing is not None:
            owner_user_ids = deployment_owner_user_ids(session)
            return AdminGrantResponse(
                admin=_project_member(
                    existing,
                    target_user,
                    owner_user_ids=owner_user_ids,
                )
            )
        now = datetime.now(UTC)
        with tenant_agnostic():
            grant = RoleGrant(
                id=new_ulid(),
                workspace_id=None,
                user_id=target_user.id,
                grant_role="manager",
                scope_kind="deployment",
                created_at=now,
                created_by_user_id=ctx.user_id,
            )
            session.add(grant)
            audit_admin(
                session,
                ctx=ctx,
                request=request,
                entity_kind="role_grant",
                entity_id=grant.id,
                action="admin.granted",
                diff={
                    "user_id": target_user.id,
                    "grant_role": "manager",
                    "scope_kind": "deployment",
                },
            )
            session.flush()
        admin_sse.publish_admin_event(
            kind="admin.admins.updated",
            ctx=ctx,
            request=request,
            payload={
                "action": "grant",
                "grant_id": grant.id,
                "user_id": target_user.id,
            },
        )
        owner_user_ids = deployment_owner_user_ids(session)
        return AdminGrantResponse(
            admin=_project_member(grant, target_user, owner_user_ids=owner_user_ids)
        )

    @router.post(
        "/admins/{id}/revoke",
        response_model=AdminRevokeResponse,
        operation_id="admin.admins.revoke",
        summary="Revoke a deployment-admin grant",
        status_code=status.HTTP_200_OK,
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "admins-revoke",
                "summary": "Revoke a deployment-admin grant",
                "mutates": True,
            },
        },
    )
    def revoke_admin(
        id: str,
        ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        request: Request,
    ) -> AdminRevokeResponse:
        """Soft-retire a deployment admin grant; audit the action.

        cd-x1xh moved deployment-admin revocation to the §02
        soft-retire shape: the row is preserved with ``revoked_at``
        + ``revoked_by_user_id`` + ``ended_on`` stamped, and live
        deployment-admin reads filter on ``revoked_at IS NULL``.
        The partial UNIQUE on ``(user_id, grant_role) WHERE
        scope_kind='deployment' AND revoked_at IS NULL`` carries
        the same predicate so a re-grant after revoke lands a
        fresh row alongside.

        Idempotent: revoking an already-revoked grant 404s (the
        live-only ``where`` filters it out); the first revoke is
        a 200 with the new state. Self-revoke is allowed — the
        last-deployment-admin guard lives separately (cd-79r
        will host it).
        """
        ensure_deployment_owner(session, ctx=ctx)
        now = datetime.now(UTC)
        with tenant_agnostic():
            grant = session.get(RoleGrant, id)
            if (
                grant is None
                or grant.scope_kind != "deployment"
                or grant.revoked_at is not None
            ):
                raise _not_found()
            grant.revoked_at = now
            grant.revoked_by_user_id = ctx.user_id
            grant.ended_on = now.date()
            audit_admin(
                session,
                ctx=ctx,
                request=request,
                entity_kind="role_grant",
                entity_id=id,
                action="admin.revoked",
                diff={"user_id": grant.user_id},
            )
            session.flush()
        admin_sse.publish_admin_event(
            kind="admin.admins.updated",
            ctx=ctx,
            request=request,
            payload={
                "action": "revoke",
                "grant_id": id,
                "user_id": grant.user_id,
            },
        )
        return AdminRevokeResponse(revoked_id=id)

    @router.get(
        "/admins/groups",
        response_model=GroupsListResponse,
        operation_id="admin.admins.groups.list",
        summary="List the deployment owners + managers groups",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "admins-groups-list",
                "summary": "List the deployment owners + managers groups",
                "mutates": False,
            },
        },
    )
    def list_groups(
        _ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
    ) -> GroupsListResponse:
        """Return the deployment owners and managers groups."""
        return GroupsListResponse(
            groups=[
                GroupResponse(
                    slug="owners",
                    name="Owners",
                    members=_owner_members(session),
                ),
                GroupResponse(
                    slug="managers",
                    name="Managers",
                    members=_manager_members(session),
                ),
            ]
        )

    @router.post(
        "/admins/groups/owners/members",
        response_model=OwnersGroupResponse,
        operation_id="admin.admins.groups.owners.add",
        summary="Add a user to the deployment owners group (owners-only)",
        status_code=status.HTTP_200_OK,
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "admins-owners-add",
                "summary": "Add a user to the deployment owners group",
                "mutates": True,
            },
        },
    )
    def add_owner(
        payload: GroupMemberRequest,
        ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        request: Request,
    ) -> OwnersGroupResponse:
        """Owners-only add to the deployment owners group."""
        ensure_deployment_owner(session, ctx=ctx)
        target = _resolve_target_user(session, request_body=payload)
        now = datetime.now(UTC)
        _row, created = add_deployment_owner(
            session,
            user_id=target.id,
            added_by_user_id=ctx.user_id,
            now=now,
        )
        if created:
            audit_admin(
                session,
                ctx=ctx,
                request=request,
                entity_kind="deployment_owner",
                entity_id=target.id,
                action="admin.owner_added",
                diff={"user_id": target.id},
            )
            session.flush()
            admin_sse.publish_admin_event(
                kind="admin.admins.updated",
                ctx=ctx,
                request=request,
                payload={"action": "owner_add", "user_id": target.id},
            )
        return OwnersGroupResponse(members=_owner_members(session))

    @router.post(
        "/admins/groups/owners/members/{user_id}/revoke",
        response_model=OwnersGroupResponse,
        operation_id="admin.admins.groups.owners.revoke",
        summary="Remove a user from the deployment owners group (owners-only)",
        status_code=status.HTTP_200_OK,
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "admins-owners-revoke",
                "summary": "Remove a user from the deployment owners group",
                "mutates": True,
            },
        },
    )
    def revoke_owner(
        user_id: str,
        ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        request: Request,
    ) -> OwnersGroupResponse:
        """Owners-only revoke from the deployment owners group."""
        ensure_deployment_owner(session, ctx=ctx)
        if user_id == ctx.user_id and deployment_owner_count(session) <= 1:
            raise _problem(
                _ERROR_LAST_OWNER,
                message="cannot remove the last deployment owner",
            )
        removed = remove_deployment_owner(session, user_id=user_id)
        if removed:
            audit_admin(
                session,
                ctx=ctx,
                request=request,
                entity_kind="deployment_owner",
                entity_id=user_id,
                action="admin.owner_removed",
                diff={"user_id": user_id},
            )
            session.flush()
            admin_sse.publish_admin_event(
                kind="admin.admins.updated",
                ctx=ctx,
                request=request,
                payload={"action": "owner_revoke", "user_id": user_id},
            )
        return OwnersGroupResponse(members=_owner_members(session))

    return router


# Re-exported for the test suite — pinning the literals here
# keeps the spec's typed-error vocabulary in one place so the
# route + the tests + the SPA stay in lockstep.
ERROR_MISSING_TARGET = _ERROR_MISSING_TARGET
ERROR_AMBIGUOUS_TARGET = _ERROR_AMBIGUOUS_TARGET
ERROR_USER_NOT_FOUND = _ERROR_USER_NOT_FOUND
ERROR_LAST_OWNER = _ERROR_LAST_OWNER
