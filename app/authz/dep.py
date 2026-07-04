"""FastAPI dependency factory for permission enforcement.

This module is the router-side wrapper around the pure-domain
:func:`app.authz.enforce.require`. It lives in its own module — and is
deliberately **not** re-exported from :mod:`app.authz`'s ``__init__`` —
so importing :mod:`app.authz` from a ``app.domain`` module does not
transitively pull in :mod:`app.api.deps` (the FastAPI plumbing
``current_workspace_context`` / ``db_session`` deps live there).

That separation is what keeps the import-linter contract
"Domain forbids handlers (api/web/cli/worker)" honest: domain services
get the pure :func:`require` via ``from app.authz import require``;
routers get the FastAPI dep via ``from app.authz.dep import Permission``.

Public surface:

* :func:`Permission` — the FastAPI dependency factory used by every
  protected v1 router.

See ``docs/specs/02-domain-model.md`` §"Permission resolution" for the
underlying rule semantics.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.authz.approval_mint import mint_and_envelope_for_http
from app.authz.enforce import (
    ApprovalRequired,
    InsufficientScope,
    InvalidScope,
    PermissionDenied,
    PermissionRuleRepository,
    UnknownActionKey,
    require,
    require_me_scope,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "MeScope",
    "Permission",
    "PermissionDependencyMetadata",
    "enforce_workspace_permission",
]


@dataclass(frozen=True, slots=True)
class PermissionDependencyMetadata:
    """Authz gate metadata attached to router dependency callables.

    FastAPI keeps dependency callables on the route graph after
    registration. The embedded-agent dispatcher reads this metadata to
    decide which OpenAPI tools are worth advertising for the current
    delegating user; the dependency itself remains the runtime
    enforcement layer.
    """

    action_key: str
    scope_kind: str
    scope_id_from_path: str | None


def _deny_to_http(action_key: str) -> HTTPException:
    """Map a domain :class:`PermissionDenied` into the HTTP 403 shape.

    Kept in one place so the router-facing error body stays
    consistent: every denied check returns the same
    ``{"error": "permission_denied", "action_key": "<key>"}`` detail.
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": "permission_denied", "action_key": action_key},
    )


def _insufficient_scope_to_http(exc: InsufficientScope) -> HTTPException:
    """Map a scoped-token :class:`InsufficientScope` into the 403 shape.

    §03 "Usage" pins ``403`` with a ``WWW-Authenticate: error=
    "insufficient_scope" scope="<scope>"`` header so the agent learns
    exactly which scope to request. ``scope`` is omitted for
    deny-by-default actions (no mapped scope) — the header then carries
    only the ``error`` token, still RFC 6750-valid. The body mirrors the
    §12 envelope with an ``insufficient_scope`` error code, distinct
    from the role-miss ``permission_denied``.
    """
    challenge = 'error="insufficient_scope"'
    detail: dict[str, str] = {
        "error": "insufficient_scope",
        "action_key": exc.action_key,
    }
    if exc.required_scope is not None:
        challenge = f'{challenge} scope="{exc.required_scope}"'
        detail["scope"] = exc.required_scope
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=detail,
        headers={"WWW-Authenticate": challenge},
    )


def _misuse_to_http(error: str, action_key: str, detail: str) -> HTTPException:
    """Map a caller bug (unknown action / invalid scope) into HTTP 422.

    The detail shape matches §12's error envelope: one ``error`` code
    the client can switch on, plus human-readable context.
    """
    # Starlette / FastAPI renamed the 422 constant in 2024; the integer
    # literal keeps the call stable across versions without chasing
    # the deprecation warning.
    return HTTPException(
        status_code=422,
        detail={"error": error, "action_key": action_key, "message": detail},
    )


def _attach_permission_metadata(
    dep: Callable[..., None],
    metadata: PermissionDependencyMetadata,
) -> None:
    vars(dep)["__crewday_permission__"] = metadata


