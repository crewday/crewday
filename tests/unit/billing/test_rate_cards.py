"""Unit tests for billing rate-card CRUD and resolution."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.billing.repositories import (
    SqlAlchemyOrganizationRepository,
    SqlAlchemyRateCardRepository,
)
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.billing.organizations import (
    OrganizationCreate,
    OrganizationService,
)
from app.domain.billing.rate_cards import (
    RateCardCreate,
    RateCardInvalid,
    RateCardNotFound,
    RateCardPatch,
    RateCardService,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
_JAN_1 = date(2026, 1, 1)
_FEB_1 = date(2026, 2, 1)
_MAR_1 = date(2026, 3, 1)


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
            slug=f"rate-{workspace_id[-6:].lower()}",
            name="Rate Cards",
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
        workspace_slug="rate-cards",
        actor_id=new_ulid(),
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _org_service(ctx: WorkspaceContext) -> OrganizationService:
    return OrganizationService(ctx, clock=FrozenClock(_PINNED))


def _rate_service(ctx: WorkspaceContext) -> RateCardService:
    return RateCardService(ctx, clock=FrozenClock(_PINNED))


def _create_org(
    s: Session,
    ctx: WorkspaceContext,
    *,
    kind: str = "client",
    display_name: str = "Dupont Family",
    default_currency: str | None = None,
) -> str:
    view = _org_service(ctx).create(
        SqlAlchemyOrganizationRepository(s),
        OrganizationCreate(
            kind=kind,
            display_name=display_name,
            default_currency=default_currency,
        ),
    )
    return view.id


def test_create_defaults_currency_allows_override_and_audits(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _seed_workspace(s, default_currency="gbp")
        ctx = _ctx(workspace_id)
        org_id = _create_org(s, ctx, default_currency="eur")
        repo = SqlAlchemyRateCardRepository(s)
        service = _rate_service(ctx)

        defaulted = service.create(
            repo,
            org_id,
            RateCardCreate(
                label="Standard",
                rates={"maid": 2500},
                active_from=_JAN_1,
                active_to=_FEB_1,
            ),
        )
        override = service.create(
            repo,
            org_id,
            RateCardCreate(
                label="US clients",
                currency="usd",
                rates={"driver": 4100},
                active_from=_JAN_1,
                active_to=_FEB_1,
            ),
        )

        assert defaulted.currency == "EUR"
        assert override.currency == "USD"
        assert defaulted.rates == {"maid": 2500}
        audits = s.scalars(
            select(AuditLog).where(AuditLog.entity_kind == "rate_card")
        ).all()
        assert [audit.action for audit in audits] == [
            "billing.rate_card.created",
            "billing.rate_card.created",
        ]
        assert audits[0].diff["after"]["rates"] == {"maid": 2500}


def test_validate_rates_and_client_organization(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _seed_workspace(s)
        ctx = _ctx(workspace_id)
        client_id = _create_org(s, ctx, kind="client", display_name="Client")
        mixed_id = _create_org(s, ctx, kind="mixed", display_name="Mixed")
        archived_id = _create_org(s, ctx, kind="client", display_name="Archived")
        vendor_id = _create_org(s, ctx, kind="vendor", display_name="Vendor")
        _org_service(ctx).archive(SqlAlchemyOrganizationRepository(s), archived_id)
        repo = SqlAlchemyRateCardRepository(s)
        service = _rate_service(ctx)

        mixed = service.create(
            repo,
            mixed_id,
            RateCardCreate(
                label="Mixed",
                rates={"maid": 2500},
                active_from=_JAN_1,
            ),
        )
        assert mixed.organization_id == mixed_id
        with pytest.raises(RateCardInvalid, match="cannot have rate cards"):
            service.create(
                repo,
                vendor_id,
                RateCardCreate(
                    label="Vendor",
                    rates={"maid": 2500},
                    active_from=_JAN_1,
                ),
            )
        with pytest.raises(RateCardNotFound, match="organization not found"):
            service.create(
                repo,
                archived_id,
                RateCardCreate(
                    label="Archived",
                    rates={"maid": 2500},
                    active_from=_JAN_1,
                ),
            )
        with pytest.raises(RateCardInvalid, match="cannot be blank"):
            service.create(
                repo,
                client_id,
                RateCardCreate(label="Blank", rates={"  ": 2500}, active_from=_JAN_1),
            )
        with pytest.raises(RateCardInvalid, match="positive integer"):
            service.create(
                repo,
                client_id,
                RateCardCreate(label="Zero", rates={"maid": 0}, active_from=_JAN_1),
            )
        with pytest.raises(RateCardInvalid, match="positive integer"):
            service.create(
                repo,
                client_id,
                RateCardCreate(
                    label="Bool",
                    rates={"maid": True},
                    active_from=_JAN_1,
                ),
            )
        with pytest.raises(RateCardInvalid, match="not a valid ISO-4217"):
            service.create(
                repo,
                client_id,
                RateCardCreate(
                    label="Bad currency",
                    rates={"maid": 2500},
                    active_from=_JAN_1,
                    currency="EURO",
                ),
            )


def test_overlap_detection_allows_adjacent_boundaries_and_resolution(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _seed_workspace(s)
        ctx = _ctx(workspace_id)
        org_id = _create_org(s, ctx)
        repo = SqlAlchemyRateCardRepository(s)
        service = _rate_service(ctx)
        first = service.create(
            repo,
            org_id,
            RateCardCreate(
                label="January",
                rates={"maid": 2500},
                active_from=_JAN_1,
                active_to=_FEB_1,
            ),
        )
        second = service.create(
            repo,
            org_id,
            RateCardCreate(
                label="February",
                rates={"maid": 3000},
                active_from=_FEB_1,
                active_to=_MAR_1,
            ),
        )
        service.create(
            repo,
            org_id,
            RateCardCreate(
                label="Overlapping driver",
                rates={"driver": 4200},
                active_from=_JAN_1,
                active_to=_MAR_1,
            ),
        )

        assert service.resolve(repo, org_id, "maid", on=_JAN_1) == 2500
        assert service.resolve(repo, org_id, "maid", on=date(2026, 1, 31)) == 2500
        assert service.resolve(repo, org_id, "maid", on=_FEB_1) == 3000
        assert service.get(repo, org_id, first.id).id == first.id
        assert service.get(repo, org_id, second.id).id == second.id
        with pytest.raises(RateCardInvalid, match="overlaps existing window"):
            service.create(
                repo,
                org_id,
                RateCardCreate(
                    label="Mid-month",
                    rates={"maid": 2800},
                    active_from=date(2026, 1, 15),
                    active_to=date(2026, 2, 15),
                ),
            )
        with pytest.raises(RateCardNotFound, match="no rate card covers"):
            service.resolve(repo, org_id, "maid", on=_MAR_1)


def test_update_detects_overlap_and_audits_changed_fields(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _seed_workspace(s)
        ctx = _ctx(workspace_id)
        org_id = _create_org(s, ctx)
        repo = SqlAlchemyRateCardRepository(s)
        service = _rate_service(ctx)
        january = service.create(
            repo,
            org_id,
            RateCardCreate(
                label="January",
                rates={"maid": 2500},
                active_from=_JAN_1,
                active_to=_FEB_1,
            ),
        )
        february = service.create(
            repo,
            org_id,
            RateCardCreate(
                label="February",
                rates={"maid": 3000},
                active_from=_FEB_1,
                active_to=_MAR_1,
            ),
        )

        with pytest.raises(RateCardInvalid, match="overlaps existing window"):
            service.update(
                repo,
                org_id,
                february.id,
                RateCardPatch({"active_from": date(2026, 1, 15)}),
            )
        with pytest.raises(RateCardInvalid, match="positive integer"):
            service.update(
                repo,
                org_id,
                january.id,
                RateCardPatch({"rates": {"maid": False}}),
            )
        updated = service.update(
            repo,
            org_id,
            january.id,
            RateCardPatch({"rates": {"maid": 2600, "driver": 4000}}),
        )

        assert updated.rates == {"maid": 2600, "driver": 4000}
        audits = s.scalars(
            select(AuditLog)
            .where(AuditLog.action == "billing.rate_card.updated")
            .order_by(AuditLog.created_at.asc())
        ).all()
        assert len(audits) == 1
        assert audits[0].diff["changed"] == ["rates"]
        assert audits[0].diff["before"]["rates"] == {"maid": 2500}
        assert audits[0].diff["after"]["rates"] == {
            "maid": 2600,
            "driver": 4000,
        }


def test_patch_active_to_null_is_distinct_from_omitted_field(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _seed_workspace(s)
        ctx = _ctx(workspace_id)
        org_id = _create_org(s, ctx)
        repo = SqlAlchemyRateCardRepository(s)
        service = _rate_service(ctx)
        rate_card = service.create(
            repo,
            org_id,
            RateCardCreate(
                label="January",
                rates={"maid": 2500},
                active_from=_JAN_1,
                active_to=_FEB_1,
            ),
        )

        renamed = service.update(
            repo,
            org_id,
            rate_card.id,
            RateCardPatch({"label": "Renamed"}),
        )
        assert renamed.active_to == _FEB_1

        open_ended = service.update(
            repo,
            org_id,
            rate_card.id,
            RateCardPatch({"active_to": None}),
        )
        assert open_ended.active_to is None

        audits = s.scalars(
            select(AuditLog)
            .where(AuditLog.action == "billing.rate_card.updated")
            .order_by(AuditLog.created_at.asc())
        ).all()
        assert [audit.diff["changed"] for audit in audits] == [
            ["label"],
            ["active_to"],
        ]
