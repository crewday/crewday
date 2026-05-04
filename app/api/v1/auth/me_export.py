"""Identity-scoped privacy export endpoint.

Mounted bare-host at ``/api/v1/me/export`` (§15 "Privacy and data
rights"). The endpoint is identity-scoped, not workspace-scoped: a
person belongs to potentially several workspaces, and the export
covers their data across all of them. This mirrors the sibling
``/me/tokens`` / ``/me/avatar`` routers — the SPA hits the bare-host
URL once a session exists.

The route layer:

* Resolves the requester from the session cookie (no Bearer token
  path; PATs cannot mint privacy exports per §03 "Personal access
  tokens" — out of scope for v1, see cd-i1qe-me-tokens-fingerprint).
* Throttles the request to a sensible per-user budget so a runaway
  client can't pin the bundle builder.
* Delegates the bundle build to :func:`request_user_export`, wiring
  a notifier closure that constructs
  :class:`~app.domain.messaging.notifications.NotificationService`
  for the requester's primary workspace and dispatches the
  ``privacy_export_ready`` email through the standard channel
  (``email_delivery`` ledger + opt-out probe). §10 marks the kind
  "required" in the spec table, but :class:`NotificationService`
  does not yet enforce required-vs-opt-out at runtime — a user with
  a wildcard opt-out row would currently see the email skipped. That
  enforcement gap is broader than this surface and tracked
  separately.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.abuse.throttle import ShieldStore
from app.adapters.db.messaging.repositories import SqlAlchemyEmailDeliveryRepository
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.adapters.mail.ports import Mailer
from app.adapters.storage.ports import Storage
from app.api.deps import db_session, get_storage
from app.api.v1._problem_json import IDENTITY_PROBLEM_RESPONSES
from app.api.v1.auth.errors import auth_conflict, auth_not_found, auth_rate_limited
from app.api.v1.auth.me_tokens import _resolve_session_user
from app.domain.messaging.notifications import NotificationKind, NotificationService
from app.domain.privacy import (
    ExportReadyNotifier,
    get_user_export,
    request_user_export,
)
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import SystemClock
from app.util.ulid import new_ulid

__all__ = ["PrivacyExportResponse", "build_me_export_router"]


_Db = Annotated[Session, Depends(db_session)]
_Storage = Annotated[Storage, Depends(get_storage)]


# Per-user budget for privacy-export requests. Building the bundle is
# I/O-heavy (every "interesting" table is scanned + the redactor runs
# over every free-text leaf), so a runaway client must not be able to
# queue dozens of concurrent jobs. The 3/hour budget aligns with the
# §15 spec intent ("data subject access requests are bursty + rare")
# and with the ``5/min/IP`` magic-link send budget — slightly tighter
# because the export is heavier than a magic-link mint.
_EXPORT_PER_USER_LIMIT = 3
_EXPORT_PER_USER_WINDOW = timedelta(hours=1)


class PrivacyExportResponse(BaseModel):
    id: str
    status: str
    poll_url: str
    download_url: str | None = None
    expires_at: datetime | None = None


def _select_workspace_context(session: Session, *, user_id: str) -> WorkspaceContext:
    """Return a :class:`WorkspaceContext` pinned to the user's primary workspace.

    The privacy-export email goes through the standard
    :class:`NotificationService` path (§10.1), which is workspace-
    scoped: the inbox row, the ``email_delivery`` ledger row, and the
    opt-out probe all join on ``workspace_id``. The bare-host caller
    has no ambient context, so we resolve the user's first workspace
    membership and synthesise a context pinned to it. Picking the
    first row by id is deterministic + cheap; if the user belongs to
    no workspace the function raises :class:`LookupError` so the
    caller can surface a 4xx (an unaffiliated user requesting an
    export is a deployment bug, not a data path).
    """
    with tenant_agnostic():
        row = session.execute(
            select(UserWorkspace.workspace_id, Workspace.slug)
            .join(Workspace, Workspace.id == UserWorkspace.workspace_id)
            .where(UserWorkspace.user_id == user_id)
            .order_by(UserWorkspace.workspace_id)
            .limit(1)
        ).first()
    if row is None:
        raise LookupError(
            f"user_id={user_id!r} has no workspace membership; "
            "cannot resolve a delivery context for the privacy export."
        )
    workspace_id, slug = row
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=user_id,
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
        principal_kind="session",
    )


def _build_export_notifier(
    session: Session,
    *,
    mailer: Mailer,
) -> ExportReadyNotifier:
    """Return a callable that fans out the ``privacy_export_ready`` email.

    The closure binds the open ``session`` + injected ``mailer`` and,
    on each export-completion call, resolves the requester's primary
    workspace, constructs a :class:`NotificationService` against that
    context, and calls ``notify`` with the rendered context. The
    service writes the inbox row, fires the SSE event, persists the
    ``email_delivery`` ledger row (queued → sent / failed), and
    submits the body to the SMTP mock — the privacy export rides the
    standard rails.
    """

    def _notify(
        *,
        user_id: str,
        export_id: str,
        download_url: str | None,
        expires_at: datetime | None,
    ) -> None:
        ctx = _select_workspace_context(session, user_id=user_id)
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=mailer,
            email_deliveries=SqlAlchemyEmailDeliveryRepository(session),
        )
        service.notify(
            recipient_user_id=user_id,
            kind=NotificationKind.PRIVACY_EXPORT_READY,
            payload={
                "export_id": export_id,
                "download_url": download_url,
                "expires_at": expires_at.isoformat()
                if expires_at is not None
                else None,
            },
        )

    return _notify


def _consume_export_budget(
    store: ShieldStore, *, user_id: str, request: Request
) -> None:
    """Apply the per-user 3/hour budget; raise 429 on overflow.

    Per-user rather than per-IP because a single user behind NAT
    sharing IP with siblings is fine, while the same user spamming
    the endpoint from one browser is what the budget exists to
    refuse. The bucket key is the user id directly — no peppering
    needed because the user id is not externally guessable and the
    abuse model is "the authenticated user is misbehaving", not
    "an attacker is enumerating".
    """
    # ``request`` is unused inside the limiter today but kept in the
    # signature so a future per-IP variant or a recovery branch (e.g.
    # 429 audit row keyed on IP hash) does not need a sweeping diff.
    del request
    now = SystemClock().now()
    if not store.check_and_record(
        scope="me.export.request",
        key=user_id,
        limit=_EXPORT_PER_USER_LIMIT,
        window=_EXPORT_PER_USER_WINDOW,
        now=now,
    ):
        raise auth_rate_limited("rate_limited")


def build_me_export_router(
    *,
    mailer: Mailer | None = None,
    rate_limit_store: ShieldStore | None = None,
) -> APIRouter:
    """Return a fresh router for the privacy-export surface.

    ``mailer`` is required for the ``privacy_export_ready`` email to
    flow through the §10 delivery path. When ``None`` (e.g. tests
    that exercise only the bundle / storage / audit contract) the
    notifier is omitted: the route still queues the export and
    returns the download URL synchronously, but no email is sent.
    The factory wires a real mailer in production; SMTP-less
    deployments skip the surface entirely (mounted only when SMTP is
    configured) — matching the §10.2 direct-mail routers.

    ``rate_limit_store`` lets tests inject a per-case
    :class:`ShieldStore` so sibling cases never share state. The
    factory's process-wide store is the production default.
    """
    router = APIRouter(
        prefix="/me",
        tags=["identity", "me", "privacy"],
        responses=IDENTITY_PROBLEM_RESPONSES,
    )

    store = rate_limit_store if rate_limit_store is not None else ShieldStore()

    @router.post(
        "/export",
        response_model=PrivacyExportResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="me.export.request",
        summary="Request a privacy export for the current user",
        openapi_extra={
            "x-cli": {
                "group": "me",
                "verb": "export",
                "summary": "Request my privacy export",
            }
        },
    )
    def post_export(
        request: Request,
        session: _Db,
        storage: _Storage,
        crewday_session: Annotated[str | None, Cookie(alias="crewday_session")] = None,
        host_session: Annotated[
            str | None, Cookie(alias="__Host-crewday_session")
        ] = None,
    ) -> PrivacyExportResponse:
        user_id = _resolve_session_user(
            session,
            cookie_primary=host_session,
            cookie_dev=crewday_session,
        )
        _consume_export_budget(store, user_id=user_id, request=request)
        # Guard the no-workspace edge case BEFORE building the bundle.
        # The notifier closure resolves a delivery context lazily, but
        # by the time it fires the bundle is already in storage and
        # the audit row has been persisted — a LookupError there would
        # surface as a 500 with an orphan blob. Probing once up-front
        # turns the failure into a clean 409 with no side effects, and
        # is essentially free (the closure would re-run the same
        # SELECT moments later anyway).
        if mailer is not None:
            try:
                _select_workspace_context(session, user_id=user_id)
            except LookupError as exc:
                raise auth_conflict("no_workspace_membership") from exc
        notifier = _build_export_notifier(session, mailer=mailer) if mailer else None
        # Compute the poll-URL base from the inbound request URL so a
        # non-default mount prefix (factory tests, future reverse-proxy
        # rewrites) carries through to the response. ``request.url.path``
        # is the full path of the POST itself (``…/me/export``); the
        # GET route appends ``/{export_id}`` to that prefix, so the
        # base path is identical and we hand it to the domain helper
        # verbatim. Starlette's ``request.url_for`` cannot help here —
        # passing ``export_id=""`` trips its non-empty path-param
        # assertion, and there is no "render minus the trailing
        # placeholder" mode on the API.
        result = request_user_export(
            session,
            storage,
            user_id=user_id,
            poll_base_path=request.url.path.rstrip("/"),
            notifier=notifier,
        )
        return PrivacyExportResponse(**asdict(result))

    @router.get(
        "/export/{export_id}",
        response_model=PrivacyExportResponse,
        operation_id="me.export.get",
        summary="Poll a privacy export",
        openapi_extra={
            "x-cli": {
                "group": "me",
                "verb": "export-status",
                "summary": "Poll my privacy export",
            }
        },
    )
    def get_export(
        export_id: str,
        session: _Db,
        storage: _Storage,
        crewday_session: Annotated[str | None, Cookie(alias="crewday_session")] = None,
        host_session: Annotated[
            str | None, Cookie(alias="__Host-crewday_session")
        ] = None,
    ) -> PrivacyExportResponse:
        user_id = _resolve_session_user(
            session,
            cookie_primary=host_session,
            cookie_dev=crewday_session,
        )
        result = get_user_export(
            session,
            storage,
            user_id=user_id,
            export_id=export_id,
        )
        if result is None:
            raise auth_not_found("export_not_found")
        return PrivacyExportResponse(**asdict(result))

    return router
