"""Unit tests for :mod:`app.adapters.db.billing.models`."""

from __future__ import annotations

import importlib
import pkgutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, cast

import pytest
from sqlalchemy import CheckConstraint, Engine, Index, create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.base import Base
from app.adapters.db.billing import (
    Organization,
    Quote,
    RateCard,
    VendorInvoice,
    WorkOrder,
)
from app.adapters.db.places import Property, PropertyWorkspace
from app.adapters.db.workspace import Workspace
from app.tenancy import registry

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)
_TODAY = date(2026, 4, 29)
_TOMORROW = date(2026, 4, 30)
_WORKSPACE_ID = "01HWA00000000000000000WSPA"
_OTHER_WORKSPACE_ID = "01HWA00000000000000000WSPB"
_PROPERTY_ID = "01HWA00000000000000000PROA"
_OTHER_PROPERTY_ID = "01HWA00000000000000000PROB"
_ORG_ID = "01HWA00000000000000000ORGA"
_OTHER_ORG_ID = "01HWA00000000000000000ORGB"
_RATE_CARD_ID = "01HWA00000000000000000RCAA"
_OTHER_RATE_CARD_ID = "01HWA00000000000000000RCAB"
_BILLING_TABLES = (
    "organization",
    "rate_card",
    "work_order",
    "quote",
    "vendor_invoice",
)


class _TableArgsCarrier(Protocol):
    __table_args__: tuple[object, ...]


def _load_all_models() -> None:
    """Import every DB model module so FK targets are present."""
    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        models_name = f"{modinfo.name}.models"
        try:
            importlib.import_module(models_name)
        except ModuleNotFoundError as exc:
            if exc.name == models_name:
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    _load_all_models()
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(
        dbapi_connection: object, _connection_record: object
    ) -> None:
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as sess:
        yield sess
        sess.rollback()


def _seed_workspace(
    session: Session, *, workspace_id: str = _WORKSPACE_ID, slug: str = "billing"
) -> Workspace:
    ws = Workspace(
        id=workspace_id,
        slug=slug,
        name="Billing",
        plan="free",
        quota_json={},
        created_at=_PINNED,
    )
    session.add(ws)
    session.flush()
    return ws


def _seed_property(
    session: Session,
    *,
    property_id: str = _PROPERTY_ID,
    workspace_id: str = _WORKSPACE_ID,
) -> Property:
    prop = Property(
        id=property_id,
        address="12 Chemin des Oliviers, Antibes",
        timezone="Europe/Paris",
        tags_json=[],
        created_at=_PINNED,
    )
    session.add(prop)
    session.flush()
    session.add(
        PropertyWorkspace(
            property_id=property_id,
            workspace_id=workspace_id,
            label="Billing",
            membership_role="owner_workspace",
            status="active",
            created_at=_PINNED,
        )
    )
    session.flush()
    return prop


def _seed_organization(
    session: Session,
    *,
    organization_id: str = _ORG_ID,
    workspace_id: str = _WORKSPACE_ID,
    display_name: str = "CleanCo SARL",
    kind: str = "mixed",
) -> Organization:
    org = Organization(
        id=organization_id,
        workspace_id=workspace_id,
        kind=kind,
        display_name=display_name,
        billing_address={"line1": "1 Rue Example", "country": "FR"},
        tax_id="FR123",
        default_currency="EUR",
        contact_email="billing@example.test",
        contact_phone="+33123456789",
        notes_md="Preferred vendor.",
        created_at=_PINNED,
    )
    session.add(org)
    session.flush()
    return org


def _seed_rate_card(
    session: Session,
    *,
    rate_card_id: str = _RATE_CARD_ID,
    workspace_id: str = _WORKSPACE_ID,
    organization_id: str = _ORG_ID,
) -> RateCard:
    rate_card = RateCard(
        id=rate_card_id,
        workspace_id=workspace_id,
        organization_id=organization_id,
        label="Standard 2026",
        currency="EUR",
        rates_json={"maid": 2500, "driver": 3000},
        active_from=_TODAY,
        active_to=None,
    )
    session.add(rate_card)
    session.flush()
    return rate_card


def _check_sql(model: object, name: str) -> str:
    table_args = cast(_TableArgsCarrier, model).__table_args__
    checks = [
        constraint
        for constraint in table_args
        if isinstance(constraint, CheckConstraint)
        and str(getattr(constraint, "name", "")).endswith(name)
    ]
    assert len(checks) == 1
    return str(checks[0].sqltext)


