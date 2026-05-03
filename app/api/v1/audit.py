"""Workspace-scoped audit feed routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.api.deps import current_workspace_context, db_session
from app.audit.tail import NDJSON_MEDIA_TYPE, AuditTailCursor, audit_tail_chunks
from app.authz.dep import Permission
from app.tenancy import WorkspaceContext

__all__ = [
    "NDJSON_MEDIA_TYPE",
    "AuditEntryResponse",
    "AuditListResponse",
    "build_workspace_audit_router",
    "router",
]

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]

_DEFAULT_LIMIT: Final[int] = 50
_MAX_LIMIT: Final[int] = 500
_TAIL_POLL_INTERVAL_SECONDS: Final[float] = 1.0
_TAIL_MAX_EMPTY_POLLS: Final[int | None] = None


class AuditEntryResponse(BaseModel):
    at: str
    actor_kind: str
    actor: str
    action: str
    target: str
    via: str
    reason: str | None
    actor_grant_role: str | None
    actor_was_owner_member: bool | None
    actor_action_key: str | None
    actor_id: str | None
    agent_label: str | None
    entity_kind: str
    entity_id: str
    correlation_id: str
    diff: dict[str, Any] | list[Any]


class AuditListResponse(BaseModel):
    data: list[AuditEntryResponse]
    next_cursor: str | None
    has_more: bool


def _format_created_at(row: AuditLog) -> str:
    moment = row.created_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat()


def _parse_iso(value: str | None, *, label: str) -> datetime | None:
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
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


def _project_row(row: AuditLog) -> AuditEntryResponse:
    diff = row.diff
    if not isinstance(diff, dict | list):
        diff = {}
    reason = None
    if isinstance(diff, dict):
        raw_reason = diff.get("reason")
        if isinstance(raw_reason, str):
            reason = raw_reason
    return AuditEntryResponse(
        at=_format_created_at(row),
        actor_kind=row.actor_kind,
        actor=row.actor_id,
        action=row.action,
        target=f"{row.entity_kind}:{row.entity_id}",
        via=row.via,
        reason=reason,
        actor_grant_role=row.actor_grant_role,
        actor_was_owner_member=row.actor_was_owner_member,
        actor_action_key=None,
        actor_id=row.actor_id,
        agent_label=None,
        entity_kind=row.entity_kind,
        entity_id=row.entity_id,
        correlation_id=row.correlation_id,
        diff=diff,
    )


def _clean_filter(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _cursor_anchor(
    session: Session,
    *,
    workspace_id: str,
    cursor: str | None,
) -> AuditLog | None:
    if cursor is None:
        return None
    return session.scalar(
        select(AuditLog)
        .where(AuditLog.workspace_id == workspace_id)
        .where(AuditLog.scope_kind == "workspace")
        .where(AuditLog.id == cursor)
    )


def _query_rows(
    session: Session,
    *,
    workspace_id: str,
    actor: str | None,
    actor_id: str | None,
    action: str | None,
    entity: str | None,
    entity_kind: str | None,
    entity_id: str | None,
    since: datetime | None,
    until: datetime | None,
    cursor: str | None,
    limit: int,
) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.workspace_id == workspace_id)
        .where(AuditLog.scope_kind == "workspace")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    )
    if actor is not None:
        stmt = stmt.where(
            or_(
                AuditLog.actor_id == actor,
                AuditLog.actor_kind == actor,
                AuditLog.actor_grant_role == actor,
            )
        )
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if entity is not None:
        if ":" in entity:
            kind, row_id = entity.split(":", 1)
            stmt = stmt.where(
                AuditLog.entity_kind == kind,
                AuditLog.entity_id == row_id,
            )
        else:
            stmt = stmt.where(
                or_(AuditLog.entity_kind == entity, AuditLog.entity_id == entity)
            )
    if entity_kind is not None:
        stmt = stmt.where(AuditLog.entity_kind == entity_kind)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if since is not None:
        stmt = stmt.where(AuditLog.created_at >= since)
    if until is not None:
        stmt = stmt.where(AuditLog.created_at <= until)

    cursor_row = _cursor_anchor(session, workspace_id=workspace_id, cursor=cursor)
    if cursor is not None and cursor_row is None:
        return []
    if cursor_row is not None:
        stmt = stmt.where(
            (AuditLog.created_at < cursor_row.created_at)
            | (
                (AuditLog.created_at == cursor_row.created_at)
                & (AuditLog.id < cursor_row.id)
            )
        )

    return list(session.scalars(stmt.limit(limit + 1)).all())


def _query_newer_rows(
    session: Session,
    *,
    workspace_id: str,
    actor: str | None,
    actor_id: str | None,
    action: str | None,
    entity: str | None,
    entity_kind: str | None,
    entity_id: str | None,
    since: datetime | None,
    until: datetime | None,
    cursor: AuditTailCursor | None,
    limit: int,
) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.workspace_id == workspace_id)
        .where(AuditLog.scope_kind == "workspace")
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    )
    if actor is not None:
        stmt = stmt.where(
            or_(
                AuditLog.actor_id == actor,
                AuditLog.actor_kind == actor,
                AuditLog.actor_grant_role == actor,
            )
        )
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if entity is not None:
        if ":" in entity:
            kind, row_id = entity.split(":", 1)
            stmt = stmt.where(
                AuditLog.entity_kind == kind,
                AuditLog.entity_id == row_id,
            )
        else:
            stmt = stmt.where(
                or_(AuditLog.entity_kind == entity, AuditLog.entity_id == entity)
            )
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
    return list(session.scalars(stmt.limit(limit)).all())


def _tail_cursor(row: AuditLog) -> AuditTailCursor:
    return AuditTailCursor(created_at=row.created_at, row_id=row.id)


def build_workspace_audit_router() -> APIRouter:
    router = APIRouter(
        tags=["audit"],
        dependencies=[Depends(Permission("audit_log.view", scope_kind="workspace"))],
    )

    @router.get(
        "/audit",
        response_model=AuditListResponse,
        operation_id="audit.list",
        summary="Page through workspace-scoped audit rows",
        openapi_extra={
            "x-cli": {
                "group": "audit",
                "verb": "list",
                "summary": "Page through workspace-scoped audit rows",
                "mutates": False,
            },
        },
    )
    def list_audit(
        ctx: _Ctx,
        session: _Db,
        actor: Annotated[str | None, Query(max_length=128)] = None,
        actor_id: Annotated[str | None, Query(max_length=64)] = None,
        action: Annotated[str | None, Query(max_length=128)] = None,
        entity: Annotated[str | None, Query(max_length=128)] = None,
        entity_kind: Annotated[str | None, Query(max_length=64)] = None,
        entity_id: Annotated[str | None, Query(max_length=64)] = None,
        since: Annotated[str | None, Query(max_length=64)] = None,
        until: Annotated[str | None, Query(max_length=64)] = None,
        cursor: Annotated[str | None, Query(max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    ) -> AuditListResponse:
        rows = _query_rows(
            session,
            workspace_id=ctx.workspace_id,
            actor=_clean_filter(actor),
            actor_id=_clean_filter(actor_id),
            action=_clean_filter(action),
            entity=_clean_filter(entity),
            entity_kind=_clean_filter(entity_kind),
            entity_id=_clean_filter(entity_id),
            since=_parse_iso(since, label="since"),
            until=_parse_iso(until, label="until"),
            cursor=_clean_filter(cursor),
            limit=limit,
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        return AuditListResponse(
            data=[_project_row(row) for row in page],
            next_cursor=page[-1].id if has_more and page else None,
            has_more=has_more,
        )

    @router.get(
        "/audit/tail",
        operation_id="audit.tail",
        summary="NDJSON projection of workspace-scoped audit rows",
        responses={
            200: {
                "content": {NDJSON_MEDIA_TYPE: {"schema": {"type": "string"}}},
                "description": "Newline-delimited JSON, one audit row per line.",
            }
        },
        openapi_extra={
            "x-cli": {
                "group": "audit",
                "verb": "tail",
                "summary": "NDJSON projection of workspace-scoped audit rows",
                "mutates": False,
            },
        },
    )
    def tail_audit(
        ctx: _Ctx,
        session: _Db,
        follow: Annotated[int, Query(ge=0, le=1)] = 0,
        actor: Annotated[str | None, Query(max_length=128)] = None,
        actor_id: Annotated[str | None, Query(max_length=64)] = None,
        action: Annotated[str | None, Query(max_length=128)] = None,
        entity: Annotated[str | None, Query(max_length=128)] = None,
        entity_kind: Annotated[str | None, Query(max_length=64)] = None,
        entity_id: Annotated[str | None, Query(max_length=64)] = None,
        since: Annotated[str | None, Query(max_length=64)] = None,
        until: Annotated[str | None, Query(max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    ) -> StreamingResponse:
        cleaned_actor = _clean_filter(actor)
        cleaned_actor_id = _clean_filter(actor_id)
        cleaned_action = _clean_filter(action)
        cleaned_entity = _clean_filter(entity)
        cleaned_entity_kind = _clean_filter(entity_kind)
        cleaned_entity_id = _clean_filter(entity_id)
        parsed_since = _parse_iso(since, label="since")
        parsed_until = _parse_iso(until, label="until")

        def _initial() -> list[AuditLog]:
            rows = _query_rows(
                session,
                workspace_id=ctx.workspace_id,
                actor=cleaned_actor,
                actor_id=cleaned_actor_id,
                action=cleaned_action,
                entity=cleaned_entity,
                entity_kind=cleaned_entity_kind,
                entity_id=cleaned_entity_id,
                since=parsed_since,
                until=parsed_until,
                cursor=None,
                limit=limit,
            )
            return rows[:limit]

        def _next(cursor: AuditTailCursor | None) -> list[AuditLog]:
            return _query_newer_rows(
                session,
                workspace_id=ctx.workspace_id,
                actor=cleaned_actor,
                actor_id=cleaned_actor_id,
                action=cleaned_action,
                entity=cleaned_entity,
                entity_kind=cleaned_entity_kind,
                entity_id=cleaned_entity_id,
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


router = build_workspace_audit_router()
