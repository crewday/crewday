"""SQLAlchemy repositories for billing domain services."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import and_, false, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql import ColumnElement

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.billing.models import (
    Organization,
    Quote,
    RateCard,
    VendorInvoice,
    WorkOrder,
    WorkOrderShiftAccrual,
)
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.time.models import Shift
from app.adapters.db.workspace.models import Workspace
from app.domain.billing.client_portal import (
    ClientPortalAccrualRow,
    ClientPortalInvoiceRow,
    ClientPortalPropertyRow,
    ClientPortalQuoteRow,
    ClientPortalRepository,
    ClientPortalScope,
)
from app.domain.billing.organizations import (
    OrganizationArtifactCounts,
    OrganizationInvalid,
    OrganizationRepository,
    OrganizationRow,
)
from app.domain.billing.quotes import (
    QuoteInvalid,
    QuoteLine,
    QuoteLinesJson,
    QuoteRepository,
    QuoteRow,
)
from app.domain.billing.rate_cards import (
    RateCardInvalid,
    RateCardOrganizationRow,
    RateCardRepository,
    RateCardRow,
)
from app.domain.billing.vendor_invoices import (
    VendorInvoiceInvalid,
    VendorInvoiceOrganizationRow,
    VendorInvoiceRepository,
    VendorInvoiceRow,
)
from app.domain.billing.work_orders import (
    ShiftAccrualRow,
    WorkOrderInvalid,
    WorkOrderOrganizationRow,
    WorkOrderPropertyRow,
    WorkOrderRateCardRow,
    WorkOrderRepository,
    WorkOrderRow,
    WorkOrderShiftRow,
)
from app.tenancy import tenant_agnostic

__all__ = [
    "SqlAlchemyClientPortalRepository",
    "SqlAlchemyOrganizationRepository",
    "SqlAlchemyQuoteRepository",
    "SqlAlchemyRateCardRepository",
    "SqlAlchemyVendorInvoiceRepository",
    "SqlAlchemyWorkOrderRepository",
]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_utc_optional(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _as_utc(value)


def _org_or_property_scope(
    *,
    organization_column: ColumnElement[str] | InstrumentedAttribute[str],
    property_column: ColumnElement[str] | InstrumentedAttribute[str],
    scope: ClientPortalScope,
) -> ColumnElement[bool]:
    clauses: list[ColumnElement[bool]] = []
    if scope.workspace_org_ids:
        clauses.append(organization_column.in_(scope.workspace_org_ids))
    if scope.property_ids:
        clauses.append(property_column.in_(scope.property_ids))
    return or_(*clauses) if clauses else false()


def _to_row(row: Organization) -> OrganizationRow:
    return OrganizationRow(
        id=row.id,
        workspace_id=row.workspace_id,
        kind=row.kind,
        display_name=row.display_name,
        billing_address=dict(row.billing_address),
        tax_id=row.tax_id,
        default_currency=row.default_currency,
        contact_email=row.contact_email,
        contact_phone=row.contact_phone,
        notes_md=row.notes_md,
        created_at=_as_utc(row.created_at),
        archived_at=_as_utc_optional(row.archived_at),
    )


class SqlAlchemyOrganizationRepository(OrganizationRepository):
    """SA-backed organization repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def get_workspace_default_currency(self, *, workspace_id: str) -> str | None:
        return self._session.scalar(
            select(Workspace.default_currency).where(Workspace.id == workspace_id)
        )

    def insert(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        kind: str,
        display_name: str,
        billing_address: Mapping[str, object],
        tax_id: str | None,
        default_currency: str,
        contact_email: str | None,
        contact_phone: str | None,
        notes_md: str | None,
        created_at: datetime,
    ) -> OrganizationRow:
        row = Organization(
            id=organization_id,
            workspace_id=workspace_id,
            kind=kind,
            display_name=display_name,
            billing_address=dict(billing_address),
            tax_id=tax_id,
            default_currency=default_currency,
            contact_email=contact_email,
            contact_phone=contact_phone,
            notes_md=notes_md,
            created_at=created_at,
            archived_at=None,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError as exc:
            raise OrganizationInvalid(
                f"organization named {display_name!r} already exists"
            ) from exc
        return _to_row(row)

    def get(
        self,
        *,
        workspace_id: str,
        organization_id: str,
        include_archived: bool,
        for_update: bool = False,
    ) -> OrganizationRow | None:
        stmt = select(Organization).where(
            Organization.workspace_id == workspace_id,
            Organization.id == organization_id,
        )
        if not include_archived:
            stmt = stmt.where(Organization.archived_at.is_(None))
        if for_update:
            stmt = stmt.with_for_update()
        row = self._session.scalars(stmt).one_or_none()
        return _to_row(row) if row is not None else None

    def get_by_display_name(
        self,
        *,
        workspace_id: str,
        display_name: str,
        exclude_id: str | None = None,
    ) -> OrganizationRow | None:
        stmt = select(Organization).where(
            Organization.workspace_id == workspace_id,
            Organization.display_name == display_name,
        )
        if exclude_id is not None:
            stmt = stmt.where(Organization.id != exclude_id)
        row = self._session.scalars(stmt).one_or_none()
        return _to_row(row) if row is not None else None

    def list(
        self,
        *,
        workspace_id: str,
        kind: str | None,
        search: str | None,
        include_archived: bool,
    ) -> Sequence[OrganizationRow]:
        stmt = (
            select(Organization)
            .where(Organization.workspace_id == workspace_id)
            .order_by(Organization.display_name.asc(), Organization.id.asc())
        )
        if kind is not None:
            stmt = stmt.where(Organization.kind == kind)
        if search is not None:
            stmt = stmt.where(Organization.display_name.ilike(f"%{search}%"))
        if not include_archived:
            stmt = stmt.where(Organization.archived_at.is_(None))
        return [_to_row(row) for row in self._session.scalars(stmt).all()]

    def artifact_counts(
        self, *, workspace_id: str, organization_id: str
    ) -> OrganizationArtifactCounts:
        return OrganizationArtifactCounts(
            rate_cards=self._count(
                RateCard,
                workspace_id=workspace_id,
                organization_column="organization_id",
                organization_id=organization_id,
            ),
            work_orders=self._count(
                WorkOrder,
                workspace_id=workspace_id,
                organization_column="organization_id",
                organization_id=organization_id,
            ),
            quotes=self._count(
                Quote,
                workspace_id=workspace_id,
                organization_column="organization_id",
                organization_id=organization_id,
            ),
            vendor_invoices=self._count(
                VendorInvoice,
                workspace_id=workspace_id,
                organization_column="vendor_org_id",
                organization_id=organization_id,
            ),
        )

    def update_fields(
        self,
        *,
        workspace_id: str,
        organization_id: str,
        fields: Mapping[str, object | None],
    ) -> OrganizationRow:
        row = self._load(workspace_id=workspace_id, organization_id=organization_id)
        try:
            with self._session.begin_nested():
                for key, value in fields.items():
                    setattr(row, key, value)
                self._session.flush()
        except IntegrityError as exc:
            if "display_name" in fields and isinstance(fields["display_name"], str):
                raise OrganizationInvalid(
                    f"organization named {fields['display_name']!r} already exists"
                ) from exc
            raise
        return _to_row(row)

    def archive(
        self,
        *,
        workspace_id: str,
        organization_id: str,
        archived_at: datetime,
    ) -> OrganizationRow:
        row = self._load(workspace_id=workspace_id, organization_id=organization_id)
        if row.archived_at is None:
            row.archived_at = archived_at
            self._session.flush()
        return _to_row(row)

    def _load(self, *, workspace_id: str, organization_id: str) -> Organization:
        row = self._session.scalars(
            select(Organization).where(
                Organization.workspace_id == workspace_id,
                Organization.id == organization_id,
            )
        ).one()
        return row

    def _count(
        self,
        model: type[RateCard] | type[WorkOrder] | type[Quote] | type[VendorInvoice],
        *,
        workspace_id: str,
        organization_column: str,
        organization_id: str,
    ) -> int:
        column = getattr(model, organization_column)
        count = self._session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.workspace_id == workspace_id, column == organization_id)
        )
        return int(count or 0)