def _index_columns(model: object, name: str) -> list[str]:
    table_args = cast(_TableArgsCarrier, model).__table_args__
    indexes = [
        index for index in table_args if isinstance(index, Index) and index.name == name
    ]
    assert len(indexes) == 1
    return [column.name for column in indexes[0].columns]


def test_package_exports_and_tenancy_registration() -> None:
    assert Organization.__tablename__ == "organization"
    assert RateCard.__tablename__ == "rate_card"
    assert WorkOrder.__tablename__ == "work_order"
    assert Quote.__tablename__ == "quote"
    assert VendorInvoice.__tablename__ == "vendor_invoice"

    snapshot = registry.scoped_tables()
    try:
        registry._reset_for_tests()
        for table in _BILLING_TABLES:
            registry.register(table)
        for table in _BILLING_TABLES:
            assert registry.is_scoped(table) is True
    finally:
        registry._reset_for_tests()
        for table in snapshot:
            registry.register(table)


def test_model_construction() -> None:
    org = Organization(
        id=_ORG_ID,
        workspace_id=_WORKSPACE_ID,
        kind="client",
        display_name="Dupont Family",
        billing_address={"line1": "12 Rue Example", "country": "FR"},
        default_currency="EUR",
        created_at=_PINNED,
    )
    rate_card = RateCard(
        id=_RATE_CARD_ID,
        workspace_id=_WORKSPACE_ID,
        organization_id=_ORG_ID,
        label="Standard",
        currency="EUR",
        rates_json={"maid": 2500},
        active_from=_TODAY,
    )
    work_order = WorkOrder(
        id="01HWA00000000000000000WORA",
        workspace_id=_WORKSPACE_ID,
        organization_id=_ORG_ID,
        property_id=_PROPERTY_ID,
        title="Replace pool pump seal",
        status="draft",
        starts_at=_PINNED,
        rate_card_id=_RATE_CARD_ID,
        total_hours_decimal=Decimal("0.00"),
        total_cents=0,
    )
    quote = Quote(
        id="01HWA00000000000000000QUOA",
        workspace_id=_WORKSPACE_ID,
        organization_id=_ORG_ID,
        property_id=_PROPERTY_ID,
        title="Pool repair quote",
        body_md="Parts and labour.",
        total_cents=12500,
        currency="EUR",
        status="draft",
    )
    invoice = VendorInvoice(
        id="01HWA00000000000000000VINA",
        workspace_id=_WORKSPACE_ID,
        vendor_org_id=_ORG_ID,
        invoice_number="INV-2026-001",
        issued_at=_TODAY,
        total_cents=12500,
        currency="EUR",
        status="received",
    )

    assert org.kind == "client"
    assert org.tax_id is None
    assert rate_card.active_to is None
    assert work_order.ends_at is None
    assert quote.sent_at is None
    assert quote.decided_at is None
    assert invoice.due_at is None


def test_check_constraints_are_declared() -> None:
    assert "client" in _check_sql(Organization, "kind")
    assert "LENGTH(default_currency)" in _check_sql(
        Organization, "default_currency_length"
    )
    assert "LENGTH(currency)" in _check_sql(RateCard, "currency_length")
    assert "active_to" in _check_sql(RateCard, "active_range")
    assert "sent" in _check_sql(WorkOrder, "status")
    assert "total_hours_decimal" in _check_sql(WorkOrder, "total_hours_decimal_nonneg")
    assert "total_cents" in _check_sql(WorkOrder, "total_cents_nonneg")
    assert "accepted" in _check_sql(Quote, "status")
    assert "LENGTH(currency)" in _check_sql(Quote, "currency_length")
    assert "approved" in _check_sql(VendorInvoice, "status")
    assert "due_at" in _check_sql(VendorInvoice, "due_range")
    assert "total_cents" in _check_sql(VendorInvoice, "total_cents_nonneg")


def test_indexes_are_declared() -> None:
    assert _index_columns(Organization, "ix_organization_workspace_kind") == [
        "workspace_id",
        "kind",
    ]
    assert _index_columns(RateCard, "ix_rate_card_workspace_organization_active") == [
        "workspace_id",
        "organization_id",
        "active_from",
    ]
    assert _index_columns(WorkOrder, "ix_work_order_workspace_status") == [
        "workspace_id",
        "status",
    ]
    assert _index_columns(WorkOrder, "ix_work_order_workspace_property_status") == [
        "workspace_id",
        "property_id",
        "status",
    ]
    assert _index_columns(Quote, "ix_quote_workspace_organization_status") == [
        "workspace_id",
        "organization_id",
        "status",
    ]
    assert _index_columns(
        VendorInvoice, "ix_vendor_invoice_workspace_vendor_status"
    ) == ["workspace_id", "vendor_org_id", "status"]


