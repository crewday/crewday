"""Integration tests for :mod:`app.adapters.db.expenses` against a real DB.

Mirrors the shape of :mod:`tests.integration.test_db_payroll` but
exercises the three expense tables landed by cd-lbn and the FK
promotions added by cd-48c1.

Coverage matrix (per the cd-48c1 brief):

* Multi-attachment cardinality (0 / 1 / N attachments per claim).
* CASCADE on claim delete sweeps :class:`ExpenseLine` +
  :class:`ExpenseAttachment`.
* RESTRICT on :class:`WorkEngagement` delete blocks when a claim
  references the engagement.
* CASCADE on workspace delete sweeps the whole expense ledger
  (claim → lines → attachments).
* CHECK-constraint violations raise :class:`IntegrityError`: state
  outside enum, category outside enum, currency length != 3,
  attachment ``pages = 0``, line ``quantity < 0`` or
  ``unit_price_cents < 0``, ``autofill_confidence_overall`` outside
  ``[0, 1]``.
* JSON ``llm_autofill_json`` round-trips on both SQLite and
  Postgres.
* Tenant-filter behaviour: two workspaces, list returns only the
  current workspace's rows; bare SELECT without WorkspaceContext
  raises :class:`TenantFilterMissing`.

The sibling unit suite covers pure-Python model construction
without the migration harness; this file is the only place the
DB-level CHECK / FK contract is exercised.

See ``docs/specs/02-domain-model.md`` §"Core entities (by
document)" (§09 row), §"Shared tables"; and
``docs/specs/09-time-payroll-expenses.md`` §"Expense claims",
§"Payout destinations", §"Amount owed to the employee".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.assets.models import Asset, AssetType
from app.adapters.db.expenses.models import (
    ExpenseAttachment,
    ExpenseClaim,
    ExpenseLine,
)
from app.adapters.db.identity.models import User
from app.adapters.db.payroll.models import PayoutDestination
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.workspace.models import WorkEngagement, Workspace
from app.tenancy import registry, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import TenantFilterMissing, install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_PURCHASED_AT = _PINNED - timedelta(days=2)


_EXPENSE_TABLES: tuple[str, ...] = (
    "expense_claim",
    "expense_line",
    "expense_attachment",
)


@pytest.fixture(scope="module")
def filtered_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory with the tenant filter installed.

    Module-scoped so SQLAlchemy's per-sessionmaker event dispatch
    doesn't churn across tests. The top-level ``db_session`` fixture
    binds directly to a raw connection for SAVEPOINT isolation and
    therefore bypasses the filter; tests that need to observe
    :class:`TenantFilterMissing` use this factory explicitly.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_expense_registered() -> None:
    """Re-register the three expense tables as workspace-scoped.

    A sibling unit test
    (``tests/unit/test_tenancy_orm_filter.py``) calls
    :func:`registry._reset_for_tests` in an autouse fixture, which
    wipes the process-wide registry. The package's import-time
    registration loses that race; this fixture restores it before
    each test so tenant-filter assertions hold under the full suite.
    """
    for table in _EXPENSE_TABLES:
        registry.register(table)


def _ctx_for(workspace: Workspace, actor_id: str) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to ``workspace``."""
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLI",
    )


def _bootstrap(
    session: Session, *, email: str, display: str, slug: str, name: str
) -> tuple[Workspace, User]:
    """Seed a user + workspace pair for a test."""
    clock = FrozenClock(_PINNED)
    user = bootstrap_user(session, email=email, display_name=display, clock=clock)
    workspace = bootstrap_workspace(
        session, slug=slug, name=name, owner_user_id=user.id, clock=clock
    )
    return workspace, user


