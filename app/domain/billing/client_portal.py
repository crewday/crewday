"""Client-portal read service for billing data."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session

from app.tenancy import WorkspaceContext

__all__ = [
    "ClientPortalBillableHoursRow",
    "ClientPortalForbidden",
    "ClientPortalInvoiceRow",
    "ClientPortalPropertyRow",
    "ClientPortalQuoteRow",
    "ClientPortalRepository",
    "ClientPortalScope",
    "ClientPortalService",
    "raw_accruals_to_billable_hours",
]


class ClientPortalForbidden(PermissionError):
    """The caller has no client grant in the active workspace."""


@dataclass(frozen=True, slots=True)
class ClientPortalScope:
    workspace_org_ids: frozenset[str]
    property_ids: frozenset[str]
    property_org_ids: frozenset[str]

    @property
    def organization_ids(self) -> frozenset[str]:
        return self.workspace_org_ids | self.property_org_ids

    @property
    def is_empty(self) -> bool:
        return not self.workspace_org_ids and not self.property_ids


@dataclass(frozen=True, slots=True)
class ClientPortalPropertyRow:
    id: str
    organization_id: str
    organization_name: str | None
    name: str
    kind: str
    address: str
    country: str
    timezone: str
    default_currency: str | None


@dataclass(frozen=True, slots=True)
class ClientPortalAccrualRow:
    work_order_id: str
    property_id: str
    property_name: str
    organization_id: str
    currency: str
    hours_decimal: Decimal
    accrued_cents: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ClientPortalBillableHoursRow:
    work_order_id: str
    property_id: str
    property_name: str
    week_start: date
    hours_decimal: Decimal
    total_cents: int
    currency: str


@dataclass(frozen=True, slots=True)
class ClientPortalInvoiceRow:
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


@dataclass(frozen=True, slots=True)
class ClientPortalQuoteRow:
    id: str
    organization_id: str
    property_id: str
    title: str
    total_cents: int
    currency: str
    status: str
    sent_at: datetime | None
    decided_at: datetime | None


class ClientPortalRepository(Protocol):
    @property
    def session(self) -> Session: ...

    def client_scope(self, *, workspace_id: str, user_id: str) -> ClientPortalScope: ...

    def list_portfolio(
        self, *, workspace_id: str, scope: ClientPortalScope
    ) -> Sequence[ClientPortalPropertyRow]: ...

    def list_accruals(
        self, *, workspace_id: str, scope: ClientPortalScope
    ) -> Sequence[ClientPortalAccrualRow]: ...

    def list_invoices(
        self, *, workspace_id: str, scope: ClientPortalScope
    ) -> Sequence[ClientPortalInvoiceRow]: ...

    def list_quotes(
        self, *, workspace_id: str, scope: ClientPortalScope
    ) -> Sequence[ClientPortalQuoteRow]: ...


def _week_start(value: datetime) -> date:
    created_date = value.date()
    return created_date - timedelta(days=created_date.weekday())


def raw_accruals_to_billable_hours(
    rows: Sequence[ClientPortalAccrualRow],
) -> tuple[ClientPortalBillableHoursRow, ...]:
    grouped: dict[
        tuple[str, str, str, date, str],
        tuple[Decimal, int],
    ] = {}
    for row in rows:
        key = (
            row.work_order_id,
            row.property_id,
            row.property_name,
            _week_start(row.created_at),
            row.currency,
        )
        hours, cents = grouped.get(key, (Decimal("0.00"), 0))
        grouped[key] = (hours + row.hours_decimal, cents + row.accrued_cents)

    out = [
        ClientPortalBillableHoursRow(
            work_order_id=work_order_id,
            property_id=property_id,
            property_name=property_name,
            week_start=week_start,
            hours_decimal=hours.quantize(Decimal("0.01")),
            total_cents=total_cents,
            currency=currency,
        )
        for (
            work_order_id,
            property_id,
            property_name,
            week_start,
            currency,
        ), (hours, total_cents) in grouped.items()
    ]
    return tuple(
        sorted(
            out,
            key=lambda row: (
                row.week_start,
                row.property_name.lower(),
                row.work_order_id,
            ),
        )
    )


class ClientPortalService:
    """Read-only client portal service scoped by client grants."""

    def __init__(self, ctx: WorkspaceContext) -> None:
        self._ctx = ctx

    def _scope(self, repo: ClientPortalRepository) -> ClientPortalScope:
        scope = repo.client_scope(
            workspace_id=self._ctx.workspace_id,
            user_id=self._ctx.actor_id,
        )
        if scope.is_empty:
            raise ClientPortalForbidden("client portal requires a client grant")
        return scope

    def portfolio(
        self, repo: ClientPortalRepository
    ) -> tuple[ClientPortalPropertyRow, ...]:
        return tuple(
            repo.list_portfolio(
                workspace_id=self._ctx.workspace_id, scope=self._scope(repo)
            )
        )

    def billable_hours(
        self, repo: ClientPortalRepository
    ) -> tuple[ClientPortalBillableHoursRow, ...]:
        rows = repo.list_accruals(
            workspace_id=self._ctx.workspace_id, scope=self._scope(repo)
        )
        return raw_accruals_to_billable_hours(rows)

    def invoices(
        self, repo: ClientPortalRepository
    ) -> tuple[ClientPortalInvoiceRow, ...]:
        return tuple(
            repo.list_invoices(
                workspace_id=self._ctx.workspace_id, scope=self._scope(repo)
            )
        )

    def quotes(self, repo: ClientPortalRepository) -> tuple[ClientPortalQuoteRow, ...]:
        return tuple(
            repo.list_quotes(
                workspace_id=self._ctx.workspace_id, scope=self._scope(repo)
            )
        )