def enforce_workspace_permission(
    session: Session,
    ctx: WorkspaceContext,
    *,
    action_key: str,
    required_scope: str | None = None,
    rule_repo: PermissionRuleRepository | None = None,
) -> None:
    """Imperatively enforce a workspace-scoped capability, mapping to HTTP.

    The :func:`Permission` dependency gates a route *before* its body
    runs, which cannot express "load the resource, then gate on a
    property of the row". Handlers that must decide after loading a row
    (the approvals desk gates ``approvals.read`` only for desk-only
    rows — see :mod:`app.api.v1.approvals`) call this instead. It raises
    the exact same :class:`HTTPException` envelopes as
    :func:`Permission` for the workspace-scope case, reusing the shared
    mappers so the error body stays single-sourced.

    ``ApprovalRequired`` is intentionally not handled — this seam is for
    read-family capabilities that never carry ``requires_approval``; a
    caller passing a gated ``action_key`` would let it propagate to the
    generic 500 handler, which is the correct "wired the wrong action"
    signal.
    """
    try:
        require(
            session,
            ctx,
            action_key=action_key,
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
            required_scope=required_scope,
            rule_repo=rule_repo,
        )
    except UnknownActionKey as exc:
        raise _misuse_to_http("unknown_action_key", action_key, str(exc)) from exc
    except InvalidScope as exc:
        raise _misuse_to_http("invalid_scope_kind", action_key, str(exc)) from exc
    except InsufficientScope as exc:
        raise _insufficient_scope_to_http(exc) from exc
    except PermissionDenied as exc:
        raise _deny_to_http(action_key) from exc


def MeScope(required_scope: str) -> Callable[..., None]:
    """Build a FastAPI dependency enforcing a ``me.*`` scope (§03 "Scopes").

    Router-side wrapper around :func:`app.authz.enforce.require_me_scope`,
    the ``me.*`` sibling of :func:`Permission`. Used only by the
    bearer-authenticated ``/w/<slug>/api/v1/me/...`` self-service routes,
    which carry a per-route ``me.*`` requirement rather than a §05 action
    key. A personal access token must hold ``required_scope`` (with the
    ``me.<r>:read`` ⊂ ``me.<r>:write`` implication); sessions, demo,
    system, and delegated tokens fall through unchecked — their only
    confinement is the route's own ``ctx.actor_id`` self-keying.

    :class:`InsufficientScope` is mapped to the same ``403``
    ``insufficient_scope`` + ``WWW-Authenticate: error="insufficient_scope"
    scope="me.<r>:<verb>"`` envelope that a scoped-token miss yields
    (:func:`_insufficient_scope_to_http`), so an agent learns exactly
    which ``me.*`` scope to request.
    """

    def _dep(
        ctx: Annotated[WorkspaceContext, Depends(current_workspace_context)],
    ) -> None:
        try:
            require_me_scope(ctx, required_scope=required_scope)
        except InsufficientScope as exc:
            raise _insufficient_scope_to_http(exc) from exc

    return _dep