def _seed_engagement(
    session: Session, *, workspace: Workspace, user: User
) -> WorkEngagement:
    """Insert an active payroll :class:`WorkEngagement` for ``user``."""
    engagement = WorkEngagement(
        id=new_ulid(),
        user_id=user.id,
        workspace_id=workspace.id,
        engagement_kind="payroll",
        started_on=_PINNED.date(),
        archived_on=None,
        notes_md="",
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add(engagement)
    session.flush()
    return engagement


def _seed_property(session: Session, *, workspace: Workspace) -> Property:
    """Insert a :class:`Property` + owner :class:`PropertyWorkspace` row.

    ``property`` is workspace-agnostic (§02 "Villa belongs to many
    workspaces"); the binding to the workspace lives on
    :class:`PropertyWorkspace`. Without that junction the tenant
    filter would hide the property from sibling reads. We add both
    under :func:`tenant_agnostic` so the inserts don't trip the
    filter (the property table itself is unscoped).
    """
    prop = Property(
        id=new_ulid(),
        name="Villa Sud",
        kind="residence",
        address="1 Pool Way",
        address_json={"line1": "1 Pool Way", "country": "FR"},
        country="FR",
        timezone="Europe/Paris",
        tags_json=[],
        welcome_defaults_json={},
        property_notes_md="",
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    with tenant_agnostic():
        session.add(prop)
        session.flush()
        session.add(
            PropertyWorkspace(
                property_id=prop.id,
                workspace_id=workspace.id,
                label="Main",
                membership_role="owner_workspace",
                status="active",
                created_at=_PINNED,
            )
        )
        session.flush()
    return prop


def _seed_asset(
    session: Session, *, workspace: Workspace, property_: Property
) -> Asset:
    """Insert an :class:`Asset` row tied to ``property_`` for FK tests."""
    asset_type = session.scalars(
        select(AssetType).where(AssetType.workspace_id == workspace.id)
    ).first()
    assert asset_type is not None, "asset_type catalog must be seeded by workspace"
    asset = Asset(
        id=new_ulid(),
        workspace_id=workspace.id,
        property_id=property_.id,
        asset_type_id=asset_type.id,
        name="Coffee Machine",
        condition="good",
        status="active",
        qr_token="qr0000000001",
        guest_visible=False,
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add(asset)
    session.flush()
    return asset


def _seed_destination(
    session: Session, *, workspace: Workspace, user: User
) -> PayoutDestination:
    """Insert a :class:`PayoutDestination` for FK promotion tests."""
    destination = PayoutDestination(
        id=new_ulid(),
        workspace_id=workspace.id,
        user_id=user.id,
        kind="bank_account",
        currency="EUR",
        display_stub="FR-12",
        secret_ref_id=None,
        country="FR",
        label="Payroll",
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add(destination)
    session.flush()
    return destination


def _claim_kwargs(
    *,
    workspace: Workspace,
    engagement: WorkEngagement,
    state: str = "draft",
    **overrides: object,
) -> dict[str, object]:
    """Return the minimum kwargs needed to insert a valid claim."""
    base: dict[str, object] = dict(
        id=new_ulid(),
        workspace_id=workspace.id,
        work_engagement_id=engagement.id,
        vendor="Monoprix",
        purchased_at=_PURCHASED_AT,
        currency="EUR",
        total_amount_cents=4250,
        category="supplies",
        state=state,
        note_md="",
        created_at=_PINNED,
    )
    base.update(overrides)
    return base


class TestAttachmentCardinality:
    """0 / 1 / N attachments per claim are all valid."""

    def test_zero_one_and_many_attachments(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="attach-card@example.com",
            display="AttachCard",
            slug="attach-card-ws",
            name="AttachCardWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            zero = ExpenseClaim(
                **_claim_kwargs(workspace=workspace, engagement=engagement)
            )
            one = ExpenseClaim(
                **_claim_kwargs(workspace=workspace, engagement=engagement)
            )
            many = ExpenseClaim(
                **_claim_kwargs(workspace=workspace, engagement=engagement)
            )
            db_session.add_all([zero, one, many])
            db_session.flush()

            db_session.add(
                ExpenseAttachment(
                    id=new_ulid(),
                    workspace_id=workspace.id,
                    claim_id=one.id,
                    blob_hash="sha256-singleattachment",
                    kind="receipt",
                    pages=1,
                    created_at=_PINNED,
                )
            )
            for n in range(3):
                db_session.add(
                    ExpenseAttachment(
                        id=new_ulid(),
                        workspace_id=workspace.id,
                        claim_id=many.id,
                        blob_hash=f"sha256-multi-{n}",
                        kind="receipt" if n == 0 else "invoice",
                        pages=n + 1,
                        created_at=_PINNED,
                    )
                )
            db_session.flush()

            for claim, expected in ((zero, 0), (one, 1), (many, 3)):
                rows = db_session.scalars(
                    select(ExpenseAttachment)
                    .where(ExpenseAttachment.claim_id == claim.id)
                    .order_by(ExpenseAttachment.id)
                ).all()
                assert len(rows) == expected, (
                    f"claim {claim.id!s} expected {expected} "
                    f"attachment(s), got {len(rows)}"
                )
        finally:
            reset_current(token)


class TestCascadeOnClaimDelete:
    """Deleting a claim sweeps its lines + attachments."""

    def test_delete_claim_cascades(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="cascade-claim@example.com",
            display="CascadeClaim",
            slug="cascade-claim-ws",
            name="CascadeClaimWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            claim = ExpenseClaim(
                **_claim_kwargs(workspace=workspace, engagement=engagement)
            )
            db_session.add(claim)
            db_session.flush()

            line = ExpenseLine(
                id=new_ulid(),
                workspace_id=workspace.id,
                claim_id=claim.id,
                description="bread",
                quantity=Decimal("2"),
                unit_price_cents=200,
                line_total_cents=400,
                source="manual",
            )
            attachment = ExpenseAttachment(
                id=new_ulid(),
                workspace_id=workspace.id,
                claim_id=claim.id,
                blob_hash="sha256-cascadetest",
                kind="receipt",
                pages=1,
                created_at=_PINNED,
            )
            db_session.add_all([line, attachment])
            db_session.flush()

            line_id = line.id
            attachment_id = attachment.id
            db_session.delete(claim)
            db_session.flush()
            # Cascade swept the dependents at the DB layer; expunge
            # the stale instances so ``get`` doesn't refresh-raise,
            # then verify absence via fresh SELECTs.
            db_session.expunge(line)
            db_session.expunge(attachment)
            assert (
                db_session.scalars(
                    select(ExpenseLine).where(ExpenseLine.id == line_id)
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(ExpenseAttachment).where(
                        ExpenseAttachment.id == attachment_id
                    )
                ).all()
                == []
            )
            assert db_session.get(ExpenseClaim, claim.id) is None
        finally:
            reset_current(token)


class TestRestrictOnWorkEngagementDelete:
    """A raw DELETE on ``work_engagement`` is blocked while a claim refs it.

    The engagement carries the payroll-law evidence trail (§09
    §"Expense claims", §15 §"Right to erasure"); a hard DELETE must
    not silently drop claim history. The normal archive path is
    ``work_engagement.archived_on``.
    """

    def test_delete_engagement_with_claim_raises(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="restrict-eng@example.com",
            display="RestrictEng",
            slug="restrict-eng-ws",
            name="RestrictEngWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                ExpenseClaim(
                    **_claim_kwargs(workspace=workspace, engagement=engagement)
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        # Hard delete the engagement under tenant_agnostic — the
        # ``work_engagement`` table is workspace-scoped but the
        # admin / purge-style sweep runs without a context. The
        # FK is ``RESTRICT``, so the flush must raise.
        with tenant_agnostic():
            loaded = db_session.get(WorkEngagement, engagement.id)
            assert loaded is not None
            db_session.delete(loaded)
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()


class TestCascadeOnWorkspaceDelete:
    """Deleting a workspace sweeps the whole expense ledger."""

    def test_delete_workspace_cascades(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="cascade-ws-exp@example.com",
            display="CascadeWsExp",
            slug="cascade-ws-exp-ws",
            name="CascadeWsExpWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            claim = ExpenseClaim(
                **_claim_kwargs(workspace=workspace, engagement=engagement)
            )
            db_session.add(claim)
            db_session.flush()
            db_session.add_all(
                [
                    ExpenseLine(
                        id=new_ulid(),
                        workspace_id=workspace.id,
                        claim_id=claim.id,
                        description="x",
                        quantity=Decimal("1"),
                        unit_price_cents=100,
                        line_total_cents=100,
                        source="manual",
                    ),
                    ExpenseAttachment(
                        id=new_ulid(),
                        workspace_id=workspace.id,
                        claim_id=claim.id,
                        blob_hash="sha256-wsdelete",
                        kind="receipt",
                        pages=1,
                        created_at=_PINNED,
                    ),
                ]
            )
            db_session.flush()
        finally:
            reset_current(token)

        # justification: a workspace delete is a platform-level op —
        # no :class:`WorkspaceContext` applies once the tenant itself
        # is the target. The test relies on the ``work_engagement``
        # → workspace cascade clearing the engagement first so the
        # ``expense_claim`` → ``work_engagement`` RESTRICT FK does
        # not block the workspace delete.
        loaded_ws = db_session.get(Workspace, workspace.id)
        assert loaded_ws is not None
        with tenant_agnostic():
            db_session.delete(loaded_ws)
            db_session.flush()

        token = set_current(_ctx_for(workspace, user.id))
        try:
            assert (
                db_session.scalars(
                    select(ExpenseClaim).where(
                        ExpenseClaim.workspace_id == workspace.id
                    )
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(ExpenseLine).where(ExpenseLine.workspace_id == workspace.id)
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(ExpenseAttachment).where(
                        ExpenseAttachment.workspace_id == workspace.id
                    )
                ).all()
                == []
            )
        finally:
            reset_current(token)


class TestCheckConstraints:
    """CHECK constraints reject values outside the v1 enums / bounds."""

    def test_bogus_state_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bad-state-exp@example.com",
            display="BadStateExp",
            slug="bad-state-exp-ws",
            name="BadStateExpWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                ExpenseClaim(
                    **_claim_kwargs(
                        workspace=workspace,
                        engagement=engagement,
                        state="maybe_later",
                    )
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_bogus_category_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bad-cat-exp@example.com",
            display="BadCatExp",
            slug="bad-cat-exp-ws",
            name="BadCatExpWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                ExpenseClaim(
                    **_claim_kwargs(
                        workspace=workspace,
                        engagement=engagement,
                        category="luxury",
                    )
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_currency_length_not_three_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bad-curr-exp@example.com",
            display="BadCurrExp",
            slug="bad-curr-exp-ws",
            name="BadCurrExpWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                ExpenseClaim(
                    **_claim_kwargs(
                        workspace=workspace,
                        engagement=engagement,
                        currency="EURO",
                    )
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_attachment_pages_zero_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bad-pages-exp@example.com",
            display="BadPagesExp",
            slug="bad-pages-exp-ws",
            name="BadPagesExpWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            claim = ExpenseClaim(
                **_claim_kwargs(workspace=workspace, engagement=engagement)
            )
            db_session.add(claim)
            db_session.flush()
            db_session.add(
                ExpenseAttachment(
                    id=new_ulid(),
                    workspace_id=workspace.id,
                    claim_id=claim.id,
                    blob_hash="sha256-zeropages",
                    kind="receipt",
                    pages=0,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_line_negative_quantity_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="neg-qty-exp@example.com",
            display="NegQtyExp",
            slug="neg-qty-exp-ws",
            name="NegQtyExpWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            claim = ExpenseClaim(
                **_claim_kwargs(workspace=workspace, engagement=engagement)
            )
            db_session.add(claim)
            db_session.flush()
            db_session.add(
                ExpenseLine(
                    id=new_ulid(),
                    workspace_id=workspace.id,
                    claim_id=claim.id,
                    description="bad",
                    quantity=Decimal("-1"),
                    unit_price_cents=100,
                    line_total_cents=0,
                    source="manual",
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_line_negative_unit_price_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="neg-unit-exp@example.com",
            display="NegUnitExp",
            slug="neg-unit-exp-ws",
            name="NegUnitExpWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            claim = ExpenseClaim(
                **_claim_kwargs(workspace=workspace, engagement=engagement)
            )
            db_session.add(claim)
            db_session.flush()
            db_session.add(
                ExpenseLine(
                    id=new_ulid(),
                    workspace_id=workspace.id,
                    claim_id=claim.id,
                    description="bad",
                    quantity=Decimal("1"),
                    unit_price_cents=-1,
                    line_total_cents=0,
                    source="manual",
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_line_zero_quantity_accepted(self, db_session: Session) -> None:
        """Zero is on the boundary — ``quantity >= 0`` is the rule."""
        workspace, user = _bootstrap(
            db_session,
            email="zero-qty-exp@example.com",
            display="ZeroQtyExp",
            slug="zero-qty-exp-ws",
            name="ZeroQtyExpWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            claim = ExpenseClaim(
                **_claim_kwargs(workspace=workspace, engagement=engagement)
            )
            db_session.add(claim)
            db_session.flush()
            db_session.add(
                ExpenseLine(
                    id=new_ulid(),
                    workspace_id=workspace.id,
                    claim_id=claim.id,
                    description="freebie",
                    quantity=Decimal("0"),
                    unit_price_cents=0,
                    line_total_cents=0,
                    source="manual",
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

    def test_autofill_confidence_above_one_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bad-conf-hi-exp@example.com",
            display="BadConfHiExp",
            slug="bad-conf-hi-exp-ws",
            name="BadConfHiExpWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                ExpenseClaim(
                    **_claim_kwargs(
                        workspace=workspace,
                        engagement=engagement,
                        autofill_confidence_overall=Decimal("1.50"),
                    )
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_autofill_confidence_below_zero_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bad-conf-lo-exp@example.com",
            display="BadConfLoExp",
            slug="bad-conf-lo-exp-ws",
            name="BadConfLoExpWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                ExpenseClaim(
                    **_claim_kwargs(
                        workspace=workspace,
                        engagement=engagement,
                        autofill_confidence_overall=Decimal("-0.10"),
                    )
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)


class TestLlmAutofillJson:
    """``llm_autofill_json`` round-trips on every backend the suite hits."""

    def test_json_round_trip(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="json-rt-exp@example.com",
            display="JsonRtExp",
            slug="json-rt-exp-ws",
            name="JsonRtExpWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            payload: dict[str, object] = {
                "vendor": {"value": "Monoprix", "confidence": 0.92},
                "lines": [
                    {"description": "bread", "quantity": 1, "unit_price_cents": 250},
                    {"description": "milk", "quantity": 2, "unit_price_cents": 150},
                ],
                "currency": {"value": "EUR", "confidence": 0.99},
                "raw_text": "Monoprix\nBread 2.50\nMilk 1.50 x 2",
            }
            claim = ExpenseClaim(
                **_claim_kwargs(
                    workspace=workspace,
                    engagement=engagement,
                    llm_autofill_json=payload,
                    autofill_confidence_overall=Decimal("0.92"),
                )
            )
            db_session.add(claim)
            db_session.flush()
            db_session.expire_all()

            reloaded = db_session.get(ExpenseClaim, claim.id)
            assert reloaded is not None
            assert reloaded.llm_autofill_json == payload
            # Decimal round-trips through Numeric(3, 2).
            assert reloaded.autofill_confidence_overall == Decimal("0.92")
        finally:
            reset_current(token)


class TestForeignKeyPromotions:
    """The promoted FKs (cd-48c1) reject orphan writes + apply ON DELETE rules."""

    def test_owed_destination_orphan_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="orphan-dest@example.com",
            display="OrphanDest",
            slug="orphan-dest-ws",
            name="OrphanDestWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                ExpenseClaim(
                    **_claim_kwargs(
                        workspace=workspace,
                        engagement=engagement,
                        owed_destination_id="01HMISSINGDESTINATION0000",
                    )
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_property_orphan_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="orphan-prop@example.com",
            display="OrphanProp",
            slug="orphan-prop-ws",
            name="OrphanPropWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                ExpenseClaim(
                    **_claim_kwargs(
                        workspace=workspace,
                        engagement=engagement,
                        property_id="01HMISSINGPROPERTY00000000",
                    )
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_asset_orphan_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="orphan-asset@example.com",
            display="OrphanAsset",
            slug="orphan-asset-ws",
            name="OrphanAssetWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            claim = ExpenseClaim(
                **_claim_kwargs(workspace=workspace, engagement=engagement)
            )
            db_session.add(claim)
            db_session.flush()
            db_session.add(
                ExpenseLine(
                    id=new_ulid(),
                    workspace_id=workspace.id,
                    claim_id=claim.id,
                    description="bad",
                    quantity=Decimal("1"),
                    unit_price_cents=100,
                    line_total_cents=100,
                    asset_id="01HMISSINGASSET0000000000",
                    source="manual",
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_payout_destination_delete_blocked_by_restrict(
        self, db_session: Session
    ) -> None:
        """RESTRICT — destinations referenced by a claim cannot be hard-deleted."""
        workspace, user = _bootstrap(
            db_session,
            email="restrict-dest@example.com",
            display="RestrictDest",
            slug="restrict-dest-ws",
            name="RestrictDestWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        destination = _seed_destination(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                ExpenseClaim(
                    **_claim_kwargs(
                        workspace=workspace,
                        engagement=engagement,
                        owed_destination_id=destination.id,
                    )
                )
            )
            db_session.flush()

            db_session.delete(destination)
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_property_delete_sets_null_on_claim(self, db_session: Session) -> None:
        """SET NULL — a deleted property nulls ``expense_claim.property_id``."""
        workspace, user = _bootstrap(
            db_session,
            email="setnull-prop@example.com",
            display="SetNullProp",
            slug="setnull-prop-ws",
            name="SetNullPropWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        prop = _seed_property(db_session, workspace=workspace)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            claim = ExpenseClaim(
                **_claim_kwargs(
                    workspace=workspace,
                    engagement=engagement,
                    property_id=prop.id,
                )
            )
            db_session.add(claim)
            db_session.flush()
            assert claim.property_id == prop.id
        finally:
            reset_current(token)

        # property is workspace-agnostic — delete under tenant_agnostic.
        with tenant_agnostic():
            loaded_prop = db_session.get(Property, prop.id)
            assert loaded_prop is not None
            db_session.delete(loaded_prop)
            db_session.flush()

        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.expire_all()
            reloaded = db_session.get(ExpenseClaim, claim.id)
            assert reloaded is not None
            assert reloaded.property_id is None
        finally:
            reset_current(token)

    def test_asset_delete_sets_null_on_line(self, db_session: Session) -> None:
        """SET NULL — a deleted asset nulls ``expense_line.asset_id``."""
        workspace, user = _bootstrap(
            db_session,
            email="setnull-asset@example.com",
            display="SetNullAsset",
            slug="setnull-asset-ws",
            name="SetNullAssetWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        prop = _seed_property(db_session, workspace=workspace)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            asset = _seed_asset(db_session, workspace=workspace, property_=prop)
            claim = ExpenseClaim(
                **_claim_kwargs(workspace=workspace, engagement=engagement)
            )
            db_session.add(claim)
            db_session.flush()
            line = ExpenseLine(
                id=new_ulid(),
                workspace_id=workspace.id,
                claim_id=claim.id,
                description="kettle",
                quantity=Decimal("1"),
                unit_price_cents=4250,
                line_total_cents=4250,
                asset_id=asset.id,
                source="manual",
            )
            db_session.add(line)
            db_session.flush()
            assert line.asset_id == asset.id

            db_session.delete(asset)
            db_session.flush()
            db_session.expire_all()

            reloaded = db_session.get(ExpenseLine, line.id)
            assert reloaded is not None
            assert reloaded.asset_id is None
        finally:
            reset_current(token)


class TestTenantFilter:
    """Two workspaces, the filter scopes reads + bare SELECT raises."""

    def test_bootstrap_two_workspaces_isolated(
        self,
        filtered_factory: sessionmaker[Session],
    ) -> None:
        with filtered_factory() as session:
            ws_a, user_a = _bootstrap(
                session,
                email="tenant-a-exp@example.com",
                display="TenantAExp",
                slug="tenant-a-exp-ws",
                name="TenantAExpWS",
            )
            ws_b, user_b = _bootstrap(
                session,
                email="tenant-b-exp@example.com",
                display="TenantBExp",
                slug="tenant-b-exp-ws",
                name="TenantBExpWS",
            )
            engagement_a = _seed_engagement(session, workspace=ws_a, user=user_a)
            engagement_b = _seed_engagement(session, workspace=ws_b, user=user_b)

            token_a = set_current(_ctx_for(ws_a, user_a.id))
            try:
                session.add(
                    ExpenseClaim(
                        **_claim_kwargs(workspace=ws_a, engagement=engagement_a)
                    )
                )
                session.flush()
            finally:
                reset_current(token_a)

            token_b = set_current(_ctx_for(ws_b, user_b.id))
            try:
                session.add(
                    ExpenseClaim(
                        **_claim_kwargs(workspace=ws_b, engagement=engagement_b)
                    )
                )
                session.flush()
            finally:
                reset_current(token_b)

            # Read-back under each ctx returns only that workspace's
            # rows even though both are present in the table.
            token_a = set_current(_ctx_for(ws_a, user_a.id))
            try:
                rows_a = session.scalars(select(ExpenseClaim)).all()
                assert len(rows_a) == 1
                assert rows_a[0].workspace_id == ws_a.id
            finally:
                reset_current(token_a)

            token_b = set_current(_ctx_for(ws_b, user_b.id))
            try:
                rows_b = session.scalars(select(ExpenseClaim)).all()
                assert len(rows_b) == 1
                assert rows_b[0].workspace_id == ws_b.id
            finally:
                reset_current(token_b)

            session.rollback()

    @pytest.mark.parametrize("model", [ExpenseClaim, ExpenseLine, ExpenseAttachment])
    def test_read_without_ctx_raises(
        self,
        filtered_factory: sessionmaker[Session],
        model: type[ExpenseClaim] | type[ExpenseLine] | type[ExpenseAttachment],
    ) -> None:
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(model))
        assert exc.value.table == model.__tablename__


class TestModelDate:
    """Sanity guard so the test file doesn't accidentally drift from the model.

    Imported at module level for a fast fail if the model schema
    changes shape underneath the suite.
    """

    def test_purchased_at_accepts_date_only(self, db_session: Session) -> None:
        """``purchased_at`` is a ``DateTime`` — a bare ``date`` round-trips
        through ``UtcDateTime`` only when wrapped to UTC midnight."""
        workspace, user = _bootstrap(
            db_session,
            email="purchase-date@example.com",
            display="PurchaseDate",
            slug="purchase-date-ws",
            name="PurchaseDateWS",
        )
        engagement = _seed_engagement(db_session, workspace=workspace, user=user)
        token = set_current(_ctx_for(workspace, user.id))
        try:
            purchased = datetime.combine(
                date(2026, 4, 17), datetime.min.time(), tzinfo=UTC
            )
            db_session.add(
                ExpenseClaim(
                    **_claim_kwargs(
                        workspace=workspace,
                        engagement=engagement,
                        purchased_at=purchased,
                    )
                )
            )
            db_session.flush()
        finally:
            reset_current(token)