def test_crud_and_status_transitions(session: Session) -> None:
    _seed_workspace(session)
    _seed_property(session)
    _seed_organization(session)
    _seed_rate_card(session)

    work_order = WorkOrder(
        id="01HWA00000000000000000WORB",
        workspace_id=_WORKSPACE_ID,
        organization_id=_ORG_ID,
        property_id=_PROPERTY_ID,
        title="Replace pool pump seal",
        status="draft",
        starts_at=_PINNED,
        ends_at=_LATER,
        rate_card_id=_RATE_CARD_ID,
        total_hours_decimal=Decimal("2.50"),
        total_cents=12500,
    )
    quote = Quote(
        id="01HWA00000000000000000QUOB",
        workspace_id=_WORKSPACE_ID,
        organization_id=_ORG_ID,
        property_id=_PROPERTY_ID,
        title="Pool repair quote",
        body_md="Parts and labour.",
        total_cents=12500,
        currency="EUR",
        status="draft",
    )
    invoice = VendorInvoice(
        id="01HWA00000000000000000VINB",
        workspace_id=_WORKSPACE_ID,
        vendor_org_id=_ORG_ID,
        invoice_number="INV-2026-002",
        issued_at=_TODAY,
        due_at=_TOMORROW,
        total_cents=12500,
        currency="EUR",
        status="received",
    )
    session.add_all([work_order, quote, invoice])
    session.flush()

    work_order.status = "sent"
    quote.status = "sent"
    quote.sent_at = _PINNED
    invoice.status = "approved"
    session.flush()

    work_order.status = "in_progress"
    quote.status = "accepted"
    quote.decided_at = _LATER
    invoice.status = "paid"
    session.flush()

    assert session.scalars(select(WorkOrder)).one().status == "in_progress"
    assert session.scalars(select(Quote)).one().status == "accepted"
    assert session.scalars(select(VendorInvoice)).one().status == "paid"


def test_child_rows_reject_cross_workspace_parent_links(session: Session) -> None:
    _seed_workspace(session)
    _seed_property(session)
    _seed_organization(session)
    _seed_rate_card(session)
    _seed_workspace(session, workspace_id=_OTHER_WORKSPACE_ID, slug="billing-other")
    _seed_property(
        session,
        property_id=_OTHER_PROPERTY_ID,
        workspace_id=_OTHER_WORKSPACE_ID,
    )
    _seed_organization(
        session,
        organization_id=_OTHER_ORG_ID,
        workspace_id=_OTHER_WORKSPACE_ID,
        display_name="Other CleanCo",
    )
    _seed_rate_card(
        session,
        rate_card_id=_OTHER_RATE_CARD_ID,
        workspace_id=_OTHER_WORKSPACE_ID,
        organization_id=_OTHER_ORG_ID,
    )
    session.commit()

    bad_rows: list[object] = [
        RateCard(
            id="01HWA00000000000000000RCAC",
            workspace_id=_WORKSPACE_ID,
            organization_id=_OTHER_ORG_ID,
            label="Cross org",
            currency="EUR",
            rates_json={"maid": 2500},
            active_from=_TODAY,
        ),
        WorkOrder(
            id="01HWA00000000000000000WORD",
            workspace_id=_WORKSPACE_ID,
            organization_id=_OTHER_ORG_ID,
            property_id=_PROPERTY_ID,
            title="Cross org work order",
            status="draft",
            starts_at=_PINNED,
            total_hours_decimal=Decimal("1.00"),
            total_cents=1000,
        ),
        WorkOrder(
            id="01HWA00000000000000000WORE",
            workspace_id=_WORKSPACE_ID,
            organization_id=_ORG_ID,
            property_id=_OTHER_PROPERTY_ID,
            title="Cross property work order",
            status="draft",
            starts_at=_PINNED,
            total_hours_decimal=Decimal("1.00"),
            total_cents=1000,
        ),
        WorkOrder(
            id="01HWA00000000000000000WORF",
            workspace_id=_WORKSPACE_ID,
            organization_id=_ORG_ID,
            property_id=_PROPERTY_ID,
            title="Cross rate card work order",
            status="draft",
            starts_at=_PINNED,
            rate_card_id=_OTHER_RATE_CARD_ID,
            total_hours_decimal=Decimal("1.00"),
            total_cents=1000,
        ),
        Quote(
            id="01HWA00000000000000000QUOD",
            workspace_id=_WORKSPACE_ID,
            organization_id=_ORG_ID,
            property_id=_OTHER_PROPERTY_ID,
            title="Cross property quote",
            body_md="Bad",
            total_cents=1000,
            currency="EUR",
            status="draft",
        ),
        VendorInvoice(
            id="01HWA00000000000000000VIND",
            workspace_id=_WORKSPACE_ID,
            vendor_org_id=_OTHER_ORG_ID,
            invoice_number="INV-CROSS",
            issued_at=_TODAY,
            total_cents=1000,
            currency="EUR",
            status="received",
        ),
    ]

    for row in bad_rows:
        session.add(row)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


