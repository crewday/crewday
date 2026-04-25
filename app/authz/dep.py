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
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.authz.enforce import (
    InvalidScope,
    PermissionDenied,
    PermissionRuleRepository,
    UnknownActionKey,
    require,
)
from app.tenancy import WorkspaceContext

__all__ = ["Permission"]


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


def Permission(
    action_key: str,
    *,
    scope_kind: str,
    scope_id_from_path: str | None = None,
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
    * :class:`PermissionDenied` → 403 ``permission_denied``.
    * Missing path param → 500 ``scope_id_unresolved`` (caller wired
      the dep incorrectly).

    ``rule_repo`` is threaded through so an app factory (cd-ika7) can
    inject a SQL-backed repo process-wide. Unit tests usually leave
    it ``None`` so the built-in empty repo applies.
    """

    def _dep(
        request: Request,
        ctx: Annotated[WorkspaceContext, Depends(current_workspace_context)],
        session: Annotated[Session, Depends(db_session)],
    ) -> None:
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
                rule_repo=rule_repo,
            )
        except UnknownActionKey as exc:
            raise _misuse_to_http("unknown_action_key", action_key, str(exc)) from exc
        except InvalidScope as exc:
            raise _misuse_to_http("invalid_scope_kind", action_key, str(exc)) from exc
        except PermissionDenied as exc:
            raise _deny_to_http(action_key) from exc

    return _dep
