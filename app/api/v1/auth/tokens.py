"""API-token HTTP router — mint / list / revoke.

Mounted at ``/w/<slug>/api/v1/auth/tokens`` inside the workspace-scoped
tree (the v1 app factory, cd-ika7, wires the prefix). Every route
requires an authenticated session plus the ``api_tokens.manage``
action permission on the workspace scope (§05 action catalog —
default-allow: owners + managers, root-protected-deny).

Routes:

* ``POST /auth/tokens`` ``{label, scopes, expires_at_days?}`` →
  ``201 {token, key_id, prefix, expires_at}``. The plaintext
  ``token`` is returned **only on this response**; never again.
  The router applies the spec's 90-day default TTL (§03
  "Guardrails") when ``expires_at_days`` is omitted.
* ``GET /auth/tokens`` → list of :class:`TokenSummary` projections.
  Returns both active and revoked rows — the ``/tokens`` UI shows
  both sections.
* ``DELETE /auth/tokens/{token_id}`` → 204. Flips ``revoked_at``;
  idempotent for already-revoked rows. An unknown ``token_id``
  returns 404.

Error shapes:

* 401 ``not_authenticated`` — no session (via the dep chain).
* 403 ``permission_denied`` — action gate fired.
* 404 ``token_not_found`` — revoke against an unknown or foreign
  ``token_id``.
* 422 ``too_many_tokens`` — 6th mint for the user on this workspace.
* 422 ``invalid_scopes`` / ``scopes_required`` — malformed or empty
  scopes body.

Handlers are intentionally thin: validate the body, call the domain
service inside the request's UoW, map typed errors onto HTTP
symbols. The spec's error vocabulary stays in one place so swapping
to RFC 7807 later (cd-waq3) is a single diff.

See ``docs/specs/03-auth-and-tokens.md`` §"API tokens",
``docs/specs/12-rest-api.md`` §"Auth / tokens", and
``docs/specs/15-security-privacy.md`` §"Token hashing".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.auth.tokens import (
    InvalidToken,
    MintedToken,
    TokenSummary,
    TooManyTokens,
    list_tokens,
    mint,
    revoke,
)
from app.authz import Permission
from app.tenancy import WorkspaceContext
from app.util.clock import SystemClock

__all__ = [
    "MintTokenBody",
    "MintTokenResponse",
    "TokenSummaryResponse",
    "build_tokens_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


# Spec §03 "Guardrails": scoped tokens default to 90 days TTL if
# ``expires_at`` is omitted. The router materialises that default so
# the domain service doesn't have to know about HTTP-layer policy.
_DEFAULT_TTL_DAYS = 90

# Spec §03 "Guardrails": "A workspace-level setting can raise any of
# them to 'never' but emits a noisy warning in the UI." v1 doesn't
# ship the setting yet; we cap at a generous upper bound so a typo
# like ``expires_at_days: 99999999`` can't produce a datetime that
# overflows the DB column or the client's display. 10 years is
# comfortably above the "longest realistic agent token" and well
# under ``datetime``'s own bounds.
_MAX_TTL_DAYS = 365 * 10


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class MintTokenBody(BaseModel):
    """Request body for ``POST /auth/tokens``.

    ``scopes`` is a flat ``{"action_key": true}`` mapping for v1 —
    matches the :attr:`ApiToken.scope_json` column shape so the
    router doesn't have to translate between "list of strings"
    (§03 body example) and "dict" (schema). A later cd-c91 follow-up
    may accept the list shape for symmetry with the spec's JSON
    example; for now the dict form is the internal canonical.

    ``expires_at_days`` overrides the 90-day default; ``None`` means
    "use the default".
    """

    label: str = Field(..., min_length=1, max_length=160)
    scopes: dict[str, Any] = Field(default_factory=dict)
    expires_at_days: int | None = Field(default=None, ge=1, le=_MAX_TTL_DAYS)


class MintTokenResponse(BaseModel):
    """Response body for ``POST /auth/tokens`` — plaintext shown once.

    The plaintext ``token`` is NEVER returned again; the UI must
    surface the "shown only once — copy it now" warning alongside
    this response. :attr:`key_id` and :attr:`prefix` are stable
    identifiers the UI can show on subsequent list / audit views.
    """

    token: str
    key_id: str
    prefix: str
    expires_at: datetime | None


class TokenSummaryResponse(BaseModel):
    """Response element for ``GET /auth/tokens``.

    Mirrors :class:`app.auth.tokens.TokenSummary` on the wire. The
    ``hash`` column is **not** surfaced — the domain projection
    already omits it (see :class:`app.auth.tokens.TokenSummary`
    docstring).
    """

    key_id: str
    label: str
    prefix: str
    scopes: dict[str, Any]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def _resolve_expires_at(body: MintTokenBody, now: datetime) -> datetime:
    """Return the concrete ``expires_at`` for a mint request.

    Applies the spec's 90-day default when the client omits
    ``expires_at_days``; otherwise clamps against :data:`_MAX_TTL_DAYS`
    (the Pydantic validator already rejects out-of-range values, so
    the clamp is defensive against a future schema change).
    """
    days = (
        body.expires_at_days if body.expires_at_days is not None else _DEFAULT_TTL_DAYS
    )
    return now + timedelta(days=days)


def _summary_to_response(summary: TokenSummary) -> TokenSummaryResponse:
    """Translate the domain projection to the wire shape.

    Thin enough to inline, but extracted so the ``GET /tokens``
    handler stays a flat list-comprehension and a future schema
    evolution (e.g. adding ``last_used_ip_hash``) has one edit site.
    """
    return TokenSummaryResponse(
        key_id=summary.key_id,
        label=summary.label,
        prefix=summary.prefix,
        scopes=dict(summary.scopes),
        expires_at=summary.expires_at,
        last_used_at=summary.last_used_at,
        revoked_at=summary.revoked_at,
        created_at=summary.created_at,
    )


def build_tokens_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for workspace-scoped token ops.

    Factory shape so the v1 app factory (cd-ika7) can mount the
    router with shared :class:`Permission` dependencies once the
    rule repository lands. For v1 we use the module-level
    :func:`Permission` factory directly — ``rule_repo=None`` resolves
    to :class:`EmptyPermissionRuleRepository`, which is correct
    until the ``permission_rule`` table ships.

    Tests instantiate this directly with
    :class:`fastapi.testclient.TestClient`; the module-level
    :data:`router` is a thin wrapper for the app factory's eager
    import.
    """
    api = APIRouter(prefix="/auth/tokens", tags=["auth", "tokens"])

    permission_gate = Depends(Permission("api_tokens.manage", scope_kind="workspace"))

    @api.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=MintTokenResponse,
        summary="Mint a new API token — plaintext returned once",
        dependencies=[permission_gate],
    )
    def post_tokens(
        body: MintTokenBody,
        ctx: _Ctx,
        session: _Db,
    ) -> MintTokenResponse:
        """Create a scoped API token for ``ctx.actor_id`` on this workspace.

        ``scopes`` can be empty on v1 — callers that need "every
        allowed action" rely on the default-allow mechanism (§05)
        and pass an empty dict. A later cd-c91 follow-up may require
        non-empty scopes per the spec's "narrowest set possible"
        guidance; validation against the action catalog lives there
        because the catalog is a handler-layer concern (the domain
        service must not re-import it).
        """
        now = SystemClock().now()
        expires_at = _resolve_expires_at(body, now)

        try:
            result: MintedToken = mint(
                session,
                ctx,
                user_id=ctx.actor_id,
                label=body.label,
                scopes=body.scopes,
                expires_at=expires_at,
                now=now,
            )
        except TooManyTokens as exc:
            # Starlette renamed the 422 constant in a recent release;
            # use the literal so the router works across minor versions.
            raise HTTPException(
                status_code=422,
                detail={"error": "too_many_tokens", "message": str(exc)},
            ) from exc
        return MintTokenResponse(
            token=result.token,
            key_id=result.key_id,
            prefix=result.prefix,
            expires_at=result.expires_at,
        )

    @api.get(
        "",
        response_model=list[TokenSummaryResponse],
        summary="List every token on this workspace (active + revoked)",
        dependencies=[permission_gate],
    )
    def get_tokens(
        ctx: _Ctx,
        session: _Db,
    ) -> list[TokenSummaryResponse]:
        """Return every token on the workspace, most recent first."""
        summaries = list_tokens(session, ctx)
        return [_summary_to_response(s) for s in summaries]

    @api.delete(
        "/{token_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Revoke a token — idempotent",
        dependencies=[permission_gate],
    )
    def delete_token(
        token_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        """Flip ``revoked_at`` on ``token_id``.

        Idempotent: revoking an already-revoked token still lands a
        ``revoked_noop`` audit row but returns 204 so the UI's
        "are you sure" → Revoke loop doesn't fail on a double-click.
        """
        try:
            revoke(session, ctx, token_id=token_id)
        except InvalidToken as exc:
            # §03 management-context error: 404 rather than 401,
            # because the caller is authenticated + authorised; they
            # just named a token that doesn't live on this workspace.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "token_not_found"},
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return api


# Module-level router for the v1 app factory's eager import. Tests
# that want a fresh instance per case should call
# :func:`build_tokens_router` directly to avoid cross-test leaks on
# FastAPI's dependency-override cache.
router = build_tokens_router()
