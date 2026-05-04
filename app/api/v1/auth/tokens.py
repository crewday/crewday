"""API-token HTTP router — mint / list / revoke.

Mounted at ``/w/<slug>/api/v1/auth/tokens`` inside the workspace-scoped
tree (the v1 app factory, cd-ika7, wires the prefix). Every route
requires an authenticated session plus the ``api_tokens.manage``
action permission on the workspace scope (§05 action catalog —
default-allow: owners + managers, root-protected-deny).

This router handles the **workspace-pinned** token kinds: the
original ``scoped`` tokens (cd-c91) and the ``delegated`` tokens
(cd-i1qe). Personal access tokens are identity-scoped and live at
the bare host on ``/api/v1/me/tokens`` — see
:mod:`app.api.v1.auth.me_tokens`.

Routes:

* ``POST /auth/tokens`` → ``201 {token, key_id, prefix, expires_at,
  kind}``. Two shapes:

  * **Scoped** (default): ``{label, scopes, expires_at_days?}``.
    ``scopes`` is the flat ``{"action_key": true}`` shape §03 pins;
    default TTL 90 days (§03 "Guardrails"). An empty dict is accepted
    for compatibility with existing scoped-token callers, and unknown
    scope keys are stored as supplied rather than catalog-validated at
    this API layer.
  * **Delegated**: ``{label, delegate: true, expires_at_days?,
    scopes: {}}``. The session user's id populates
    ``delegate_for_user_id``; scopes MUST be empty (§03 "Delegated
    tokens"); default TTL 30 days.

  The plaintext ``token`` is returned **only on this response**;
  never again.

* ``GET /auth/tokens`` → list of :class:`TokenSummary` projections.
  Returns both active and revoked scoped / delegated rows — the
  ``/tokens`` UI shows both sections. Personal tokens are excluded
  per §03.
* ``DELETE /auth/tokens/{token_id}`` → 204. Flips ``revoked_at``;
  idempotent for already-revoked rows. An unknown / foreign /
  personal ``token_id`` returns 404 (same shape — we don't leak
  whose tokens exist).
* ``POST /auth/tokens/{token_id}/revoke`` → 204. Alias of the
  ``DELETE`` shape above for clients (CSRF-tolerant fetchers, the
  /tokens SPA, integration scripts that prefer POST verbs). Same
  idempotency contract.
* ``POST /auth/tokens/{token_id}/rotate`` → ``200 {token, key_id,
  prefix, expires_at, kind}``. §03 "Revocation and rotation":
  rotates the secret in place, leaving ``key_id`` / ``label`` /
  ``scopes`` / ``expires_at`` untouched. The old secret remains valid
  for the 1h ``previous_hash`` overlap. PAT / revoked / expired /
  cross-workspace ids collapse to 404.
* ``GET /auth/tokens/{token_id}/audit`` → list of
  :class:`TokenAuditEntryResponse` — the per-token lifecycle trail
  (mint / rotate / revoke / revoked_noop), newest first. The
  per-request log surface (method / path / IP / user_agent)
  belongs to a sibling ``api_token_request_log`` table tracked
  under cd-ocdg7.

Error shapes:

* 401 ``not_authenticated`` — no session (via the dep chain).
* 403 ``permission_denied`` — action gate fired.
* 404 ``token_not_found`` — revoke against an unknown, foreign, or
  personal ``token_id``.
* 422 ``too_many_tokens`` — 6th scoped/delegated mint for the user
  on this workspace.
* 422 ``too_many_workspace_tokens`` — 51st live scoped/delegated mint
  on this workspace.
* 422 ``delegated_requires_empty_scopes`` — delegated mint with a
  non-empty ``scopes`` body.
* 422 ``delegated_requires_session`` — delegated mint attempted by
  a non-session caller (token-presented or system). §03 "Delegated
  tokens" + §11 "no transitive delegation": a delegated token can
  only be created by a passkey session, so a token-presented mint
  request is refused at the seam.
* 422 ``me_scope_conflict`` — scoped mint with a ``me:*`` key in
  ``scopes``.

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

from app.adapters.db.identity.models import ApiToken
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.api.v1._problem_json import IDENTITY_PROBLEM_RESPONSES
from app.auth.tokens import (
    DELEGATED_DEFAULT_TTL_DAYS,
    SCOPED_DEFAULT_TTL_DAYS,
    InvalidToken,
    MintedToken,
    TokenKind,
    TokenShapeError,
    TokenSummary,
    TooManyTokens,
    TooManyWorkspaceTokens,
    list_audit,
    list_tokens,
    mint,
    revoke,
    rotate,
)
from app.authz.dep import Permission
from app.events.bus import bus as default_event_bus
from app.events.types import ApiTokenCreated, ApiTokenRevoked, ApiTokenRotated
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import SystemClock

__all__ = [
    "MintTokenBody",
    "MintTokenResponse",
    "TokenAuditEntryResponse",
    "TokenListResponse",
    "TokenSummaryResponse",
    "build_tokens_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


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

    ``scopes`` is a flat ``{"action_key": true}`` mapping for v1. It
    matches the :attr:`ApiToken.scope_json` column shape, so the router
    stores it as supplied: empty dicts are valid for scoped tokens and
    unknown workspace scope keys are not catalog-validated here.

    ``delegate`` (§03 "Delegated tokens") — when ``true``, the row
    is minted as a delegated token acting for the session user
    (``delegate_for_user_id``); ``scopes`` MUST be empty because
    authority resolves against the delegating user's grants. When
    ``false`` (default), the row is a classic scoped token.

    ``expires_at_days`` overrides the per-kind default (90 days for
    scoped, 30 days for delegated); ``None`` means "use the default".
    """

    label: str = Field(..., min_length=1, max_length=160)
    scopes: dict[str, Any] = Field(default_factory=dict)
    expires_at_days: int | None = Field(default=None, ge=1, le=_MAX_TTL_DAYS)
    delegate: bool = Field(
        default=False,
        description=(
            "When true, mint a delegated token whose authority inherits "
            "the session user's role_grants (§03). Scopes must be empty."
        ),
    )


