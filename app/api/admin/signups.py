"""Deployment-admin signup / abuse-signal surface."""

from __future__ import annotations

from datetime import UTC
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.api.admin.deps import current_deployment_admin_principal
from app.api.deps import db_session
from app.tenancy import DeploymentContext, tenant_agnostic

__all__ = [
    "SignupAuditEntry",
    "SignupAuditKind",
    "SignupsListResponse",
    "build_admin_signups_router",
]


_AdminCtx = Annotated[DeploymentContext, Depends(current_deployment_admin_principal)]
_Db = Annotated[Session, Depends(db_session)]


# Spec §12 "Pagination": ``limit`` defaults to 50 and caps at 500. We
# mirror those bounds verbatim so once the real query lands the knob
# already behaves the way the rest of v1 does.
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500

SignupAuditKind = Literal[
    "burst_rate",
    "distinct_emails_one_ip",
    "repeat_email",
    "quota_near_breach",
]
_SIGNUP_KIND_PATTERN: str = (
    "^(|burst_rate|distinct_emails_one_ip|repeat_email|quota_near_breach)$"
)


class SignupAuditEntry(BaseModel):
    """One abuse-signal row surfaced on ``/admin/signups``.

    Mirrors the §15 catalogue of signup events: each row carries
    the ``kind`` (``burst_rate`` / ``distinct_emails_one_ip`` /
    ``repeat_email`` / ``quota_near_breach`` etc.), an opaque
    ``event_id`` for the audit row, an ``occurred_at`` timestamp,
    a hashed ``ip_hash`` (no plaintext IPs per §15 "Logging and
    redaction") and a free-form ``detail`` bag the UI projects
    without interpretation.

    The model is defined today — with no emitting call site — so
    cd-ovt4 has a stable wire contract to populate. Every field is
    optional on the placeholder shape because no real row exists;
    cd-ovt4 will tighten the Pydantic model as the emitter lands.
    """

    event_id: str
    kind: SignupAuditKind
    occurred_at: str
    ip_hash: str | None = None
    email_hash: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class SignupsListResponse(BaseModel):
    """Collection envelope for ``GET /admin/signups``.

    Shape matches the §12 "Pagination" canonical envelope —
    ``{data, next_cursor, has_more}`` — so the downstream task that
    wires the real query against ``audit_log`` doesn't have to
    re-shape the response. Returning the envelope shape today also
    means the SPA can start rendering the page (empty state, loading
    skeleton, filter chrome) before cd-ovt4 lands.
    """

    data: list[SignupAuditEntry]
    next_cursor: str | None = None
    has_more: bool = False


def build_admin_signups_router() -> APIRouter:
    """Return the router carrying the deployment signup-abuse feed."""
    router = APIRouter(tags=["admin"])

    @router.get(
        "/signups",
        response_model=SignupsListResponse,
        operation_id="admin.signups.list",
        summary="List deployment signup abuse signals",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "signups-list",
                "summary": "List deployment signup abuse signals",
                "mutates": False,
            },
        },
    )
    def get_signups(
        _ctx: _AdminCtx,
        session: _Db,
        kind: Annotated[str, Query(pattern=_SIGNUP_KIND_PATTERN)] = "",
        cursor: Annotated[str | None, Query(max_length=256)] = None,
        limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    ) -> SignupsListResponse:
        """Return deployment-scoped suspicious signup signals."""
        rows = _query_rows(session, kind=_query_kind(kind))
        entries = _project_rows(rows)
        if cursor is not None:
            entries = _entries_after_cursor(entries, cursor=cursor)
        page = entries[:limit]
        return SignupsListResponse(
            data=page,
            next_cursor=page[-1].event_id if len(entries) > limit else None,
            has_more=len(entries) > limit,
        )

    return router


def _format_created_at(row: AuditLog) -> str:
    moment = row.created_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat()


def _query_rows(
    session: Session,
    *,
    kind: SignupAuditKind | None,
) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(
            AuditLog.scope_kind == "deployment",
            AuditLog.action == "audit.signup.suspicious",
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    )
    with tenant_agnostic():
        rows = list(session.scalars(stmt).all())
    if kind is None:
        return rows
    return [row for row in rows if _row_kind(row) == kind]


def _query_kind(value: str) -> SignupAuditKind | None:
    if value == "":
        return None
    if value == "burst_rate":
        return "burst_rate"
    if value == "distinct_emails_one_ip":
        return "distinct_emails_one_ip"
    if value == "repeat_email":
        return "repeat_email"
    return "quota_near_breach"


def _entries_after_cursor(
    entries: list[SignupAuditEntry],
    *,
    cursor: str,
) -> list[SignupAuditEntry]:
    for idx, entry in enumerate(entries):
        if entry.event_id == cursor:
            return entries[idx + 1 :]
    return []


def _row_diff(row: AuditLog) -> dict[str, Any]:
    diff = row.diff
    return diff if isinstance(diff, dict) else {}


def _row_kind(row: AuditLog) -> SignupAuditKind | None:
    raw = _row_diff(row).get("kind")
    if raw == "burst_rate":
        return "burst_rate"
    if raw == "distinct_emails_one_ip":
        return "distinct_emails_one_ip"
    if raw == "repeat_email":
        return "repeat_email"
    if raw == "quota_near_breach":
        return "quota_near_breach"
    return None


def _hash_value(diff: dict[str, Any], key: str) -> str | None:
    value = diff.get(key)
    return value if isinstance(value, str) and value else None


def _project_row(row: AuditLog, *, count: int | None = None) -> SignupAuditEntry:
    diff = _row_diff(row)
    kind = _row_kind(row)
    if kind is None:
        raise ValueError("cannot project unknown signup signal kind")
    detail = dict(diff)
    if count is not None:
        detail["count"] = count
    return SignupAuditEntry(
        event_id=row.id,
        kind=kind,
        occurred_at=_format_created_at(row),
        ip_hash=_hash_value(diff, "ip_hash"),
        email_hash=_hash_value(diff, "email_hash"),
        detail=detail,
    )


def _project_rows(rows: list[AuditLog]) -> list[SignupAuditEntry]:
    entries: list[SignupAuditEntry] = []
    repeat_rollups: dict[str, tuple[AuditLog, int]] = {}
    for row in rows:
        diff = _row_diff(row)
        kind = _row_kind(row)
        if kind is None:
            continue
        if kind != "repeat_email":
            entries.append(_project_row(row))
            continue
        email_hash = _hash_value(diff, "email_hash")
        if email_hash is None:
            entries.append(_project_row(row))
            continue
        current = repeat_rollups.get(email_hash)
        if current is None:
            repeat_rollups[email_hash] = (row, 1)
        else:
            newest, count = current
            repeat_rollups[email_hash] = (newest, count + 1)
    entries.extend(
        _project_row(row, count=count) for row, count in repeat_rollups.values()
    )
    entries.sort(key=lambda entry: (entry.occurred_at, entry.event_id), reverse=True)
    return entries
