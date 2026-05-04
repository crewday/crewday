"""Deployment-admin audit feed routes.

Mounts under ``/admin/api/v1`` (§12 "Admin surface"):

* ``GET /audit`` — page through ``audit_log`` rows whose
  ``scope_kind = 'deployment'``. Filters mirror the workspace
  audit-feed contract (cd-b0au): ``actor`` / ``action`` /
  ``entity_kind`` / ``entity_id`` / ``since`` / ``until``.
* ``GET /audit/tail?follow=0|1`` — NDJSON projection of the
  same query. ``follow=0`` is a bounded one-shot dump;
  ``follow=1`` keeps polling for rows newer than the emitted
  high-water cursor until the client disconnects.

Reads run under :func:`tenant_agnostic` because the deployment
partition (``workspace_id IS NULL``) is invisible to the ORM
tenant filter — see :class:`AuditLog`'s docstring.

See ``docs/specs/12-rest-api.md`` §"Admin surface" §"Deployment
audit", ``docs/specs/02-domain-model.md`` §"audit_log".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.api.admin.deps import current_deployment_admin_principal
from app.api.deps import db_session
from app.audit.tail import (
    NDJSON_MEDIA_TYPE,
    AuditTailCursor,
    audit_tail_chunks,
)
from app.tenancy import DeploymentContext, tenant_agnostic

__all__ = [
    "AuditEntryResponse",
    "AuditListResponse",
    "build_admin_audit_router",
]


_Db = Annotated[Session, Depends(db_session)]

# Spec §12 "Pagination": ``limit`` defaults to 50 and caps at 500.
# Mirror the workspace-side sub-routers so the cursor-walk logic
# behaves the same on both trees.
_DEFAULT_LIMIT: Final[int] = 50
_MAX_LIMIT: Final[int] = 500


_TAIL_POLL_INTERVAL_SECONDS: Final[float] = 1.0
_TAIL_MAX_EMPTY_POLLS: Final[int | None] = None


class AuditEntryResponse(BaseModel):
    """Wire shape for one ``audit_log`` row in the deployment feed.

    Mirrors :class:`AuditLog` columns 1-for-1 except for the
    transport-shaped ``created_at`` (ISO-8601 UTC string). The
    SPA's :interface:`AuditEntry` (``mocks/web/src/types/api.ts``)
    consumes a richer projection with display labels (``actor``,
    ``target``); we surface the raw IDs here so the SPA is free
    to look up display names lazily — denormalising on the hot
    audit path would balloon the payload.
    """

    id: str
    actor_id: str
    actor_kind: str
    actor_grant_role: str
    actor_was_owner_member: bool
    entity_kind: str
    entity_id: str
    action: str
    diff: dict[str, Any] | list[Any]
    correlation_id: str
    created_at: str


class AuditListResponse(BaseModel):
    """Body of ``GET /admin/api/v1/audit``.

    Standard §12 cursor envelope. ``next_cursor`` is the
    :attr:`AuditLog.id` of the last row on the page, or ``None``
    when fewer than ``limit`` rows remain. ``has_more`` is the
    explicit boolean clients prefer over a "next is non-null"
    inference.
    """

    data: list[AuditEntryResponse]
    next_cursor: str | None
    has_more: bool


def _format_created_at(row: AuditLog) -> str:
    """ISO-8601 UTC for ``row.created_at``.

    Matches :func:`app.api.admin.me._format_granted_at` —
    SQLite drops tzinfo on ``DateTime(timezone=True)``
    round-trips, so we force UTC unconditionally.
    """
    moment = row.created_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat()


def _project_row(row: AuditLog) -> AuditEntryResponse:
    """Build the wire-shaped response from one ORM row."""
    diff = row.diff
    if not isinstance(diff, dict | list):
        # Defensive: the writer constrains diff to dict|list, but a
        # row written before cd-kgcc's diff redaction landed could
        # carry a different shape. Surface as ``{}`` rather than
        # crashing — the audit feed must stay readable.
        diff = {}
    return AuditEntryResponse(
        id=row.id,
        actor_id=row.actor_id,
        actor_kind=row.actor_kind,
        actor_grant_role=row.actor_grant_role,
        actor_was_owner_member=row.actor_was_owner_member,
        entity_kind=row.entity_kind,
        entity_id=row.entity_id,
        action=row.action,
        diff=diff,
        correlation_id=row.correlation_id,
        created_at=_format_created_at(row),
    )


def _query_rows(
    session: Session,
    *,
    actor_id: str | None,
    action: str | None,
    entity_kind: str | None,
    entity_id: str | None,
    since: datetime | None,
    until: datetime | None,
    cursor: str | None,
    limit: int,
) -> list[AuditLog]:
    """Run the deployment-audit SELECT with the supplied filters.

    Ordered newest-first by ``(created_at, id)`` to match the
    ``ix_audit_log_scope_kind_created`` index; the trailing
    ``id`` column makes the cursor-walk stable across rows that
    share a millisecond timestamp.
    """
    stmt = (
        select(AuditLog)
        .where(AuditLog.scope_kind == "deployment")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    )
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if entity_kind is not None:
        stmt = stmt.where(AuditLog.entity_kind == entity_kind)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if since is not None:
        stmt = stmt.where(AuditLog.created_at >= since)
    if until is not None:
        stmt = stmt.where(AuditLog.created_at <= until)
    if cursor is not None:
        # ``cursor`` is the id of the last row served on the
        # previous page; we walk strictly older. The lookup
        # narrows to rows whose (created_at, id) tuple is
        # strictly less than the cursor's — using a single
        # SELECT to re-fetch the cursor row would balloon the
        # query; pin the cursor by id alone and let the
        # newest-first ordering ensure we never re-visit a row.
        #
        # ``audit_log`` is a workspace-scoped table in the ORM
        # tenant filter (see :mod:`app.adapters.db.audit`); the
        # admin tree has no :class:`WorkspaceContext`, so the
        # lookup must run under :func:`tenant_agnostic` or the
        # filter raises :class:`TenantFilterMissing` at execute
        # time. The block below covers both the cursor
        # resolution and the SELECT it parameterises.
        with tenant_agnostic():
            cursor_row = session.get(AuditLog, cursor)
        if cursor_row is None:
            # Stale or fabricated cursor — the row no longer
            # exists (deleted by the retention rotator, or the
            # caller forged an id). Without an anchor we cannot
            # narrow strictly-older; silently dropping the
            # WHERE would re-page the whole feed and trap the
            # client in an infinite walk. Return an explicit
            # empty page instead so the client treats the
            # cursor as exhausted.
            return []
        stmt = stmt.where(
            (AuditLog.created_at < cursor_row.created_at)
            | (
                (AuditLog.created_at == cursor_row.created_at)
                & (AuditLog.id < cursor_row.id)
            )
        )
    # Fetch one extra row to learn whether there's a next page
    # without a second COUNT query.
    stmt = stmt.limit(limit + 1)
    with tenant_agnostic():
        return list(session.scalars(stmt).all())


def _query_newer_rows(
    session: Session,
    *,
    actor_id: str | None,
    action: str | None,
    entity_kind: str | None,
    entity_id: str | None,
    since: datetime | None,
    until: datetime | None,
    cursor: AuditTailCursor | None,
    limit: int,
) -> list[AuditLog]:
    """Return deployment-audit rows newer than the follow cursor."""
    stmt = (
        select(AuditLog)
        .where(AuditLog.scope_kind == "deployment")
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        .limit(limit)
    )
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if entity_kind is not None:
        stmt = stmt.where(AuditLog.entity_kind == entity_kind)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if since is not None:
        stmt = stmt.where(AuditLog.created_at >= since)
    if until is not None:
        stmt = stmt.where(AuditLog.created_at <= until)
    if cursor is not None:
        stmt = stmt.where(
            (AuditLog.created_at > cursor.created_at)
            | (
                (AuditLog.created_at == cursor.created_at)
                & (AuditLog.id > cursor.row_id)
            )
        )
    with tenant_agnostic():
        return list(session.scalars(stmt).all())


def _tail_cursor(row: AuditLog) -> AuditTailCursor:
    return AuditTailCursor(created_at=row.created_at, row_id=row.id)


def _parse_iso(value: str | None, *, label: str) -> datetime | None:
    """Parse an ISO-8601 query value or raise a typed 422.

    Defensive against the ``Z`` suffix (Python's
    :func:`datetime.fromisoformat` accepts ``+00:00`` but historically
    refused the literal ``Z`` until 3.11). Empty strings collapse
    to ``None`` so callers can omit the param without sending a
    sentinel.
    """
    if value is None or value == "":
        return None
    candidate = value
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_iso8601",
                "message": f"{label}: expected ISO-8601 timestamp, got {value!r}",
            },
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def build_admin_audit_router() -> APIRouter:
    """Return the router carrying the deployment-audit admin routes."""
    router = APIRouter(tags=["admin"])

    @router.get(
        "/audit",
        response_model=AuditListResponse,
        operation_id="admin.audit.list",
        summary="Page through deployment-scope audit rows",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "audit-list",
                "summary": "Page through deployment-scope audit rows",
                "mutates": False,
            },
        },
    )
    def list_audit(
        _ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        actor_id: Annotated[str | None, Query(max_length=64)] = None,
        action: Annotated[str | None, Query(max_length=128)] = None,
        entity_kind: Annotated[str | None, Query(max_length=64)] = None,
        entity_id: Annotated[str | None, Query(max_length=64)] = None,
        since: Annotated[
            str,
            Query(max_length=64, json_schema_extra={"format": "date-time"}),
        ] = "",
        until: Annotated[
            str,
            Query(max_length=64, json_schema_extra={"format": "date-time"}),
        ] = "",
        cursor: Annotated[str | None, Query(max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    ) -> AuditListResponse:
        """Return a deployment-audit page filtered by the query params.

        Pagination follows the §12 cursor envelope. The query's
        ``actor_id`` / ``action`` / ``entity_kind`` / ``entity_id``
        columns map 1-for-1 onto :class:`AuditLog` fields; ``since``
        / ``until`` clamp on ``created_at`` (inclusive). The
        cd-jlms slice does not honour the spec's free-text
        ``actor`` filter (display-name search) — that needs a join
        to :class:`User` and is filed under cd-b0au alongside the
        workspace audit feed.
        """
        rows = _query_rows(
            session,
            actor_id=actor_id,
            action=action,
            entity_kind=entity_kind,
            entity_id=entity_id,
            since=_parse_iso(since, label="since"),
            until=_parse_iso(until, label="until"),
            cursor=cursor,
            limit=limit,
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = page[-1].id if has_more and page else None
        return AuditListResponse(
            data=[_project_row(row) for row in page],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    @router.get(
        "/audit/tail",
        operation_id="admin.audit.tail",
        summary="NDJSON projection of the deployment audit feed",
        responses={
            200: {
                "content": {NDJSON_MEDIA_TYPE: {"schema": {"type": "string"}}},
                "description": "Newline-delimited JSON, one audit row per line.",
            }
        },
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "audit-tail",
                "summary": "NDJSON projection of the deployment audit feed",
                "mutates": False,
            },
        },
    )
    def tail_audit(
        _ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        # ``follow=1`` is the spec's "stream forever" knob. Surface
        # as ``int`` so the SPA can pass ``?follow=1`` literally per
        # the spec example.
        follow: Annotated[int, Query(ge=0, le=1)] = 0,
        actor_id: Annotated[str | None, Query(max_length=64)] = None,
        action: Annotated[str | None, Query(max_length=128)] = None,
        entity_kind: Annotated[str | None, Query(max_length=64)] = None,
        entity_id: Annotated[str | None, Query(max_length=64)] = None,
        since: Annotated[
            str,
            Query(max_length=64, json_schema_extra={"format": "date-time"}),
        ] = "",
        until: Annotated[
            str,
            Query(max_length=64, json_schema_extra={"format": "date-time"}),
        ] = "",
        limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    ) -> StreamingResponse:
        """Stream the deployment audit feed as NDJSON.

        The initial body returns one chunk per matching row in
        newest-first order — exactly the same projection as
        ``GET /audit`` but in NDJSON shape so a CLI ``--follow``
        consumer can ``jq -c .`` into structured data without
        peeling a JSON array. When ``follow=1``, the response then
        keeps polling and emits newer rows until disconnect.

        The handler is sync because the underlying SELECT runs
        against the synchronous SQLAlchemy session; FastAPI
        wraps the generator in a thread-pool when needed.
        """
        parsed_since = _parse_iso(since, label="since")
        parsed_until = _parse_iso(until, label="until")

        def _initial() -> list[AuditLog]:
            rows = _query_rows(
                session,
                actor_id=actor_id,
                action=action,
                entity_kind=entity_kind,
                entity_id=entity_id,
                since=parsed_since,
                until=parsed_until,
                cursor=None,
                limit=limit,
            )
            return rows[:limit]

        def _next(cursor: AuditTailCursor | None) -> list[AuditLog]:
            return _query_newer_rows(
                session,
                actor_id=actor_id,
                action=action,
                entity_kind=entity_kind,
                entity_id=entity_id,
                since=parsed_since,
                until=parsed_until,
                cursor=cursor,
                limit=limit,
            )

        return StreamingResponse(
            audit_tail_chunks(
                fetch_initial=_initial,
                fetch_next=_next,
                project_row=_project_row,
                cursor_for=_tail_cursor,
                follow=follow == 1,
                poll_interval_seconds=_TAIL_POLL_INTERVAL_SECONDS,
                max_empty_polls=_TAIL_MAX_EMPTY_POLLS,
            ),
            media_type=NDJSON_MEDIA_TYPE,
        )

    return router


# Re-exported so a future module-level helper (e.g. a
# format-audit-rows function shared with the workspace tail)
# has a stable import. Marking it ``__all__``-public means the
# eventual cd-7xth promotion stays a single-file diff.
project_audit_row = _project_row