class MintTokenResponse(BaseModel):
    """Response body for ``POST /auth/tokens`` — plaintext shown once.

    The plaintext ``token`` is NEVER returned again; the UI must
    surface the "shown only once — copy it now" warning alongside
    this response. :attr:`key_id` and :attr:`prefix` are stable
    identifiers the UI can show on subsequent list / audit views.
    ``kind`` echoes the domain discriminator so the ``/tokens`` UI
    can render the right "Copy this once" chrome without a follow-up
    fetch.
    """

    token: str
    key_id: str
    prefix: str
    expires_at: datetime | None
    kind: TokenKind


class TokenSummaryResponse(BaseModel):
    """Response element for ``GET /auth/tokens``.

    Mirrors :class:`app.auth.tokens.TokenSummary` on the wire. The
    ``hash`` column is **not** surfaced — the domain projection
    already omits it (see :class:`app.auth.tokens.TokenSummary`
    docstring). ``kind`` and ``delegate_for_user_id`` surface the
    cd-i1qe discriminator so the UI can flag delegated rows.
    """

    key_id: str
    label: str
    prefix: str
    scopes: dict[str, Any]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    kind: TokenKind
    delegate_for_user_id: str | None


class TokenListResponse(BaseModel):
    """Collection envelope for ``GET /auth/tokens``.

    Matches §12 "Pagination" verbatim — ``{data, next_cursor,
    has_more}``. Mirrors the shape used by every other paginated
    list router on the workspace surface (e.g. ``WorkRoleListResponse``)
    so the SPA + CLI surfaces have one envelope to special-case.
    """

    data: list[TokenSummaryResponse]
    next_cursor: str | None = None
    has_more: bool = False