class SqlAlchemyClientPortalRepository(ClientPortalRepository):
    """SA-backed read repository for the client portal."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def client_scope(self, *, workspace_id: str, user_id: str) -> ClientPortalScope:
        workspace_org_ids = frozenset(
            org_id
            for org_id in self._session.scalars(
                select(RoleGrant.binding_org_id).where(
                    RoleGrant.workspace_id == workspace_id,
                    RoleGrant.user_id == user_id,
                    RoleGrant.scope_kind == "workspace",
                    RoleGrant.grant_role == "client",
                    RoleGrant.scope_property_id.is_(None),
                    RoleGrant.binding_org_id.is_not(None),
                    # cd-x1xh: live grants only — soft-retired client
                    # grants must not widen the portal scope.
                    RoleGrant.revoked_at.is_(None),
                )
            ).all()
            if org_id is not None
        )

        scoped_properties = self._session.execute(
            select(RoleGrant.scope_property_id, Property.client_org_id)
            .join(Property, Property.id == RoleGrant.scope_property_id)
            .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
            .where(
                RoleGrant.workspace_id == workspace_id,
                RoleGrant.user_id == user_id,
                RoleGrant.scope_kind == "workspace",
                RoleGrant.grant_role == "client",
                RoleGrant.scope_property_id.is_not(None),
                # cd-x1xh: live grants only — soft-retired client
                # grants must not widen the portal scope.
                RoleGrant.revoked_at.is_(None),
                PropertyWorkspace.workspace_id == workspace_id,
                PropertyWorkspace.status == "active",
                Property.deleted_at.is_(None),
                Property.client_org_id.is_not(None),
            )
        ).all()
        property_ids = frozenset(
            property_id
            for property_id, _org_id in scoped_properties
            if property_id is not None
        )
        property_org_ids = frozenset(
            org_id for _property_id, org_id in scoped_properties if org_id is not None
        )
        return ClientPortalScope(
            workspace_org_ids=workspace_org_ids,
            property_ids=property_ids,
            property_org_ids=property_org_ids,
        )

    def list_portfolio(
        self, *, workspace_id: str, scope: ClientPortalScope
    ) -> Sequence[ClientPortalPropertyRow]:
        clauses: list[ColumnElement[bool]] = []
        if scope.workspace_org_ids:
            clauses.append(Property.client_org_id.in_(scope.workspace_org_ids))
        if scope.property_ids:
            clauses.append(Property.id.in_(scope.property_ids))
        stmt = (
            select(Property, Organization.display_name)
            .outerjoin(
                Organization,
                and_(
                    Organization.id == Property.client_org_id,
                    Organization.workspace_id == workspace_id,
                    Organization.archived_at.is_(None),
                ),
            )
            .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
            .where(
                PropertyWorkspace.workspace_id == workspace_id,
                PropertyWorkspace.status == "active",
                Property.deleted_at.is_(None),
                Property.client_org_id.is_not(None),
                or_(*clauses) if clauses else false(),
            )
            .order_by(Property.name.asc(), Property.id.asc())
        )
        rows = self._session.execute(stmt).all()
        return [
            ClientPortalPropertyRow(
                id=row.id,
                organization_id=row.client_org_id or "",
                organization_name=organization_name,
                name=row.name or row.address,
                kind=row.kind,
                address=row.address,
                country=row.country,
                timezone=row.timezone,
                default_currency=row.default_currency,
            )
            for row, organization_name in rows
        ]

    def list_accruals(
        self, *, workspace_id: str, scope: ClientPortalScope
    ) -> Sequence[ClientPortalAccrualRow]:
        stmt = (
            select(WorkOrderShiftAccrual, WorkOrder, Property, Organization)
            .join(
                WorkOrder,
                (WorkOrder.id == WorkOrderShiftAccrual.work_order_id)
                & (WorkOrder.workspace_id == WorkOrderShiftAccrual.workspace_id),
            )
            .join(Property, Property.id == WorkOrder.property_id)
            .join(Organization, Organization.id == WorkOrder.organization_id)
            .where(
                WorkOrderShiftAccrual.workspace_id == workspace_id,
                Organization.workspace_id == workspace_id,
                Property.deleted_at.is_(None),
                _org_or_property_scope(
                    organization_column=WorkOrder.organization_id,
                    property_column=WorkOrder.property_id,
                    scope=scope,
                ),
            )
            .order_by(
                WorkOrderShiftAccrual.created_at.asc(),
                WorkOrder.id.asc(),
                WorkOrderShiftAccrual.id.asc(),
            )
        )
        return [
            ClientPortalAccrualRow(
                work_order_id=work_order.id,
                property_id=work_order.property_id,
                property_name=prop.name or prop.address,
                organization_id=work_order.organization_id,
                currency=org.default_currency,
                hours_decimal=Decimal(accrual.hours_decimal).quantize(Decimal("0.01")),
                accrued_cents=accrual.accrued_cents,
                created_at=_as_utc(accrual.created_at),
            )
            for accrual, work_order, prop, org in self._session.execute(stmt).all()
        ]

    def list_invoices(
        self, *, workspace_id: str, scope: ClientPortalScope
    ) -> Sequence[ClientPortalInvoiceRow]:
        if not scope.workspace_org_ids:
            return []
        rows = self._session.scalars(
            select(VendorInvoice)
            .where(
                VendorInvoice.workspace_id == workspace_id,
                VendorInvoice.vendor_org_id.in_(scope.workspace_org_ids),
            )
            .order_by(
                VendorInvoice.issued_at.desc(),
                VendorInvoice.invoice_number.asc(),
                VendorInvoice.id.asc(),
            )
        ).all()
        return [
            ClientPortalInvoiceRow(
                id=row.id,
                organization_id=row.vendor_org_id,
                invoice_number=row.invoice_number,
                issued_at=row.issued_at,
                due_at=row.due_at,
                total_cents=row.total_cents,
                currency=row.currency,
                status=row.status,
                proof_of_payment_file_ids=tuple(row.proof_of_payment_file_ids),
                pdf_url=None,
            )
            for row in rows
        ]

    def list_quotes(
        self, *, workspace_id: str, scope: ClientPortalScope
    ) -> Sequence[ClientPortalQuoteRow]:
        rows = self._session.scalars(
            select(Quote)
            .where(
                Quote.workspace_id == workspace_id,
                Quote.status != "draft",
                _org_or_property_scope(
                    organization_column=Quote.organization_id,
                    property_column=Quote.property_id,
                    scope=scope,
                ),
            )
            .order_by(Quote.sent_at.desc(), Quote.id.asc())
        ).all()
        return [
            ClientPortalQuoteRow(
                id=row.id,
                organization_id=row.organization_id,
                property_id=row.property_id,
                title=row.title,
                total_cents=row.total_cents,
                currency=row.currency,
                status=row.status,
                sent_at=_as_utc_optional(row.sent_at),
                decided_at=_as_utc_optional(row.decided_at),
            )
            for row in rows
        ]


def _to_quote_row(row: Quote) -> QuoteRow:
    return QuoteRow(
        id=row.id,
        workspace_id=row.workspace_id,
        organization_id=row.organization_id,
        property_id=row.property_id,
        title=row.title,
        body_md=row.body_md,
        lines_json=_quote_lines_json(row.lines_json),
        subtotal_cents=row.subtotal_cents,
        tax_cents=row.tax_cents,
        total_cents=row.total_cents,
        currency=row.currency,
        status=row.status,
        superseded_by_quote_id=row.superseded_by_quote_id,
        sent_at=_as_utc_optional(row.sent_at),
        decided_at=_as_utc_optional(row.decided_at),
    )


def _quote_lines_json(value: Mapping[str, object]) -> QuoteLinesJson:
    schema_version = value.get("schema_version")
    lines = value.get("lines")
    if schema_version != 1 or not isinstance(lines, list):
        raise ValueError("quote.lines_json must be a v1 line payload")
    clean_lines: list[QuoteLine] = []
    for raw_line in lines:
        if not isinstance(raw_line, Mapping):
            raise ValueError("quote.lines_json lines must be objects")
        clean_lines.append(
            QuoteLine(
                kind=str(raw_line["kind"]),
                description=str(raw_line["description"]),
                quantity=_quote_line_quantity(raw_line["quantity"]),
                unit=str(raw_line["unit"]),
                unit_price_cents=int(raw_line["unit_price_cents"]),
                total_cents=int(raw_line["total_cents"]),
            )
        )
    return {"schema_version": 1, "lines": clean_lines}


def _quote_line_quantity(value: object) -> int | float | str:
    if isinstance(value, int | float | str):
        return value
    return str(value)


def _to_rate_card_organization_row(row: Organization) -> RateCardOrganizationRow:
    return RateCardOrganizationRow(
        id=row.id,
        workspace_id=row.workspace_id,
        kind=row.kind,
        default_currency=row.default_currency,
    )


def _to_rate_card_row(row: RateCard) -> RateCardRow:
    return RateCardRow(
        id=row.id,
        workspace_id=row.workspace_id,
        organization_id=row.organization_id,
        label=row.label,
        currency=row.currency,
        rates=dict(row.rates_json),
        active_from=row.active_from,
        active_to=row.active_to,
    )


def _to_work_order_row(row: WorkOrder) -> WorkOrderRow:
    return WorkOrderRow(
        id=row.id,
        workspace_id=row.workspace_id,
        organization_id=row.organization_id,
        property_id=row.property_id,
        title=row.title,
        status=row.status,
        starts_at=_as_utc(row.starts_at),
        ends_at=_as_utc_optional(row.ends_at),
        rate_card_id=row.rate_card_id,
        total_hours_decimal=Decimal(row.total_hours_decimal).quantize(Decimal("0.01")),
        total_cents=row.total_cents,
    )


def _to_work_order_organization_row(row: Organization) -> WorkOrderOrganizationRow:
    return WorkOrderOrganizationRow(
        id=row.id,
        workspace_id=row.workspace_id,
        kind=row.kind,
        default_currency=row.default_currency,
    )


def _to_work_order_property_row(row: Property) -> WorkOrderPropertyRow:
    return WorkOrderPropertyRow(
        id=row.id,
        client_org_id=row.client_org_id,
        default_currency=row.default_currency,
    )


def _to_work_order_rate_card_row(row: RateCard) -> WorkOrderRateCardRow:
    return WorkOrderRateCardRow(
        id=row.id,
        workspace_id=row.workspace_id,
        organization_id=row.organization_id,
        currency=row.currency,
        rates=dict(row.rates_json),
        active_from=row.active_from,
        active_to=row.active_to,
    )


def _to_work_order_shift_row(row: Shift) -> WorkOrderShiftRow:
    return WorkOrderShiftRow(
        id=row.id,
        workspace_id=row.workspace_id,
        starts_at=_as_utc(row.starts_at),
        ends_at=_as_utc_optional(row.ends_at),
        property_id=row.property_id,
    )


def _to_vendor_invoice_organization_row(
    row: Organization,
) -> VendorInvoiceOrganizationRow:
    return VendorInvoiceOrganizationRow(
        id=row.id,
        workspace_id=row.workspace_id,
        kind=row.kind,
        default_currency=row.default_currency,
    )


def _to_vendor_invoice_row(row: VendorInvoice) -> VendorInvoiceRow:
    return VendorInvoiceRow(
        id=row.id,
        workspace_id=row.workspace_id,
        vendor_org_id=row.vendor_org_id,
        invoice_number=row.invoice_number,
        issued_at=row.issued_at,
        due_at=row.due_at,
        total_cents=row.total_cents,
        currency=row.currency,
        status=row.status,
        pdf_blob_hash=row.pdf_blob_hash,
        approved_at=_as_utc_optional(row.approved_at),
        paid_at=_as_utc_optional(row.paid_at),
        payment_method=row.payment_method,
        proof_blob_hash=row.proof_blob_hash,
        proof_of_payment_file_ids=tuple(row.proof_of_payment_file_ids or ()),
        disputed_at=_as_utc_optional(row.disputed_at),
        notes_md=row.notes_md,
    )


class SqlAlchemyRateCardRepository(RateCardRepository):
    """SA-backed rate-card repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def get_organization(
        self, *, workspace_id: str, organization_id: str, for_update: bool = False
    ) -> RateCardOrganizationRow | None:
        stmt = select(Organization).where(
            Organization.workspace_id == workspace_id,
            Organization.id == organization_id,
            Organization.archived_at.is_(None),
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = self._session.scalars(stmt).one_or_none()
        return _to_rate_card_organization_row(row) if row is not None else None

    def insert(
        self,
        *,
        rate_card_id: str,
        workspace_id: str,
        organization_id: str,
        label: str,
        currency: str,
        rates: Mapping[str, int],
        active_from: date,
        active_to: date | None,
    ) -> RateCardRow:
        row = RateCard(
            id=rate_card_id,
            workspace_id=workspace_id,
            organization_id=organization_id,
            label=label,
            currency=currency,
            rates_json=dict(rates),
            active_from=active_from,
            active_to=active_to,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError as exc:
            raise RateCardInvalid(
                "rate card references an unknown organization or duplicate window"
            ) from exc
        return _to_rate_card_row(row)

    def list(self, *, workspace_id: str, organization_id: str) -> Sequence[RateCardRow]:
        rows = self._session.scalars(
            select(RateCard)
            .where(
                RateCard.workspace_id == workspace_id,
                RateCard.organization_id == organization_id,
            )
            .order_by(
                RateCard.active_from.asc(),
                RateCard.label.asc(),
                RateCard.id.asc(),
            )
        ).all()
        return [_to_rate_card_row(row) for row in rows]

    def get(
        self,
        *,
        workspace_id: str,
        organization_id: str,
        rate_card_id: str,
        for_update: bool = False,
    ) -> RateCardRow | None:
        stmt = select(RateCard).where(
            RateCard.workspace_id == workspace_id,
            RateCard.organization_id == organization_id,
            RateCard.id == rate_card_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = self._session.scalars(stmt).one_or_none()
        return _to_rate_card_row(row) if row is not None else None

    def update_fields(
        self,
        *,
        workspace_id: str,
        organization_id: str,
        rate_card_id: str,
        fields: Mapping[str, object | None],
    ) -> RateCardRow:
        row = self._session.scalars(
            select(RateCard).where(
                RateCard.workspace_id == workspace_id,
                RateCard.organization_id == organization_id,
                RateCard.id == rate_card_id,
            )
        ).one()
        try:
            with self._session.begin_nested():
                for key, value in fields.items():
                    setattr(row, key, value)
                self._session.flush()
        except IntegrityError as exc:
            raise RateCardInvalid(
                "rate card references an unknown organization or duplicate window"
            ) from exc
        return _to_rate_card_row(row)


class SqlAlchemyWorkOrderRepository(WorkOrderRepository):
    """SA-backed work-order repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def get_workspace_default_currency(self, *, workspace_id: str) -> str | None:
        return self._session.scalar(
            select(Workspace.default_currency).where(Workspace.id == workspace_id)
        )

    def get_organization(
        self, *, workspace_id: str, organization_id: str, for_update: bool = False
    ) -> WorkOrderOrganizationRow | None:
        stmt = select(Organization).where(
            Organization.workspace_id == workspace_id,
            Organization.id == organization_id,
            Organization.archived_at.is_(None),
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = self._session.scalars(stmt).one_or_none()
        return _to_work_order_organization_row(row) if row is not None else None

    def get_property(
        self, *, workspace_id: str, property_id: str
    ) -> WorkOrderPropertyRow | None:
        row = self._session.scalars(
            select(Property)
            .where(Property.id == property_id, Property.deleted_at.is_(None))
            .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
            .where(
                PropertyWorkspace.workspace_id == workspace_id,
                PropertyWorkspace.status == "active",
            )
        ).one_or_none()
        return _to_work_order_property_row(row) if row is not None else None

    def get_rate_card(
        self, *, workspace_id: str, organization_id: str, rate_card_id: str
    ) -> WorkOrderRateCardRow | None:
        row = self._session.scalars(
            select(RateCard).where(
                RateCard.workspace_id == workspace_id,
                RateCard.organization_id == organization_id,
                RateCard.id == rate_card_id,
            )
        ).one_or_none()
        return _to_work_order_rate_card_row(row) if row is not None else None

    def insert(
        self,
        *,
        work_order_id: str,
        workspace_id: str,
        organization_id: str,
        property_id: str,
        title: str,
        status: str,
        starts_at: datetime,
        ends_at: datetime | None,
        rate_card_id: str | None,
    ) -> WorkOrderRow:
        row = WorkOrder(
            id=work_order_id,
            workspace_id=workspace_id,
            organization_id=organization_id,
            property_id=property_id,
            title=title,
            status=status,
            starts_at=starts_at,
            ends_at=ends_at,
            rate_card_id=rate_card_id,
            total_hours_decimal=Decimal("0.00"),
            total_cents=0,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError as exc:
            raise WorkOrderInvalid(
                "work order references an unknown billing artifact"
            ) from exc
        return _to_work_order_row(row)

    def get(
        self, *, workspace_id: str, work_order_id: str, for_update: bool = False
    ) -> WorkOrderRow | None:
        stmt = select(WorkOrder).where(
            WorkOrder.workspace_id == workspace_id,
            WorkOrder.id == work_order_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = self._session.scalars(stmt).one_or_none()
        return _to_work_order_row(row) if row is not None else None

    def list(
        self,
        *,
        workspace_id: str,
        organization_id: str | None,
        property_id: str | None,
        status: str | None,
    ) -> Sequence[WorkOrderRow]:
        stmt = (
            select(WorkOrder)
            .where(WorkOrder.workspace_id == workspace_id)
            .order_by(WorkOrder.starts_at.desc(), WorkOrder.id.asc())
        )
        if organization_id is not None:
            stmt = stmt.where(WorkOrder.organization_id == organization_id)
        if property_id is not None:
            stmt = stmt.where(WorkOrder.property_id == property_id)
        if status is not None:
            stmt = stmt.where(WorkOrder.status == status)
        return [_to_work_order_row(row) for row in self._session.scalars(stmt).all()]

    def update_fields(
        self,
        *,
        workspace_id: str,
        work_order_id: str,
        fields: Mapping[str, object | None],
    ) -> WorkOrderRow:
        row = self._session.scalars(
            select(WorkOrder).where(
                WorkOrder.workspace_id == workspace_id,
                WorkOrder.id == work_order_id,
            )
        ).one()
        try:
            with self._session.begin_nested():
                for key, value in fields.items():
                    setattr(row, key, value)
                self._session.flush()
        except IntegrityError as exc:
            raise WorkOrderInvalid(
                "work order references an unknown billing artifact"
            ) from exc
        return _to_work_order_row(row)

    def get_shift(
        self, *, workspace_id: str, shift_id: str
    ) -> WorkOrderShiftRow | None:
        row = self._session.scalars(
            select(Shift).where(
                Shift.workspace_id == workspace_id,
                Shift.id == shift_id,
            )
        ).one_or_none()
        return _to_work_order_shift_row(row) if row is not None else None

    def open_for_property(
        self, *, workspace_id: str, property_id: str, for_update: bool = False
    ) -> Sequence[WorkOrderRow]:
        stmt = select(WorkOrder).where(
            WorkOrder.workspace_id == workspace_id,
            WorkOrder.property_id == property_id,
            WorkOrder.status == "in_progress",
        )
        if for_update:
            stmt = stmt.with_for_update()
        rows = self._session.scalars(stmt).all()
        return [_to_work_order_row(row) for row in rows]

    def append_shift_accrual(
        self,
        *,
        accrual_id: str,
        workspace_id: str,
        work_order_id: str,
        shift_id: str,
        hours_decimal: Decimal,
        hourly_rate_cents: int,
        accrued_cents: int,
        created_at: datetime,
    ) -> ShiftAccrualRow | None:
        row = WorkOrderShiftAccrual(
            id=accrual_id,
            workspace_id=workspace_id,
            work_order_id=work_order_id,
            shift_id=shift_id,
            hours_decimal=hours_decimal,
            hourly_rate_cents=hourly_rate_cents,
            accrued_cents=accrued_cents,
            created_at=created_at,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError:
            return None

        self._session.execute(
            update(WorkOrder)
            .where(
                WorkOrder.workspace_id == workspace_id,
                WorkOrder.id == work_order_id,
            )
            .values(
                total_hours_decimal=WorkOrder.total_hours_decimal + hours_decimal,
                total_cents=WorkOrder.total_cents + accrued_cents,
            )
        )
        self._session.flush()
        return ShiftAccrualRow(
            id=row.id,
            workspace_id=row.workspace_id,
            work_order_id=row.work_order_id,
            shift_id=row.shift_id,
            hours_decimal=Decimal(row.hours_decimal).quantize(Decimal("0.01")),
            hourly_rate_cents=row.hourly_rate_cents,
            accrued_cents=row.accrued_cents,
            created_at=_as_utc(row.created_at),
        )


class SqlAlchemyQuoteRepository(QuoteRepository):
    """SA-backed quote repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def get_workspace_default_currency(self, *, workspace_id: str) -> str | None:
        return self._session.scalar(
            select(Workspace.default_currency).where(Workspace.id == workspace_id)
        )

    def organization_contact_email(
        self, *, workspace_id: str, organization_id: str
    ) -> str | None:
        return self._session.scalar(
            select(Organization.contact_email).where(
                Organization.workspace_id == workspace_id,
                Organization.id == organization_id,
                Organization.archived_at.is_(None),
            )
        )

    def insert(
        self,
        *,
        quote_id: str,
        workspace_id: str,
        organization_id: str,
        property_id: str,
        title: str,
        body_md: str,
        lines_json: QuoteLinesJson,
        subtotal_cents: int,
        tax_cents: int,
        total_cents: int,
        currency: str,
        status: str,
        superseded_by_quote_id: str | None = None,
    ) -> QuoteRow:
        row = Quote(
            id=quote_id,
            workspace_id=workspace_id,
            organization_id=organization_id,
            property_id=property_id,
            title=title,
            body_md=body_md,
            lines_json=lines_json,
            subtotal_cents=subtotal_cents,
            tax_cents=tax_cents,
            total_cents=total_cents,
            currency=currency,
            status=status,
            superseded_by_quote_id=superseded_by_quote_id,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError as exc:
            raise QuoteInvalid("quote references an unknown billing artifact") from exc
        return _to_quote_row(row)

    def get(
        self, *, workspace_id: str, quote_id: str, for_update: bool = False
    ) -> QuoteRow | None:
        stmt = select(Quote).where(
            Quote.workspace_id == workspace_id,
            Quote.id == quote_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = self._session.scalars(stmt).one_or_none()
        return _to_quote_row(row) if row is not None else None

    def get_public(self, *, quote_id: str, for_update: bool = False) -> QuoteRow | None:
        stmt = select(Quote).where(Quote.id == quote_id)
        if for_update:
            stmt = stmt.with_for_update()
        # justification: public quote-token routes have no workspace request context.
        with tenant_agnostic():
            row = self._session.scalars(stmt).one_or_none()
        return _to_quote_row(row) if row is not None else None

    def list(
        self,
        *,
        workspace_id: str,
        organization_id: str | None,
        property_id: str | None,
        status: str | None,
    ) -> Sequence[QuoteRow]:
        stmt = (
            select(Quote)
            .where(Quote.workspace_id == workspace_id)
            .order_by(Quote.id.asc())
        )
        if organization_id is not None:
            stmt = stmt.where(Quote.organization_id == organization_id)
        if property_id is not None:
            stmt = stmt.where(Quote.property_id == property_id)
        if status is not None:
            stmt = stmt.where(Quote.status == status)
        return [_to_quote_row(row) for row in self._session.scalars(stmt).all()]

    def update_fields(
        self,
        *,
        workspace_id: str,
        quote_id: str,
        fields: Mapping[str, object | None],
    ) -> QuoteRow:
        # justification: public quote-token decisions update by explicit workspace_id.
        with tenant_agnostic():
            row = self._session.scalars(
                select(Quote).where(
                    Quote.workspace_id == workspace_id, Quote.id == quote_id
                )
            ).one()
        try:
            with self._session.begin_nested():
                for key, value in fields.items():
                    setattr(row, key, value)
                self._session.flush()
        except IntegrityError as exc:
            raise QuoteInvalid("quote references an unknown billing artifact") from exc
        return _to_quote_row(row)


class SqlAlchemyVendorInvoiceRepository(VendorInvoiceRepository):
    """SA-backed vendor-invoice repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def get_organization(
        self, *, workspace_id: str, organization_id: str, for_update: bool = False
    ) -> VendorInvoiceOrganizationRow | None:
        stmt = select(Organization).where(
            Organization.workspace_id == workspace_id,
            Organization.id == organization_id,
            Organization.archived_at.is_(None),
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = self._session.scalars(stmt).one_or_none()
        return _to_vendor_invoice_organization_row(row) if row is not None else None

    def insert(
        self,
        *,
        invoice_id: str,
        workspace_id: str,
        vendor_org_id: str,
        invoice_number: str,
        issued_at: date,
        due_at: date | None,
        total_cents: int,
        currency: str,
        status: str,
        notes_md: str | None,
    ) -> VendorInvoiceRow:
        row = VendorInvoice(
            id=invoice_id,
            workspace_id=workspace_id,
            vendor_org_id=vendor_org_id,
            invoice_number=invoice_number,
            issued_at=issued_at,
            due_at=due_at,
            total_cents=total_cents,
            currency=currency,
            status=status,
            pdf_blob_hash=None,
            approved_at=None,
            paid_at=None,
            payment_method=None,
            proof_blob_hash=None,
            proof_of_payment_file_ids=[],
            disputed_at=None,
            notes_md=notes_md,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError as exc:
            raise VendorInvoiceInvalid(
                "vendor invoice references an unknown vendor organization "
                "or duplicates invoice_number for that vendor"
            ) from exc
        return _to_vendor_invoice_row(row)

    def get(
        self, *, workspace_id: str, invoice_id: str, for_update: bool = False
    ) -> VendorInvoiceRow | None:
        stmt = select(VendorInvoice).where(
            VendorInvoice.workspace_id == workspace_id,
            VendorInvoice.id == invoice_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = self._session.scalars(stmt).one_or_none()
        return _to_vendor_invoice_row(row) if row is not None else None

    def list(
        self,
        *,
        workspace_id: str,
        vendor_org_id: str | None,
        status: str | None,
    ) -> Sequence[VendorInvoiceRow]:
        stmt = (
            select(VendorInvoice)
            .where(VendorInvoice.workspace_id == workspace_id)
            .order_by(
                VendorInvoice.issued_at.desc(),
                VendorInvoice.invoice_number.asc(),
                VendorInvoice.id.asc(),
            )
        )
        if vendor_org_id is not None:
            stmt = stmt.where(VendorInvoice.vendor_org_id == vendor_org_id)
        if status is not None:
            stmt = stmt.where(VendorInvoice.status == status)
        return [
            _to_vendor_invoice_row(row) for row in self._session.scalars(stmt).all()
        ]

    def update_fields(
        self,
        *,
        workspace_id: str,
        invoice_id: str,
        fields: Mapping[str, object | None],
    ) -> VendorInvoiceRow:
        row = self._session.scalars(
            select(VendorInvoice).where(
                VendorInvoice.workspace_id == workspace_id,
                VendorInvoice.id == invoice_id,
            )
        ).one()
        try:
            with self._session.begin_nested():
                for key, value in fields.items():
                    setattr(row, key, value)
                self._session.flush()
        except IntegrityError as exc:
            raise VendorInvoiceInvalid(
                "vendor invoice references an unknown vendor organization "
                "or duplicates invoice_number for that vendor"
            ) from exc
        return _to_vendor_invoice_row(row)
