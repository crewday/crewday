"""SQLAlchemy repositories for billing domain services."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.billing.models import (
    Organization,
    Quote,
    RateCard,
    VendorInvoice,
    WorkOrder,
)
from app.adapters.db.workspace.models import Workspace
from app.domain.billing.organizations import (
    OrganizationArtifactCounts,
    OrganizationInvalid,
    OrganizationRepository,
    OrganizationRow,
)
from app.domain.billing.quotes import QuoteInvalid, QuoteRepository, QuoteRow
from app.domain.billing.rate_cards import (
    RateCardInvalid,
    RateCardOrganizationRow,
    RateCardRepository,
    RateCardRow,
)

__all__ = [
    "SqlAlchemyOrganizationRepository",
    "SqlAlchemyQuoteRepository",
    "SqlAlchemyRateCardRepository",
]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_utc_optional(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _as_utc(value)


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


def _to_quote_row(row: Quote) -> QuoteRow:
    return QuoteRow(
        id=row.id,
        workspace_id=row.workspace_id,
        organization_id=row.organization_id,
        property_id=row.property_id,
        title=row.title,
        body_md=row.body_md,
        total_cents=row.total_cents,
        currency=row.currency,
        status=row.status,
        sent_at=_as_utc_optional(row.sent_at),
        decided_at=_as_utc_optional(row.decided_at),
    )


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

    def list(
        self, *, workspace_id: str, organization_id: str
    ) -> Sequence[RateCardRow]:
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
        total_cents: int,
        currency: str,
        status: str,
    ) -> QuoteRow:
        row = Quote(
            id=quote_id,
            workspace_id=workspace_id,
            organization_id=organization_id,
            property_id=property_id,
            title=title,
            body_md=body_md,
            total_cents=total_cents,
            currency=currency,
            status=status,
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