class TokenAuditEntryResponse(BaseModel):
    """Response element for ``GET /auth/tokens/{token_id}/audit``.

    Mirrors :class:`app.auth.tokens.TokenAuditEntry`. The v1 surface
    is the lifecycle trail (``api_token.minted`` /
    ``api_token.rotated`` / ``api_token.revoked`` /
    ``api_token.revoked_noop``); a future per-request log will
    extend this shape rather than replace it.
    """

    at: datetime
    action: str
    actor_id: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def _resolve_expires_at(body: MintTokenBody, now: datetime) -> datetime:
    """Return the concrete ``expires_at`` for a mint request.

    Applies the spec's per-kind default when the client omits
    ``expires_at_days`` (30 days for delegated, 90 days for scoped);
    otherwise clamps against :data:`_MAX_TTL_DAYS` (the Pydantic
    validator already rejects out-of-range values, so the clamp is
    defensive against a future schema change).
    """
    if body.expires_at_days is not None:
        days = body.expires_at_days
    elif body.delegate:
        days = DELEGATED_DEFAULT_TTL_DAYS
    else:
        days = SCOPED_DEFAULT_TTL_DAYS
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
        kind=summary.kind,
        delegate_for_user_id=summary.delegate_for_user_id,
    )


def _publish_api_token_event(
    ctx: WorkspaceContext,
    event_type: type[ApiTokenCreated] | type[ApiTokenRevoked] | type[ApiTokenRotated],
    token_id: str,
    *,
    kind: TokenKind | None = None,
) -> None:
    payload = {
        "workspace_id": ctx.workspace_id,
        "actor_id": ctx.actor_id,
        "correlation_id": ctx.audit_correlation_id,
        "occurred_at": SystemClock().now(),
        "id": token_id,
    }
    if event_type is ApiTokenCreated:
        if kind is None:
            raise ValueError("api_token.created requires token kind")
        default_event_bus.publish(ApiTokenCreated(kind=kind, **payload))
        return
    default_event_bus.publish(event_type(**payload))


