"""Client portal read-only API."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.adapters.db.billing.repositories import SqlAlchemyClientPortalRepository
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    Cursor,
    CursorScalar,
    LimitQuery,
    PageCursorQuery,
    decode_page_cursor,
    encode_page_cursor,
)
from app.audit import write_audit
from app.domain.billing.client_portal import (
    ClientPortalBillableHoursRow,
    ClientPortalForbidden,
    ClientPortalInvoiceRow,
    ClientPortalPropertyRow,
    ClientPortalQuoteRow,
    ClientPortalService,
)
from app.domain.errors import Forbidden, InvalidCursor
from app.tenancy import WorkspaceContext

__all__ = ["build_client_portal_router"]

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_CACHE_CONTROL = "private, max-age=30"


class ClientPortalPropertyResponse(BaseModel):
    id: str
    organization_id: str
    organization_name: str | None
    name: str
    kind: str
    address: str
    country: str
    timezone: str
    default_currency: str | None

    @classmethod
    def from_row(cls, row: ClientPortalPropertyRow) -> ClientPortalPropertyResponse:
        return cls(
            id=row.id,
            organization_id=row.organization_id,
            organization_name=row.organization_name,
            name=row.name,
            kind=row.kind,
            address=row.address,
            country=row.country,
            timezone=row.timezone,
            default_currency=row.default_currency,
        )


class ClientPortalBillableHoursResponse(BaseModel):
    work_order_id: str
    property_id: str
    property_name: str
    week_start: date
    hours_decimal: Decimal
    total_cents: int
    currency: str

    @classmethod
    def from_row(
        cls, row: ClientPortalBillableHoursRow
    ) -> ClientPortalBillableHoursResponse:
        return cls(
            work_order_id=row.work_order_id,
            property_id=row.property_id,
            property_name=row.property_name,
            week_start=row.week_start,
            hours_decimal=row.hours_decimal,
            total_cents=row.total_cents,
            currency=row.currency,
        )


class ClientPortalInvoiceResponse(BaseModel):
    id: str
    organization_id: str
    invoice_number: str
    issued_at: date
    due_at: date | None
    total_cents: int
    currency: str
    status: str
    proof_of_payment_file_ids: tuple[str, ...]
    pdf_url: str | None

    @classmethod
    def from_row(cls, row: ClientPortalInvoiceRow) -> ClientPortalInvoiceResponse:
        return cls(
            id=row.id,
            organization_id=row.organization_id,
            invoice_number=row.invoice_number,
            issued_at=row.issued_at,
            due_at=row.due_at,
            total_cents=row.total_cents,
            currency=row.currency,
            status=row.status,
            proof_of_payment_file_ids=row.proof_of_payment_file_ids,
            pdf_url=row.pdf_url,
        )


class ClientPortalQuoteResponse(BaseModel):
    id: str
    organization_id: str
    property_id: str
    title: str
    total_cents: int
    currency: str
    status: str
    sent_at: datetime | None
    decided_at: datetime | None
    accept_url: str | None

    @classmethod
    def from_row(
        cls, row: ClientPortalQuoteRow, ctx: WorkspaceContext
    ) -> ClientPortalQuoteResponse:
        return cls(
            id=row.id,
            organization_id=row.organization_id,
            property_id=row.property_id,
            title=row.title,
            total_cents=row.total_cents,
            currency=row.currency,
            status=row.status,
            sent_at=row.sent_at,
            decided_at=row.decided_at,
            accept_url=(
                f"/w/{ctx.workspace_slug}/api/v1/billing/quotes/{row.id}/accept"
                if row.status == "sent"
                else None
            ),
        )


class ClientPortalPropertyPage(BaseModel):
    data: list[ClientPortalPropertyResponse]
    next_cursor: str | None
    has_more: bool


class ClientPortalBillableHoursPage(BaseModel):
    data: list[ClientPortalBillableHoursResponse]
    next_cursor: str | None
    has_more: bool


class ClientPortalInvoicePage(BaseModel):
    data: list[ClientPortalInvoiceResponse]
    next_cursor: str | None
    has_more: bool


class ClientPortalQuotePage(BaseModel):
    data: list[ClientPortalQuoteResponse]
    next_cursor: str | None
    has_more: bool


def _forbidden(exc: ClientPortalForbidden) -> Forbidden:
    message = str(exc)
    return Forbidden(
        message,
        extra={"error": "client_portal_forbidden", "message": message},
    )


def _set_cache_header(response: Response) -> None:
    response.headers["Cache-Control"] = _CACHE_CONTROL


def _audit_view(session: Session, ctx: WorkspaceContext, *, slug: str) -> None:
    write_audit(
        session,
        ctx,
        entity_kind="client_portal",
        entity_id=ctx.workspace_id,
        action="client_portal.viewed",
        diff={"slug": slug},
        via="api",
    )


def _cursor_value(value: CursorScalar) -> CursorScalar:
    return value


def _decoded_cursor(cursor: str | None) -> Cursor | None:
    decoded = decode_page_cursor(cursor)
    if decoded is not None and not isinstance(decoded.last_sort_value, str):
        raise InvalidCursor(
            "cursor sort value is invalid for this resource",
            extra={
                "error": "invalid_cursor",
                "message": "cursor sort value is invalid for this resource",
            },
        )
    return decoded


def _page_rows[RowT: object](
    rows: Sequence[RowT],
    *,
    limit: int,
    cursor: str | None,
    sort_value: Callable[[RowT], CursorScalar],
    row_id: Callable[[RowT], str],
    direction: str = "asc",
) -> tuple[list[RowT], str | None, bool]:
    decoded = _decoded_cursor(cursor)
    descending = direction == "desc"
    ordered_rows = sorted(
        rows,
        key=lambda row: (_cursor_value(sort_value(row)), row_id(row)),
        reverse=descending,
    )
    page_rows: list[RowT] = []
    for row in ordered_rows:
        if decoded is not None:
            boundary = (decoded.last_sort_value, decoded.last_id_ulid)
            current = (_cursor_value(sort_value(row)), row_id(row))
            if (current >= boundary) if descending else (current <= boundary):
                continue
        page_rows.append(row)
        if len(page_rows) > limit:
            break

    has_more = len(page_rows) > limit
    data = page_rows[:limit]
    next_cursor = None
    if has_more and data:
        last = data[-1]
        next_cursor = encode_page_cursor(
            Cursor(last_sort_value=sort_value(last), last_id_ulid=row_id(last))
        )
    return data, next_cursor, has_more


def build_client_portal_router() -> APIRouter:
    router = APIRouter(prefix="/client", tags=["client", "billing"])

    @router.get(
        "/portfolio",
        response_model=ClientPortalPropertyPage,
        operation_id="client.portfolio.list",
        summary="List client-owned properties",
    )
    def portfolio(
        response: Response,
        ctx: _Ctx,
        session: _Db,
        limit: LimitQuery = DEFAULT_LIMIT,
        cursor: PageCursorQuery = None,
    ) -> ClientPortalPropertyPage:
        try:
            rows = ClientPortalService(ctx).portfolio(
                SqlAlchemyClientPortalRepository(session)
            )
        except ClientPortalForbidden as exc:
            raise _forbidden(exc) from exc
        data, next_cursor, has_more = _page_rows(
            rows,
            limit=limit,
            cursor=cursor,
            sort_value=lambda row: row.name.lower(),
            row_id=lambda row: row.id,
        )
        _set_cache_header(response)
        _audit_view(session, ctx, slug="portfolio")
        return ClientPortalPropertyPage(
            data=[ClientPortalPropertyResponse.from_row(row) for row in data],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    @router.get(
        "/billable-hours",
        response_model=ClientPortalBillableHoursPage,
        operation_id="client.billable_hours.list",
        summary="List client billable hours",
    )
    def billable_hours(
        response: Response,
        ctx: _Ctx,
        session: _Db,
        limit: LimitQuery = DEFAULT_LIMIT,
        cursor: PageCursorQuery = None,
    ) -> ClientPortalBillableHoursPage:
        try:
            rows = ClientPortalService(ctx).billable_hours(
                SqlAlchemyClientPortalRepository(session)
            )
        except ClientPortalForbidden as exc:
            raise _forbidden(exc) from exc
        data, next_cursor, has_more = _page_rows(
            rows,
            limit=limit,
            cursor=cursor,
            sort_value=lambda row: row.week_start.isoformat(),
            row_id=lambda row: f"{row.work_order_id}:{row.property_id}",
        )
        _set_cache_header(response)
        _audit_view(session, ctx, slug="billable-hours")
        return ClientPortalBillableHoursPage(
            data=[ClientPortalBillableHoursResponse.from_row(row) for row in data],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    @router.get(
        "/invoices",
        response_model=ClientPortalInvoicePage,
        operation_id="client.invoices.list",
        summary="List client invoices",
    )
    def invoices(
        response: Response,
        ctx: _Ctx,
        session: _Db,
        limit: LimitQuery = DEFAULT_LIMIT,
        cursor: PageCursorQuery = None,
    ) -> ClientPortalInvoicePage:
        try:
            rows = ClientPortalService(ctx).invoices(
                SqlAlchemyClientPortalRepository(session)
            )
        except ClientPortalForbidden as exc:
            raise _forbidden(exc) from exc
        data, next_cursor, has_more = _page_rows(
            rows,
            limit=limit,
            cursor=cursor,
            sort_value=lambda row: row.issued_at.isoformat(),
            row_id=lambda row: row.id,
            direction="desc",
        )
        _set_cache_header(response)
        _audit_view(session, ctx, slug="invoices")
        return ClientPortalInvoicePage(
            data=[ClientPortalInvoiceResponse.from_row(row) for row in data],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    @router.get(
        "/quotes",
        response_model=ClientPortalQuotePage,
        operation_id="client.quotes.list",
        summary="List client quotes",
    )
    def quotes(
        response: Response,
        ctx: _Ctx,
        session: _Db,
        limit: LimitQuery = DEFAULT_LIMIT,
        cursor: PageCursorQuery = None,
    ) -> ClientPortalQuotePage:
        try:
            rows = ClientPortalService(ctx).quotes(
                SqlAlchemyClientPortalRepository(session)
            )
        except ClientPortalForbidden as exc:
            raise _forbidden(exc) from exc
        data, next_cursor, has_more = _page_rows(
            rows,
            limit=limit,
            cursor=cursor,
            sort_value=lambda row: row.sent_at.isoformat() if row.sent_at else "",
            row_id=lambda row: row.id,
            direction="desc",
        )
        _set_cache_header(response)
        _audit_view(session, ctx, slug="quotes")
        return ClientPortalQuotePage(
            data=[ClientPortalQuoteResponse.from_row(row, ctx) for row in data],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    return router
