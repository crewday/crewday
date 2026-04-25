"""Permission-rules HTTP router — ``/permission_rules`` (spec §12).

Mounted inside the ``/w/<slug>/api/v1`` tree by the app factory. Every
route requires an active :class:`~app.tenancy.WorkspaceContext` and is
gated by the root-only ``permissions.edit_rules`` action (§05). v1
surface:

* ``GET /permission_rules`` — cursor-paginated list of every rule
  scoped to the caller's workspace, optionally filtered by
  ``scope_kind``, ``scope_id``, and ``action_key``.
* ``POST /permission_rules`` — create a new rule.
* ``DELETE /permission_rules/{id}`` — revoke a rule.

**v1 reality.** The ``permission_rule`` table is NOT in the v1 schema
yet (see :mod:`app.adapters.db.authz.models` docstring); the
:class:`~app.authz.PermissionRuleRepository` Protocol is plumbed
end-to-end so the resolver works against an empty repo
(:class:`~app.authz.EmptyPermissionRuleRepository`). This router
mirrors that reality:

* The GET endpoint always returns an empty page until the SQL
  adapter lands. The cursor pagination scaffolding is in place so
  the wire shape matches §12 verbatim.
* The POST and DELETE endpoints raise 503
  ``permission_rule_table_unavailable`` until the table ships. The
  action gate still fires first (root-only), so unauthorised callers
  see 403 rather than the 503; this preserves §15's posture
  ("authorise before signalling capacity issues").

When the SQL adapter ships (cd-dzp follow-up), the GET handler
delegates to the repo, and POST + DELETE call into the eventual
:mod:`app.domain.identity.permission_rules` service module. The wire
shape stays stable; only the body of the handlers changes.

See ``docs/specs/02-domain-model.md`` §"permission_rule",
``docs/specs/05-employees-and-roles.md`` §"Action catalog", and
``docs/specs/12-rest-api.md`` §"Users / work roles / settings".
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
)
from app.authz import Permission
from app.tenancy import WorkspaceContext

__all__ = [
    "PermissionRuleCreateRequest",
    "PermissionRuleListResponse",
    "PermissionRuleResponse",
    "build_permission_rules_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


# Mirror :data:`app.domain.identity._action_catalog.VALID_SCOPE_KINDS`.
ScopeKindLiteral = Literal["workspace", "property", "organization", "deployment"]
RuleEffectLiteral = Literal["allow", "deny"]
SubjectKindLiteral = Literal["user", "group"]


# ---------------------------------------------------------------------------
# Wire-facing shapes
# ---------------------------------------------------------------------------


class PermissionRuleCreateRequest(BaseModel):
    """Request body for ``POST /permission_rules``."""

    model_config = ConfigDict(extra="forbid")

    scope_kind: ScopeKindLiteral
    scope_id: str = Field(..., min_length=1, max_length=64)
    action_key: str = Field(..., min_length=1, max_length=128)
    subject_kind: SubjectKindLiteral
    subject_id: str = Field(..., min_length=1, max_length=64)
    effect: RuleEffectLiteral


class PermissionRuleResponse(BaseModel):
    """Response shape for permission-rule reads + writes."""

    id: str
    scope_kind: str
    scope_id: str
    action_key: str
    subject_kind: str
    subject_id: str
    effect: str
    created_at: datetime
    created_by_user_id: str | None
    revoked_at: datetime | None


class PermissionRuleListResponse(BaseModel):
    """Collection envelope for ``GET /permission_rules``."""

    data: list[PermissionRuleResponse]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


_ScopeKindFilter = Annotated[
    ScopeKindLiteral | None,
    Query(
        description=(
            "Narrow the listing to rules of the given scope kind. Mirrors §12 verbatim."
        ),
    ),
]
_ScopeIdFilter = Annotated[
    str | None,
    Query(
        max_length=64,
        description=("Narrow the listing to rules with a specific ``scope_id``."),
    ),
]
_ActionKeyFilter = Annotated[
    str | None,
    Query(
        max_length=128,
        description=("Narrow the listing to rules for a specific ``action_key``."),
    ),
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _http_for_table_unavailable() -> HTTPException:
    """503 stub used until the ``permission_rule`` table ships.

    The action gate (root-only) already cleared the caller; signalling
    capacity-unavailable on the *write* path is honest about the v1
    state. Reads are always honest-empty so a misconfigured admin UI
    can still render.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "permission_rule_table_unavailable",
            "message": (
                "permission_rule table not present in v1 schema; rule "
                "writes land with the cd-dzp follow-up that ships the "
                "table"
            ),
        },
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_permission_rules_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for permission-rule ops."""
    api = APIRouter(prefix="/permission_rules", tags=["identity", "permission_rules"])

    # ``permissions.edit_rules`` is root-only — the action gate keeps
    # rule reads + writes governance-sensitive (the resolver's
    # behaviour depends on rules, so even visibility is a §15
    # privilege). Mirroring the spec's pin: only owners.
    edit_gate = Depends(Permission("permissions.edit_rules", scope_kind="workspace"))

    @api.get(
        "",
        response_model=PermissionRuleListResponse,
        operation_id="permission_rules.list",
        summary="List permission rules in the caller's workspace",
        dependencies=[edit_gate],
        openapi_extra={
            "x-cli": {
                "group": "permission-rules",
                "verb": "list",
                "summary": "List permission rules",
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
        action_key: _ActionKeyFilter = None,
    ) -> PermissionRuleListResponse:
        """Return an empty page until the ``permission_rule`` table lands.

        The cursor scaffolding stays so a subsequent SQL adapter only
        has to swap the source of ``rules`` — neither the wire shape
        nor the query params change.
        """
        # ``decode_cursor`` is called for shape validation (an invalid
        # cursor 422s); the result is unused while the table is empty.
        _ = decode_cursor(cursor)
        # Suppress unused-variable lints — every filter is part of the
        # route surface; we keep the params so OpenAPI advertises them
        # accurately, even though there's nothing to filter yet.
        _ = (ctx, session, limit, scope_kind, scope_id, action_key)
        return PermissionRuleListResponse(data=[], next_cursor=None, has_more=False)

    @api.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=PermissionRuleResponse,
        operation_id="permission_rules.create",
        summary="Create a permission rule (root-only)",
        dependencies=[edit_gate],
        openapi_extra={
            "x-cli": {
                "group": "permission-rules",
                "verb": "create",
                "summary": "Create a permission rule",
                "mutates": True,
            },
        },
    )
    def create(
        body: PermissionRuleCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> PermissionRuleResponse:
        """Insert a new rule.

        Returns 503 ``permission_rule_table_unavailable`` until the
        backing table ships. The handler signature is final so the
        cd-dzp follow-up only swaps the body.
        """
        _ = (body, ctx, session)
        raise _http_for_table_unavailable()

    @api.delete(
        "/{rule_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="permission_rules.revoke",
        summary="Revoke a permission rule (root-only)",
        dependencies=[edit_gate],
        openapi_extra={
            "x-cli": {
                "group": "permission-rules",
                "verb": "revoke",
                "summary": "Revoke a permission rule",
                "mutates": True,
            },
        },
    )
    def delete(
        rule_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        """Revoke a rule.

        Returns 503 ``permission_rule_table_unavailable`` until the
        backing table ships.
        """
        _ = (rule_id, ctx, session)
        raise _http_for_table_unavailable()

    return api


# Module-level router for the v1 app factory's eager import.
router = build_permission_rules_router()
