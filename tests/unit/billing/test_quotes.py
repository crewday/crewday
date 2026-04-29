"""Unit tests for billing quote service."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.billing.models import Organization, Quote, WorkOrder
from app.adapters.db.billing.repositories import SqlAlchemyQuoteRepository
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.billing.quotes import (
    QuoteCreate,
    QuoteInvalid,
    QuotePatch,
    QuoteService,
    QuoteTokenInvalid,
)
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests._fakes.mailer import InMemoryMailer

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
_SIGNING_KEY = b"quote-test-signing-key-32-bytes!!"


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


def _seed_workspace_graph(s: Session) -> tuple[str, str, str]:
    workspace_id = new_ulid()
    org_id = new_ulid()
    property_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"billing-{workspace_id[-6:].lower()}",
            name="Billing",
            plan="free",
            quota_json={},
            settings_json={},
            default_currency="EUR",
            created_at=_PINNED,
        )
    )
    s.flush()
    s.add(
        Organization(
            id=org_id,
            workspace_id=workspace_id,
            kind="client",
            display_name="Dupont Family",
            billing_address={},
            tax_id=None,
            default_currency="EUR",
            contact_email="client@example.com",
            contact_phone=None,
            notes_md=None,
            created_at=_PINNED,
        )
    )
    s.flush()
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
            client_org_id=org_id,
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
    s.add(
        WorkOrder(
            id=new_ulid(),
            workspace_id=workspace_id,
            organization_id=org_id,
            property_id=property_id,
            title="Pool repair",
            status="draft",
            starts_at=_PINNED,
            total_hours_decimal=Decimal("0.00"),
            total_cents=0,
        )
    )
    s.flush()
    return workspace_id, org_id, property_id


def _service(ctx: WorkspaceContext) -> QuoteService:
    return QuoteService(ctx, clock=FrozenClock(_PINNED), signing_key=_SIGNING_KEY)


def _create_quote(
    s: Session,
    ctx: WorkspaceContext,
    *,
    title: str = "Pool repair quote",
) -> str:
    view = _service(ctx).create(
        SqlAlchemyQuoteRepository(s),
        QuoteCreate(
            organization_id=s.info["org_id"],
            property_id=s.info["property_id"],
            title=title,
            body_md="Parts and labour.",
            total_cents=12500,
        ),
    )
    return view.id


def _seeded_session(
    factory: sessionmaker[Session],
) -> Iterator[tuple[Session, WorkspaceContext]]:
    with factory() as s:
        workspace_id, org_id, property_id = _seed_workspace_graph(s)
        s.info["org_id"] = org_id
        s.info["property_id"] = property_id
        yield s, _ctx(workspace_id)


def test_sent_quote_cannot_be_updated_and_can_be_superseded(
    factory: sessionmaker[Session],
) -> None:
    for s, ctx in _seeded_session(factory):
        repo = SqlAlchemyQuoteRepository(s)
        quote_id = _create_quote(s, ctx)
        service = _service(ctx)
        service.send(
            repo, quote_id, mailer=InMemoryMailer(), base_url="https://crew.day"
        )

        with pytest.raises(QuoteInvalid, match="supersede"):
            service.update(repo, quote_id, QuotePatch(fields={"title": "New title"}))

        clone = service.supersede(
            repo, quote_id, QuotePatch(fields={"title": "New title"})
        )

        assert clone.id != quote_id
        assert clone.title == "New title"
        assert clone.status == "draft"
        original = repo.get(workspace_id=ctx.workspace_id, quote_id=quote_id)
        assert original is not None
        assert original.status == "expired"


def test_quote_token_rejects_expired_and_tampered(
    factory: sessionmaker[Session],
) -> None:
    for s, ctx in _seeded_session(factory):
        quote_id = _create_quote(s, ctx)
        row = SqlAlchemyQuoteRepository(s).get(
            workspace_id=ctx.workspace_id, quote_id=quote_id
        )
        assert row is not None
        service = _service(ctx)
        expired = service.sign_token(row, expires_at=_PINNED - timedelta(seconds=1))
        valid = service.sign_token(row, expires_at=_PINNED + timedelta(days=1))

        with pytest.raises(QuoteTokenInvalid, match="expired"):
            service.verify_token(expired, quote_id=quote_id)
        with pytest.raises(QuoteTokenInvalid):
            service.verify_token(valid[:-1] + "x", quote_id=quote_id)


def test_send_resend_emits_one_email_per_call_and_audits(
    factory: sessionmaker[Session],
) -> None:
    for s, ctx in _seeded_session(factory):
        repo = SqlAlchemyQuoteRepository(s)
        quote_id = _create_quote(s, ctx)
        mailer = InMemoryMailer()
        service = _service(ctx)

        first = service.send(repo, quote_id, mailer=mailer, base_url="https://crew.day")
        second = service.send(
            repo, quote_id, mailer=mailer, base_url="https://crew.day"
        )

        assert first.status == "sent"
        assert second.status == "sent"
        assert len(mailer.sent) == 2
        assert all(f"/q/{quote_id}?token=" in msg.body_text for msg in mailer.sent)
        sent_audits = s.scalars(
            select(AuditLog).where(AuditLog.action == "billing.quote.sent")
        ).all()
        assert len(sent_audits) == 2


def test_send_does_not_mail_when_sent_state_cannot_flush(
    factory: sessionmaker[Session],
) -> None:
    for s, ctx in _seeded_session(factory):
        repo = SqlAlchemyQuoteRepository(s)
        quote_id = _create_quote(s, ctx)
        mailer = InMemoryMailer()

        @event.listens_for(s, "before_flush")
        def _fail_sent_flush(
            session: Session,
            _flush_context: object,
            _instances: object,
            quote_id: str = quote_id,
        ) -> None:
            for obj in session.dirty:
                if (
                    isinstance(obj, Quote)
                    and obj.id == quote_id
                    and obj.status == "sent"
                ):
                    raise RuntimeError("sent state flush failed")

        with pytest.raises(RuntimeError, match="sent state flush failed"):
            _service(ctx).send(
                repo, quote_id, mailer=mailer, base_url="https://crew.day"
            )

        assert mailer.sent == []


def test_public_accept_is_idempotent_and_audits_guest_token_hint(
    factory: sessionmaker[Session],
) -> None:
    for s, ctx in _seeded_session(factory):
        repo = SqlAlchemyQuoteRepository(s)
        quote_id = _create_quote(s, ctx)
        service = _service(ctx)
        sent = service.send(
            repo, quote_id, mailer=InMemoryMailer(), base_url="https://crew.day"
        )
        row = repo.get(workspace_id=ctx.workspace_id, quote_id=quote_id)
        assert row is not None
        token = service.sign_token(row, expires_at=_PINNED + timedelta(days=1))

        accepted = service.public_accept(repo, quote_id=sent.id, token=token)
        accepted_again = service.public_accept(repo, quote_id=sent.id, token=token)

        assert accepted.status == "accepted"
        assert accepted_again.status == "accepted"
        audits = s.scalars(
            select(AuditLog).where(AuditLog.action == "billing.quote.accepted")
        ).all()
        assert len(audits) == 1
        assert audits[0].diff["actor_kind"] == "guest_token"


def test_public_get_validates_token_workspace(
    factory: sessionmaker[Session],
) -> None:
    for s, ctx in _seeded_session(factory):
        repo = SqlAlchemyQuoteRepository(s)
        quote_id = _create_quote(s, ctx)
        row = repo.get(workspace_id=ctx.workspace_id, quote_id=quote_id)
        assert row is not None
        service = _service(ctx)
        valid = service.sign_token(row, expires_at=_PINNED + timedelta(days=1))
        wrong_workspace = service.sign_token(
            row.__class__(
                id=row.id,
                workspace_id=new_ulid(),
                organization_id=row.organization_id,
                property_id=row.property_id,
                title=row.title,
                body_md=row.body_md,
                total_cents=row.total_cents,
                currency=row.currency,
                status=row.status,
                sent_at=row.sent_at,
                decided_at=row.decided_at,
            ),
            expires_at=_PINNED + timedelta(days=1),
        )

        assert service.public_get(repo, quote_id=quote_id, token=valid).id == quote_id
        with pytest.raises(QuoteTokenInvalid):
            service.public_get(repo, quote_id=quote_id, token=wrong_workspace)


def test_supersede_rejects_unknown_patch_fields(
    factory: sessionmaker[Session],
) -> None:
    for s, ctx in _seeded_session(factory):
        repo = SqlAlchemyQuoteRepository(s)
        quote_id = _create_quote(s, ctx)

        with pytest.raises(QuoteInvalid, match="unknown quote fields"):
            _service(ctx).supersede(
                repo, quote_id, QuotePatch(fields={"unexpected": "ignored"})
            )
