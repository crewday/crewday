"""Unit tests for billing organization CRUD."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.billing.models import (
    Organization,
    Quote,
    RateCard,
    VendorInvoice,
    WorkOrder,
)
from app.adapters.db.billing.repositories import SqlAlchemyOrganizationRepository
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.billing.organizations import (
    OrganizationCreate,
    OrganizationInvalid,
    OrganizationNotFound,
    OrganizationPatch,
    OrganizationService,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
_TODAY = date(2026, 4, 29)


def _load_all_models() -> None:
    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def _seed_workspace(s: Session, *, default_currency: str = "EUR") -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"billing-{workspace_id[-6:].lower()}",
            name="Billing",
            plan="free",
            quota_json={},
            settings_json={},
            default_currency=default_currency,
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _ctx(workspace_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="billing",
        actor_id=new_ulid(),
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _service(ctx: WorkspaceContext) -> OrganizationService:
    return OrganizationService(ctx, clock=FrozenClock(_PINNED))


def _create_org(
    s: Session,
    ctx: WorkspaceContext,
    *,
    kind: str = "mixed",
    display_name: str = "CleanCo SARL",
) -> str:
    view = _service(ctx).create(
        SqlAlchemyOrganizationRepository(s),
        OrganizationCreate(kind=kind, display_name=display_name),
    )
    return view.id


def _seed_rate_card(s: Session, *, workspace_id: str, organization_id: str) -> None:
    s.add(
        RateCard(
            id=new_ulid(),
            workspace_id=workspace_id,
            organization_id=organization_id,
            label="Standard",
            currency="EUR",
            rates_json={"maid": 2500},
            active_from=_TODAY,
        )
    )
    s.flush()


def _seed_vendor_invoice(
    s: Session, *, workspace_id: str, organization_id: str
) -> None:
    s.add(
        VendorInvoice(
            id=new_ulid(),
            workspace_id=workspace_id,
            vendor_org_id=organization_id,
            invoice_number=f"INV-{organization_id[-6:]}",
            issued_at=_TODAY,
            total_cents=1000,
            currency="EUR",
            status="received",
        )
    )
    s.flush()


def _seed_property(s: Session, *, workspace_id: str) -> str:
    property_id = new_ulid()
    s.add(
        Property(
            id=property_id,
            name="Billing Villa",
            kind="vacation",
            address="1 Billing Way",
            address_json={"country": "FR"},
            country="FR",
            timezone="Europe/Paris",
            lat=None,
            lon=None,
            client_org_id=None,
            owner_user_id=None,
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
            deleted_at=None,
        )
    )
    s.add(
        PropertyWorkspace(
            property_id=property_id,
            workspace_id=workspace_id,
            label="Billing Villa",
            membership_role="owner_workspace",
            share_guest_identity=False,
            status="active",
            created_at=_PINNED,
        )
    )
    s.flush()
    return property_id


def test_create_defaults_currency_from_workspace_and_audits(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _seed_workspace(s, default_currency="gbp")
        ctx = _ctx(workspace_id)

        view = _service(ctx).create(
            SqlAlchemyOrganizationRepository(s),
            OrganizationCreate(
                kind="client",
                display_name="  Dupont Family  ",
                billing_address={"country": "FR"},
            ),
        )

        assert view.kind == "client"
        assert view.display_name == "Dupont Family"
        assert view.default_currency == "GBP"
        assert view.archived_at is None

        audit = s.scalar(select(AuditLog))
        assert audit is not None
        assert audit.action == "billing.organization.created"
        assert audit.entity_id == view.id


def test_list_filters_by_kind_search_and_archived_state(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _seed_workspace(s)
        ctx = _ctx(workspace_id)
        repo = SqlAlchemyOrganizationRepository(s)
        service = _service(ctx)
        alpha = service.create(
            repo,
            OrganizationCreate(kind="client", display_name="Alpha Family"),
        )
        service.create(repo, OrganizationCreate(kind="vendor", display_name="Beta Co"))
        service.create(
            repo,
            OrganizationCreate(kind="client", display_name="Gamma Family"),
        )
        service.archive(repo, alpha.id)

        visible = service.list(repo, kind="client", search="family")
        assert [org.display_name for org in visible] == ["Gamma Family"]

        archived = service.list(
            repo,
            kind="client",
            search="family",
            include_archived=True,
        )
        assert [org.display_name for org in archived] == [
            "Alpha Family",
            "Gamma Family",
        ]


def test_update_validates_kind_transitions_against_artifacts(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _seed_workspace(s)
        ctx = _ctx(workspace_id)
        repo = SqlAlchemyOrganizationRepository(s)
        client_artifact_org = _create_org(
            s,
            ctx,
            kind="mixed",
            display_name="Client Artifact Org",
        )
        vendor_artifact_org = _create_org(
            s,
            ctx,
            kind="mixed",
            display_name="Vendor Artifact Org",
        )
        _seed_rate_card(
            s,
            workspace_id=workspace_id,
            organization_id=client_artifact_org,
        )
        _seed_vendor_invoice(
            s,
            workspace_id=workspace_id,
            organization_id=vendor_artifact_org,
        )

        with pytest.raises(OrganizationInvalid, match="cannot become vendor"):
            _service(ctx).update(
                repo,
                client_artifact_org,
                OrganizationPatch({"kind": "vendor"}),
            )
        with pytest.raises(OrganizationInvalid, match="cannot become client"):
            _service(ctx).update(
                repo,
                vendor_artifact_org,
                OrganizationPatch({"kind": "client"}),
            )


def test_update_validates_work_order_and_quote_as_client_artifacts(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _seed_workspace(s)
        property_id = _seed_property(s, workspace_id=workspace_id)
        ctx = _ctx(workspace_id)
        repo = SqlAlchemyOrganizationRepository(s)
        work_order_org = _create_org(
            s,
            ctx,
            kind="mixed",
            display_name="Work Order Org",
        )
        quote_org = _create_org(
            s,
            ctx,
            kind="mixed",
            display_name="Quote Org",
        )
        s.add(
            WorkOrder(
                id=new_ulid(),
                workspace_id=workspace_id,
                organization_id=work_order_org,
                property_id=property_id,
                title="Deep clean",
                status="draft",
                starts_at=_PINNED,
                ends_at=None,
                rate_card_id=None,
                total_hours_decimal=Decimal("0.00"),
                total_cents=0,
            )
        )
        s.add(
            Quote(
                id=new_ulid(),
                workspace_id=workspace_id,
                organization_id=quote_org,
                property_id=property_id,
                title="Spring package",
                body_md="",
                total_cents=0,
                currency="EUR",
                status="draft",
                sent_at=None,
                decided_at=None,
            )
        )
        s.flush()

        with pytest.raises(OrganizationInvalid, match="cannot become vendor"):
            _service(ctx).update(
                repo,
                work_order_org,
                OrganizationPatch({"kind": "vendor"}),
            )
        with pytest.raises(OrganizationInvalid, match="cannot become vendor"):
            _service(ctx).update(
                repo,
                quote_org,
                OrganizationPatch({"kind": "vendor"}),
            )


def test_duplicate_names_are_rejected_without_hiding_original(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _seed_workspace(s)
        ctx = _ctx(workspace_id)
        repo = SqlAlchemyOrganizationRepository(s)
        first = _service(ctx).create(
            repo,
            OrganizationCreate(kind="client", display_name="Dupont Family"),
        )

        with pytest.raises(OrganizationInvalid, match="already exists"):
            _service(ctx).create(
                repo,
                OrganizationCreate(kind="vendor", display_name=" Dupont Family "),
            )

        assert _service(ctx).get(repo, first.id).id == first.id


def test_update_rejects_duplicate_name_and_archived_rows(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _seed_workspace(s)
        ctx = _ctx(workspace_id)
        repo = SqlAlchemyOrganizationRepository(s)
        service = _service(ctx)
        first = service.create(
            repo,
            OrganizationCreate(kind="client", display_name="Alpha Family"),
        )
        second = service.create(
            repo,
            OrganizationCreate(kind="vendor", display_name="Beta Co"),
        )

        with pytest.raises(OrganizationInvalid, match="already exists"):
            service.update(
                repo,
                second.id,
                OrganizationPatch({"display_name": "Alpha Family"}),
            )

        service.archive(repo, second.id)
        with pytest.raises(OrganizationNotFound, match="organization not found"):
            service.update(
                repo,
                second.id,
                OrganizationPatch({"display_name": "Retired Beta"}),
            )
        with pytest.raises(OrganizationNotFound, match="organization not found"):
            service.get(repo, second.id)
        archived = service.get(repo, second.id, include_archived=True)
        assert archived.archived_at is not None
        assert service.get(repo, first.id).display_name == "Alpha Family"


def test_update_and_archive_write_audits_without_hard_delete(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _seed_workspace(s)
        ctx = _ctx(workspace_id)
        repo = SqlAlchemyOrganizationRepository(s)
        service = _service(ctx)
        org_id = _create_org(s, ctx, kind="client", display_name="Old Name")

        updated = service.update(
            repo,
            org_id,
            OrganizationPatch({"display_name": "New Name", "tax_id": None}),
        )
        archived = service.archive(repo, org_id)

        assert updated.display_name == "New Name"
        assert archived.archived_at == _PINNED

        row = s.get(Organization, org_id)
        assert row is not None
        assert row.archived_at is not None
        assert row.archived_at.replace(tzinfo=UTC) == _PINNED

        actions = s.scalars(select(AuditLog.action).order_by(AuditLog.id)).all()
        assert actions == [
            "billing.organization.created",
            "billing.organization.updated",
            "billing.organization.archived",
        ]