def _is_live_workspace_token(
    session: Session,
    ctx: WorkspaceContext,
    token_id: str,
) -> bool:
    with tenant_agnostic():
        row = session.get(ApiToken, token_id)
    return (
        row is not None
        and row.kind != "personal"
        and row.workspace_id == ctx.workspace_id
        and row.revoked_at is None
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
    # Tags: ``identity`` surfaces every identity-adjacent operation
    # under one OpenAPI section (spec §01 context map + §12 Auth);
    # ``auth`` + ``tokens`` are kept for back-compat with existing
    # clients that filter on the finer-grained tags.
    api = APIRouter(
        prefix="/auth/tokens",
        tags=["identity", "auth", "tokens"],
        responses=IDENTITY_PROBLEM_RESPONSES,
    )

    permission_gate = Depends(Permission("api_tokens.manage", scope_kind="workspace"))

    @api.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=MintTokenResponse,
        operation_id="tokens.mint",
        summary="Mint a new API token — plaintext returned once",
        dependencies=[permission_gate],
        openapi_extra={
            # Workspace-scoped token mint — the canonical
            # ``crewday tokens create`` verb in spec §13. The
            # ``x-cli.group`` is pinned here so the heuristic's bare
            # ``tokens`` group cannot collide with the bare-host
            # ``/me/tokens`` surface (which surfaces as ``me-tokens``;
            # see ``app/api/v1/auth/me_tokens.py``).
            "x-cli": {
                "group": "tokens",
                "verb": "create",
                "summary": "Mint a workspace API token — plaintext shown once",
                "mutates": True,
            },
        },
    )
    def post_tokens(
        body: MintTokenBody,
        ctx: _Ctx,
        session: _Db,
    ) -> MintTokenResponse:
        """Create a scoped or delegated API token on this workspace.

        Branches on :attr:`MintTokenBody.delegate`:

        * ``delegate=false`` (default) — scoped token. ``scopes`` is
          the flat ``{"action_key": true}`` dict. Empty is allowed on
          v1; a cd-c91 follow-up may require non-empty scopes per
          the spec's "narrowest set possible" guidance.
        * ``delegate=true`` — delegated token acting for
          ``ctx.actor_id``. ``scopes`` MUST be empty (enforced at
          the domain layer and surfaced as 422
          ``delegated_requires_empty_scopes``).

        Per-kind shape errors from the domain service collapse into
        one 422 envelope whose ``error`` code varies by clause —
        that way the spec's error taxonomy lives in one place and
        the SPA's form-level messaging keys off the stable codes.
        """
        now = SystemClock().now()
        expires_at = _resolve_expires_at(body, now)

        kind: TokenKind = "delegated" if body.delegate else "scoped"

        # §03 "Delegated tokens" + §11 "no transitive delegation": a
        # delegated token can only be minted by a passkey session.
        # Reject every non-session caller (a bearer-token-presented
        # request or a system-driven helper) at the route seam — the
        # domain layer can't tell them apart from a real session caller
        # without the transport bit on ``ctx``. The 422 envelope mirrors
        # the rest of the mint-shape error taxonomy so the SPA's form
        # error rendering keys off a stable ``error`` code.
        if kind == "delegated" and ctx.principal_kind != "session":
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "delegated_requires_session",
                    "message": (
                        "delegated tokens can only be minted from a "
                        "passkey session — no transitive delegation"
                    ),
                },
            )

        try:
            result: MintedToken = mint(
                session,
                ctx,
                user_id=ctx.actor_id,
                label=body.label,
                scopes=body.scopes,
                expires_at=expires_at,
                kind=kind,
                delegate_for_user_id=(ctx.actor_id if kind == "delegated" else None),
                now=now,
            )
        except TooManyTokens as exc:
            # Starlette renamed the 422 constant in a recent release;
            # use the literal so the router works across minor versions.
            raise HTTPException(
                status_code=422,
                detail={"error": "too_many_tokens", "message": str(exc)},
            ) from exc
        except TooManyWorkspaceTokens as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "too_many_workspace_tokens", "message": str(exc)},
            ) from exc
        except TokenShapeError as exc:
            # Shape errors map to the spec's error codes:
            # * delegated + non-empty scopes → ``delegated_requires_empty_scopes``
            # * scoped + me:* key → ``me_scope_conflict``
            # The domain layer raises a single error type with a
            # human message; we branch here by inspecting the
            # request shape rather than reparsing the message.
            if kind == "delegated" and body.scopes:
                code = "delegated_requires_empty_scopes"
            elif kind == "scoped" and any(k.startswith("me.") for k in body.scopes):
                code = "me_scope_conflict"
            else:
                code = "invalid_token_shape"
            raise HTTPException(
                status_code=422,
                detail={"error": code, "message": str(exc)},
            ) from exc
        _publish_api_token_event(
            ctx,
            ApiTokenCreated,
            result.key_id,
            kind=result.kind,
        )
        return MintTokenResponse(
            token=result.token,
            key_id=result.key_id,
            prefix=result.prefix,
            expires_at=result.expires_at,
            kind=result.kind,
        )

    @api.get(
        "",
        response_model=TokenListResponse,
        operation_id="tokens.list",
        summary="List every token on this workspace (active + revoked)",
        dependencies=[permission_gate],
        openapi_extra={
            "x-cli": {
                "group": "tokens",
                "verb": "list",
                "summary": "List workspace API tokens (active + revoked)",
                "mutates": False,
            },
        },
    )
    def get_tokens(
        ctx: _Ctx,
        session: _Db,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> TokenListResponse:
        """Return a cursor-paginated page of workspace tokens.

        Most recent first (``id DESC`` — ULIDs are time-ordered, so the
        natural id sort matches the spec's "most recent first" wording
        without needing a composite cursor key).

        Cursor semantics: ``cursor`` is the opaque ``next_cursor`` from
        the previous page; omit it on the first call. ``limit`` defaults
        to :data:`~app.api.pagination.DEFAULT_LIMIT` and is bounded to
        ``[1, 500]`` per §12 "Pagination". A malformed / tampered cursor
        raises :class:`~app.domain.errors.InvalidCursor`, which the
        problem+json seam returns as 422 ``invalid_cursor``.
        """
        after_id = decode_cursor(cursor)
        # Service returns up to ``limit + 1`` rows so :func:`paginate`
        # can decide ``has_more`` without a second query.
        summaries = list_tokens(
            session,
            ctx,
            limit=limit,
            after_id=after_id,
        )
        page = paginate(
            summaries,
            limit=limit,
            key_getter=lambda s: s.key_id,
        )
        return TokenListResponse(
            data=[_summary_to_response(s) for s in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @api.delete(
        "/{token_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="tokens.revoke",
        summary="Revoke a token — idempotent",
        dependencies=[permission_gate],
        openapi_extra={
            "x-cli": {
                "group": "tokens",
                "verb": "revoke",
                "summary": "Revoke a workspace API token",
                "mutates": True,
            },
        },
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
        should_publish = _is_live_workspace_token(session, ctx, token_id)
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
        if should_publish:
            _publish_api_token_event(ctx, ApiTokenRevoked, token_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post(
        "/{token_id}/revoke",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="auth.tokens.revoke_post",
        summary="Revoke a token via POST — alias of DELETE",
        dependencies=[permission_gate],
        openapi_extra={
            # Same x-cli verb as the DELETE form — the SPA prefers
            # POST because some browsers / proxies strip request
            # bodies on DELETE. Both paths share the same idempotency
            # contract; CLI consumers should still prefer the DELETE
            # form for consistency with REST conventions.
            "x-cli": {
                "group": "tokens",
                "verb": "revoke",
                "summary": "Revoke a workspace API token (POST alias)",
                "mutates": True,
            },
        },
    )
    def post_revoke_token(
        token_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        """POST alias for :func:`delete_token`.

        The /tokens SPA (cd-htab) and the mock router consume the
        POST shape. Same idempotent contract, same 404 collapse on
        unknown / cross-workspace / personal token ids.
        """
        should_publish = _is_live_workspace_token(session, ctx, token_id)
        try:
            revoke(session, ctx, token_id=token_id)
        except InvalidToken as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "token_not_found"},
            ) from exc
        if should_publish:
            _publish_api_token_event(ctx, ApiTokenRevoked, token_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post(
        "/{token_id}/rotate",
        response_model=MintTokenResponse,
        operation_id="auth.tokens.rotate",
        summary="Rotate a token's secret in place — plaintext returned once",
        dependencies=[permission_gate],
        openapi_extra={
            "x-cli": {
                "group": "tokens",
                "verb": "rotate",
                "summary": "Rotate a workspace API token's secret",
                "mutates": True,
            },
        },
    )
    def post_rotate_token(
        token_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> MintTokenResponse:
        """Rotate ``token_id``'s secret in place.

        Same row, same ``key_id`` / ``label`` / ``scopes`` /
        ``expires_at`` — only the secret + prefix change. The new
        plaintext is returned exactly once on this response, the
        same one-shot contract as ``POST /auth/tokens``.

        404 ``token_not_found`` collapses unknown / cross-workspace
        / personal / revoked / expired ids — the API doesn't leak
        which mode fired.
        """
        try:
            result = rotate(session, ctx, token_id=token_id)
        except InvalidToken as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "token_not_found"},
            ) from exc
        _publish_api_token_event(ctx, ApiTokenRotated, token_id)
        return MintTokenResponse(
            token=result.token,
            key_id=result.key_id,
            prefix=result.prefix,
            expires_at=result.expires_at,
            kind=result.kind,
        )

    @api.get(
        "/{token_id}/audit",
        response_model=list[TokenAuditEntryResponse],
        operation_id="auth.tokens.audit",
        summary="Per-token audit timeline — newest first",
        dependencies=[permission_gate],
        openapi_extra={
            "x-cli": {
                "group": "tokens",
                "verb": "audit",
                "summary": "Show a workspace API token's audit timeline",
                "mutates": False,
            },
        },
    )
    def get_token_audit(
        token_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> list[TokenAuditEntryResponse]:
        """Return the workspace audit_log rows tied to ``token_id``.

        The list scope is the caller's workspace — a manager on
        workspace A cannot read another workspace's token audit
        rows. v1 surfaces the lifecycle events
        (``api_token.minted`` / ``rotated`` / ``revoked`` /
        ``revoked_noop``); a sibling per-request log lands as a
        follow-up so the manager has *some* trail today rather
        than none.
        """
        entries = list_audit(session, ctx, token_id=token_id)
        return [
            TokenAuditEntryResponse(
                at=e.at,
                action=e.action,
                actor_id=e.actor_id,
                correlation_id=e.correlation_id,
            )
            for e in entries
        ]

    return api


# Module-level router for the v1 app factory's eager import. Tests
# that want a fresh instance per case should call
# :func:`build_tokens_router` directly to avoid cross-test leaks on
# FastAPI's dependency-override cache.
router = build_tokens_router()