@pytest.mark.parametrize(
    ("table", "bad_kwargs"),
    [
        ("organization", {"kind": "supplier"}),
        ("organization", {"default_currency": "EURO"}),
        ("rate_card", {"currency": "EURO"}),
        ("rate_card", {"active_to": _TODAY}),
        ("work_order", {"status": "accepted"}),
        ("work_order", {"total_hours_decimal": Decimal("-0.01")}),
        ("work_order", {"total_cents": -1}),
        ("quote", {"status": "submitted"}),
        ("quote", {"currency": "EURO"}),
        ("quote", {"total_cents": -1}),
        ("vendor_invoice", {"status": "submitted"}),
        ("vendor_invoice", {"currency": "EURO"}),
        ("vendor_invoice", {"total_cents": -1}),
        ("vendor_invoice", {"due_at": date(2026, 4, 28)}),
    ],
)
def test_db_check_constraints_reject_invalid_values(
    session: Session, table: str, bad_kwargs: dict[str, object]
) -> None:
    _seed_workspace(session)
    _seed_property(session)
    _seed_organization(session)
    _seed_rate_card(session)

    row: object
    if table == "organization":
        organization_kwargs: dict[str, object] = {
            "id": "01HWA00000000000000000ORGB",
            "workspace_id": _WORKSPACE_ID,
            "kind": "client",
            "display_name": "Bad Org",
            "billing_address": {},
            "default_currency": "EUR",
            "created_at": _PINNED,
        }
        organization_kwargs.update(bad_kwargs)
        row = Organization(**organization_kwargs)
    elif table == "rate_card":
        rate_card_kwargs: dict[str, object] = {
            "id": "01HWA00000000000000000RCAB",
            "workspace_id": _WORKSPACE_ID,
            "organization_id": _ORG_ID,
            "label": "Bad Card",
            "currency": "EUR",
            "rates_json": {"maid": 2500},
            "active_from": _TODAY,
        }
        rate_card_kwargs.update(bad_kwargs)
        row = RateCard(**rate_card_kwargs)
    elif table == "work_order":
        work_order_kwargs: dict[str, object] = {
            "id": "01HWA00000000000000000WORC",
            "workspace_id": _WORKSPACE_ID,
            "organization_id": _ORG_ID,
            "property_id": _PROPERTY_ID,
            "title": "Bad work order",
            "status": "draft",
            "starts_at": _PINNED,
            "total_hours_decimal": Decimal("1.00"),
            "total_cents": 1000,
        }
        work_order_kwargs.update(bad_kwargs)
        row = WorkOrder(**work_order_kwargs)
    elif table == "quote":
        quote_kwargs: dict[str, object] = {
            "id": "01HWA00000000000000000QUOC",
            "workspace_id": _WORKSPACE_ID,
            "organization_id": _ORG_ID,
            "property_id": _PROPERTY_ID,
            "title": "Bad quote",
            "body_md": "Bad",
            "total_cents": 1000,
            "currency": "EUR",
            "status": "draft",
        }
        quote_kwargs.update(bad_kwargs)
        row = Quote(**quote_kwargs)
    else:
        vendor_invoice_kwargs: dict[str, object] = {
            "id": "01HWA00000000000000000VINC",
            "workspace_id": _WORKSPACE_ID,
            "vendor_org_id": _ORG_ID,
            "invoice_number": "INV-BAD",
            "issued_at": _TODAY,
            "total_cents": 1000,
            "currency": "EUR",
            "status": "received",
        }
        vendor_invoice_kwargs.update(bad_kwargs)
        row = VendorInvoice(**vendor_invoice_kwargs)

    session.add(row)
    with pytest.raises(IntegrityError):
        session.flush()