def Permission(
    action_key: str,
    *,
    scope_kind: str,
    scope_id_from_path: str | None = None,
    required_scope: str | None = None,
    rule_repo: PermissionRuleRepository | None = None,
) -> Callable[..., None]:
    """Build a FastAPI dependency that enforces ``action_key``.

    Two wiring patterns — the caller picks at ``Depends()`` time:

    * **Workspace-scoped** — ``Permission("scope.view",
      scope_kind="workspace")``. The dep resolves ``scope_id`` from
      ``ctx.workspace_id`` automatically.
    * **Property-scoped** — ``Permission("tasks.create",
      scope_kind="property", scope_id_from_path="property_id")``. The
      dep reads ``request.path_params["property_id"]`` to get the
      target. The ancestor workspace comes from the ctx as usual.
      Organization-scope or deployment-scope endpoints pass the
      corresponding path-param name.

    The returned callable is the dependency; :class:`Depends` wires
    it into the route. Errors flow through :class:`HTTPException`:

    * :class:`UnknownActionKey` → 422 ``unknown_action_key``.
    * :class:`InvalidScope` → 422 ``invalid_scope_kind``.
    * :class:`InsufficientScope` → 403 ``insufficient_scope`` with a
      ``WWW-Authenticate: error="insufficient_scope"`` header (§03
      "Usage"); only scoped API tokens can trip it.
    * :class:`PermissionDenied` → 403 ``permission_denied``.
    * :class:`ApprovalRequired` → 409 ``approval_required`` (mints
      one ``approval_request`` row and commits before returning).
    * Missing path param → 500 ``scope_id_unresolved`` (caller wired
      the dep incorrectly).

    ``required_scope`` is the resource scope a scoped API token must
    hold for this route (cd-821v1). The resource-agnostic generic gates
    ``scope.view`` / ``scope.edit_settings`` (§05) can't map their action
    key to one §03 scope, so a route reading properties passes
    ``required_scope="properties:read"`` and one editing inventory
    settings passes ``required_scope="inventory:write"``. It steers the
    scoped-token gate only — the role walk is unchanged, and leaving it
    ``None`` keeps the action→scope map's deny-by-default for families
    the §03 taxonomy doesn't name (assets, billing, permissions, …).

    ``rule_repo`` is threaded through so an app factory (cd-ika7) can
    inject a SQL-backed repo process-wide. Unit tests usually leave
    it ``None`` so the built-in empty repo applies.
    """

    def _dep(
        request: Request,
        ctx: Annotated[WorkspaceContext, Depends(current_workspace_context)],
        session: Annotated[Session, Depends(db_session)],
    ) -> None:
        # code-health: ignore[nloc] Policy txn keeps auth, validation, state, and events together.  # noqa: E501
        if scope_id_from_path is None:
            # Default: workspace-scope gate. Non-workspace scope_kinds
            # without a path-param source are a wiring bug — fall
            # back to ctx.workspace_id for ``workspace`` only.
            if scope_kind == "workspace":
                scope_id = ctx.workspace_id
            else:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "error": "scope_id_unresolved",
                        "message": (
                            f"Permission({action_key!r}) has scope_kind="
                            f"{scope_kind!r} but no scope_id_from_path set"
                        ),
                    },
                )
        else:
            raw = request.path_params.get(scope_id_from_path)
            if raw is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "error": "scope_id_unresolved",
                        "message": (
                            f"Permission({action_key!r}) expected path-param "
                            f"{scope_id_from_path!r} but none was provided"
                        ),
                    },
                )
            # ``path_params`` values arrive as strings from the
            # Starlette router; narrow explicitly to keep mypy happy.
            if not isinstance(raw, str):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "error": "scope_id_unresolved",
                        "message": (
                            f"Permission({action_key!r}) path-param "
                            f"{scope_id_from_path!r} is not a string"
                        ),
                    },
                )
            scope_id = raw

        try:
            require(
                session,
                ctx,
                action_key=action_key,
                scope_kind=scope_kind,
                scope_id=scope_id,
                required_scope=required_scope,
                rule_repo=rule_repo,
            )
        except UnknownActionKey as exc:
            raise _misuse_to_http("unknown_action_key", action_key, str(exc)) from exc
        except InvalidScope as exc:
            raise _misuse_to_http("invalid_scope_kind", action_key, str(exc)) from exc
        except InsufficientScope as exc:
            raise _insufficient_scope_to_http(exc) from exc
        except PermissionDenied as exc:
            raise _deny_to_http(action_key) from exc
        except ApprovalRequired as exc:
            # The action was allowed but the catalog flags it
            # ``requires_approval=True`` — mint the HITL row and
            # surface the §12 409 envelope. ``PermissionDenied`` is
            # mutually exclusive with this branch (the resolver
            # raises one or the other, never both).
            raise mint_and_envelope_for_http(request, session, ctx, exc) from exc

    _attach_permission_metadata(
        _dep,
        PermissionDependencyMetadata(
            action_key=action_key,
            scope_kind=scope_kind,
            scope_id_from_path=scope_id_from_path,
        ),
    )
    return _dep
