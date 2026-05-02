"""Integration tests for the invite-accept work_engagement seed path.

Exercises cd-dv2's accept-side behaviour: :func:`_activate_invite`
seeds a minimal pending :class:`WorkEngagement` row at the moment
the invitee completes their passkey challenge. Nothing
workspace-scoped is materialised at invite-create time.

Narrow scope on purpose — the broader invite / accept / remove
flow is covered by
``tests/integration/identity/test_membership.py``. This file owns
only the engagement-seed assertions so the cd-dv2 acceptance
criterion ("Unit tests cover happy path + idempotent archive +
accept-time engagement seeding (nothing seeded at invite-create
time)") maps to a dedicated file the Beads task can reference.

See ``docs/specs/03-auth-and-tokens.md`` §"Additional users
(invite → click-to-accept)" and
``docs/specs/05-employees-and-roles.md`` §"Work engagement".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.identity.models import (
    Invite,
    PasskeyCredential,
)
from app.adapters.db.workspace.models import (
    UserWorkRole,
    WorkEngagement,
    WorkRole,
)
from app.auth._throttle import Throttle
from app.auth.magic_link_port import MagicLinkAdapter
from app.config import Settings
from app.domain.identity import membership
from app.tenancy import registry, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from tests._fakes.mailer import InMemoryMailer
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_BASE_URL = "https://crew.day"

_TEST_SETTINGS = Settings(
    root_key=SecretStr("test-root-key-for-invite-accept-0123456789abcdef"),
    public_url=_BASE_URL,
)


_SLUG_COUNTER = 0


def _next_slug() -> str:
    """Return a fresh, validator-compliant workspace slug for the test."""
    global _SLUG_COUNTER
    _SLUG_COUNTER += 1
    return f"inv-accept-{_SLUG_COUNTER:05d}"


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    """Re-register the workspace-scoped tables this test module depends on."""
    registry.register("role_grant")
    registry.register("permission_group")
    registry.register("permission_group_member")
    registry.register("audit_log")
    registry.register("invite")
    registry.register("user_workspace")
    registry.register("work_engagement")
    registry.register("user_work_role")
    registry.register("work_role")


def _ctx_for(workspace_id: str, workspace_slug: str, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLA",
    )


@pytest.fixture
def env(
    db_session: Session,
) -> Iterator[tuple[Session, WorkspaceContext, InMemoryMailer, Throttle]]:
    """Yield ``(session, ctx, mailer, throttle)`` bound to a fresh workspace."""
    install_tenant_filter(db_session)

    slug = _next_slug()
    clock = FrozenClock(_PINNED)
    user = bootstrap_user(
        db_session,
        email=f"{slug}@example.com",
        display_name=f"Owner {slug}",
        clock=clock,
    )
    ws = bootstrap_workspace(
        db_session,
        slug=slug,
        name=f"WS {slug}",
        owner_user_id=user.id,
        clock=clock,
    )
    ctx = _ctx_for(ws.id, ws.slug, user.id)

    token = set_current(ctx)
    try:
        yield db_session, ctx, InMemoryMailer(), Throttle()
    finally:
        reset_current(token)


def _invite_worker(
    session: Session,
    ctx: WorkspaceContext,
    mailer: InMemoryMailer,
    throttle: Throttle,
    *,
    email: str,
    display_name: str,
    role: str = "worker",
) -> membership.InviteOutcome:
    return membership.invite(
        session,
        ctx,
        email=email,
        display_name=display_name,
        grants=[
            {
                "scope_kind": "workspace",
                "scope_id": ctx.workspace_id,
                "grant_role": role,
            }
        ],
        mailer=mailer,
        throttle=throttle,
        base_url=_BASE_URL,
        settings=_TEST_SETTINGS,
        inviter_display_name="Owner",
        workspace_name=ctx.workspace_slug,
        link_port=MagicLinkAdapter(session),
    )


def _extract_token_from_body(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("http://") or stripped.startswith("https://"):
            return stripped.rsplit("/", 1)[-1]
    raise AssertionError(f"no URL in body: {body!r}")


def _seed_passkey(session: Session, *, user_id: str, clock: FrozenClock) -> None:
    with tenant_agnostic():
        session.add(
            PasskeyCredential(
                id=f"pk-{user_id}".encode(),
                user_id=user_id,
                public_key=b"test-public-key",
                sign_count=0,
                transports=None,
                backup_eligible=False,
                label="test passkey",
                created_at=clock.now(),
                last_used_at=None,
            )
        )
        session.flush()


def _engagements_for(
    session: Session, *, user_id: str, workspace_id: str
) -> list[WorkEngagement]:
    return list(
        session.scalars(
            select(WorkEngagement).where(
                WorkEngagement.user_id == user_id,
                WorkEngagement.workspace_id == workspace_id,
            )
        ).all()
    )


def _user_work_roles_for(
    session: Session, *, user_id: str, workspace_id: str
) -> list[UserWorkRole]:
    return list(
        session.scalars(
            select(UserWorkRole)
            .where(
                UserWorkRole.user_id == user_id,
                UserWorkRole.workspace_id == workspace_id,
            )
            .order_by(UserWorkRole.work_role_id.asc())
        ).all()
    )


def _seed_work_role(
    session: Session,
    *,
    workspace_id: str,
    key: str,
    name: str,
    clock: FrozenClock,
) -> str:
    """Insert a live :class:`WorkRole` and return its id."""
    from app.util.ulid import new_ulid

    role_id = new_ulid(clock=clock)
    with tenant_agnostic():
        session.add(
            WorkRole(
                id=role_id,
                workspace_id=workspace_id,
                key=key,
                name=name,
                created_at=clock.now(),
            )
        )
        session.flush()
    return role_id


# ---------------------------------------------------------------------------
# Tests — seed behaviour
# ---------------------------------------------------------------------------


class TestNothingSeededAtInviteCreate:
    """Invite-create does NOT land a workspace-scoped engagement row."""

    def test_invite_does_not_create_work_engagement(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        outcome = _invite_worker(
            session,
            ctx,
            mailer,
            throttle,
            email="alice@example.com",
            display_name="Alice",
        )
        assert outcome.user_id is not None
        rows = _engagements_for(
            session, user_id=outcome.user_id, workspace_id=ctx.workspace_id
        )
        assert rows == [], (
            "invite-create seeded a work_engagement row — should only "
            "happen at accept time per §03 'Additional users'"
        )


class TestAcceptSeedsEngagement:
    """Passkey-finish → ``_activate_invite`` lands a pending engagement."""

    def test_worker_grant_seeds_engagement_on_accept(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        outcome = _invite_worker(
            session,
            ctx,
            mailer,
            throttle,
            email="bob@example.com",
            display_name="Bob",
            role="worker",
        )
        assert outcome.user_id is not None

        token = _extract_token_from_body(mailer.sent[0].body_text)
        acceptance = membership.consume_invite_token(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=throttle,
            settings=_TEST_SETTINGS,
            link_port=MagicLinkAdapter(session),
        )
        assert isinstance(acceptance, membership.NewUserAcceptance)

        _seed_passkey(
            session,
            user_id=acceptance.session.user_id,
            clock=FrozenClock(_PINNED),
        )
        workspace_id = membership.complete_invite(
            session,
            invite_id=outcome.id,
            settings=_TEST_SETTINGS,
        )
        assert workspace_id == ctx.workspace_id

        rows = _engagements_for(
            session, user_id=outcome.user_id, workspace_id=ctx.workspace_id
        )
        assert len(rows) == 1
        engagement = rows[0]
        assert engagement.archived_on is None
        assert engagement.engagement_kind == "payroll"
        # ``complete_invite`` runs under SystemClock (no Clock dep on
        # the hook yet), so ``started_on`` is the production now.date()
        # — we only assert the column is populated.
        assert engagement.started_on is not None

        # Audit row on the engagement id — proves the seed landed via
        # :func:`seed_pending_work_engagement`, not a dangling side-effect.
        audit_rows = list(
            session.scalars(
                select(AuditLog).where(AuditLog.entity_id == engagement.id)
            ).all()
        )
        assert [r.action for r in audit_rows] == ["work_engagement.seeded_on_accept"]

    def test_manager_grant_seeds_engagement_on_accept(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        """``manager`` grants also draw from the pay pipeline — they seed."""
        session, ctx, mailer, throttle = env
        outcome = _invite_worker(
            session,
            ctx,
            mailer,
            throttle,
            email="mgr@example.com",
            display_name="Manager",
            role="manager",
        )
        assert outcome.user_id is not None

        token = _extract_token_from_body(mailer.sent[0].body_text)
        acceptance = membership.consume_invite_token(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=throttle,
            settings=_TEST_SETTINGS,
            link_port=MagicLinkAdapter(session),
        )
        assert isinstance(acceptance, membership.NewUserAcceptance)
        _seed_passkey(
            session,
            user_id=acceptance.session.user_id,
            clock=FrozenClock(_PINNED),
        )
        membership.complete_invite(
            session,
            invite_id=outcome.id,
            settings=_TEST_SETTINGS,
        )
        rows = _engagements_for(
            session, user_id=outcome.user_id, workspace_id=ctx.workspace_id
        )
        assert len(rows) == 1
        assert rows[0].archived_on is None
        assert rows[0].engagement_kind == "payroll"

    def test_client_grant_does_not_seed_engagement(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        """``client`` / ``guest`` grants do not draw pay — no seed."""
        session, ctx, mailer, throttle = env
        outcome = _invite_worker(
            session,
            ctx,
            mailer,
            throttle,
            email="carol@example.com",
            display_name="Carol",
            role="client",
        )
        assert outcome.user_id is not None

        token = _extract_token_from_body(mailer.sent[0].body_text)
        acceptance = membership.consume_invite_token(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=throttle,
            settings=_TEST_SETTINGS,
            link_port=MagicLinkAdapter(session),
        )
        assert isinstance(acceptance, membership.NewUserAcceptance)
        _seed_passkey(
            session,
            user_id=acceptance.session.user_id,
            clock=FrozenClock(_PINNED),
        )
        membership.complete_invite(
            session,
            invite_id=outcome.id,
            settings=_TEST_SETTINGS,
        )
        rows = _engagements_for(
            session, user_id=outcome.user_id, workspace_id=ctx.workspace_id
        )
        assert rows == []

    def test_accept_seed_is_idempotent_on_replay(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        """Second accept-time seed call returns the same engagement row.

        Drives :func:`seed_pending_work_engagement` directly with a
        pre-existing active row to prove the idempotency the accept
        path relies on.
        """
        from app.adapters.db.workspace.repositories import (
            SqlAlchemyMembershipRepository,
        )
        from app.services.employees.service import (
            seed_pending_work_engagement,
        )

        session, ctx, _mailer, _throttle = env
        # Seed a user + membership directly (no invite needed).
        user = bootstrap_user(
            session,
            email="idem@example.com",
            display_name="Idem",
            clock=FrozenClock(_PINNED),
        )
        from app.adapters.db.workspace.models import UserWorkspace

        with tenant_agnostic():
            session.add(
                UserWorkspace(
                    user_id=user.id,
                    workspace_id=ctx.workspace_id,
                    source="workspace_grant",
                    added_at=_PINNED,
                )
            )
            session.flush()

        repo = SqlAlchemyMembershipRepository(session)
        first = seed_pending_work_engagement(repo, ctx, user_id=user.id, now=_PINNED)
        second = seed_pending_work_engagement(repo, ctx, user_id=user.id, now=_PINNED)
        assert first is not None
        assert second is not None
        assert first.id == second.id


# ---------------------------------------------------------------------------
# cd-4o61 — invite carries pending ``work_engagement`` + ``user_work_roles``
# ---------------------------------------------------------------------------


def _invite_with_payload(
    session: Session,
    ctx: WorkspaceContext,
    mailer: InMemoryMailer,
    throttle: Throttle,
    *,
    email: str,
    display_name: str,
    role: str = "worker",
    work_engagement: dict[str, object] | None = None,
    user_work_roles: list[dict[str, object]] | None = None,
) -> membership.InviteOutcome:
    """Helper variant of :func:`_invite_worker` that wires the cd-4o61 payloads."""
    return membership.invite(
        session,
        ctx,
        email=email,
        display_name=display_name,
        grants=[
            {
                "scope_kind": "workspace",
                "scope_id": ctx.workspace_id,
                "grant_role": role,
            }
        ],
        work_engagement=work_engagement,
        user_work_roles=user_work_roles,
        mailer=mailer,
        throttle=throttle,
        base_url=_BASE_URL,
        settings=_TEST_SETTINGS,
        inviter_display_name="Owner",
        workspace_name=ctx.workspace_slug,
        link_port=MagicLinkAdapter(session),
    )


class TestInvitePersistsPendingPayload:
    """``invite()`` lands ``work_engagement`` + ``user_work_roles`` JSON.

    cd-4o61. The accept path consumes these columns; if they don't
    persist, the activation downstream would silently fall back to
    legacy defaults.
    """

    def test_invite_persists_work_engagement_json(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        outcome = _invite_with_payload(
            session,
            ctx,
            mailer,
            throttle,
            email="contractor@example.com",
            display_name="Connie",
            work_engagement={"engagement_kind": "contractor"},
        )
        invite_row = session.get(Invite, outcome.id)
        assert invite_row is not None
        assert invite_row.work_engagement_json == {"engagement_kind": "contractor"}
        assert invite_row.user_work_roles_json == []

    def test_invite_persists_user_work_roles_json(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        clock = FrozenClock(_PINNED)
        wr_id = _seed_work_role(
            session,
            workspace_id=ctx.workspace_id,
            key="maid",
            name="Maid",
            clock=clock,
        )
        outcome = _invite_with_payload(
            session,
            ctx,
            mailer,
            throttle,
            email="role@example.com",
            display_name="Roxie",
            user_work_roles=[{"work_role_id": wr_id}],
        )
        invite_row = session.get(Invite, outcome.id)
        assert invite_row is not None
        assert invite_row.user_work_roles_json == [{"work_role_id": wr_id}]

    def test_invite_rejects_unknown_work_role_id(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            _invite_with_payload(
                session,
                ctx,
                mailer,
                throttle,
                email="bad@example.com",
                display_name="Bad",
                user_work_roles=[{"work_role_id": "01HZZZZZZZZZZZZZZZZZZZZZ99"}],
            )
        assert "work_role_id" in str(exc.value)

    def test_invite_rejects_cross_workspace_work_role(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        """A WorkRole from a different workspace must surface as missing."""
        session, ctx, mailer, throttle = env
        # Bootstrap a separate workspace so we have a real WorkRole
        # row that the caller's workspace does not own.
        clock = FrozenClock(_PINNED)
        other_owner = bootstrap_user(
            session,
            email="other-owner@example.com",
            display_name="Other Owner",
            clock=clock,
        )
        other_ws = bootstrap_workspace(
            session,
            slug=f"other-{_next_slug()}",
            name="Other WS",
            owner_user_id=other_owner.id,
            clock=clock,
        )
        foreign_id = _seed_work_role(
            session,
            workspace_id=other_ws.id,
            key="cook",
            name="Cook",
            clock=clock,
        )

        with pytest.raises(membership.InviteBodyInvalid) as exc:
            _invite_with_payload(
                session,
                ctx,
                mailer,
                throttle,
                email="xws@example.com",
                display_name="X-WS",
                user_work_roles=[{"work_role_id": foreign_id}],
            )
        assert "work_role_id" in str(exc.value)

    def test_invite_rejects_agency_supplied_without_supplier_org(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        with pytest.raises(membership.InviteBodyInvalid):
            _invite_with_payload(
                session,
                ctx,
                mailer,
                throttle,
                email="agency@example.com",
                display_name="Agency",
                work_engagement={"engagement_kind": "agency_supplied"},
            )

    def test_refresh_path_overwrites_pending_payload(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        """A re-invite replaces the pending sub-payload — last write wins."""
        session, ctx, mailer, throttle = env
        first = _invite_with_payload(
            session,
            ctx,
            mailer,
            throttle,
            email="redo@example.com",
            display_name="Redo",
            work_engagement={"engagement_kind": "payroll"},
        )
        second = _invite_with_payload(
            session,
            ctx,
            mailer,
            throttle,
            email="redo@example.com",
            display_name="Redo",
            work_engagement={"engagement_kind": "contractor"},
        )
        assert first.id == second.id
        invite_row = session.get(Invite, second.id)
        assert invite_row is not None
        assert invite_row.work_engagement_json == {"engagement_kind": "contractor"}


class TestAcceptConsumesPendingPayload:
    """``_activate_invite`` consumes the JSON payload atomically."""

    def test_accept_overrides_default_engagement_kind(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        outcome = _invite_with_payload(
            session,
            ctx,
            mailer,
            throttle,
            email="cont@example.com",
            display_name="Cont",
            work_engagement={"engagement_kind": "contractor"},
        )
        token = _extract_token_from_body(mailer.sent[0].body_text)
        acceptance = membership.consume_invite_token(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=throttle,
            settings=_TEST_SETTINGS,
            link_port=MagicLinkAdapter(session),
        )
        assert isinstance(acceptance, membership.NewUserAcceptance)
        _seed_passkey(
            session,
            user_id=acceptance.session.user_id,
            clock=FrozenClock(_PINNED),
        )
        membership.complete_invite(
            session,
            invite_id=outcome.id,
            settings=_TEST_SETTINGS,
        )
        assert outcome.user_id is not None
        rows = _engagements_for(
            session, user_id=outcome.user_id, workspace_id=ctx.workspace_id
        )
        assert len(rows) == 1
        assert rows[0].engagement_kind == "contractor"
        assert rows[0].supplier_org_id is None

    def test_accept_inserts_user_work_role_rows(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        clock = FrozenClock(_PINNED)
        wr_a = _seed_work_role(
            session,
            workspace_id=ctx.workspace_id,
            key="maid",
            name="Maid",
            clock=clock,
        )
        wr_b = _seed_work_role(
            session,
            workspace_id=ctx.workspace_id,
            key="cook",
            name="Cook",
            clock=clock,
        )
        outcome = _invite_with_payload(
            session,
            ctx,
            mailer,
            throttle,
            email="multi@example.com",
            display_name="Multi",
            user_work_roles=[
                {"work_role_id": wr_a},
                {"work_role_id": wr_b},
            ],
        )
        token = _extract_token_from_body(mailer.sent[0].body_text)
        acceptance = membership.consume_invite_token(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=throttle,
            settings=_TEST_SETTINGS,
            link_port=MagicLinkAdapter(session),
        )
        assert isinstance(acceptance, membership.NewUserAcceptance)
        _seed_passkey(
            session,
            user_id=acceptance.session.user_id,
            clock=FrozenClock(_PINNED),
        )
        membership.complete_invite(
            session,
            invite_id=outcome.id,
            settings=_TEST_SETTINGS,
        )
        assert outcome.user_id is not None
        uwr_rows = _user_work_roles_for(
            session, user_id=outcome.user_id, workspace_id=ctx.workspace_id
        )
        assert sorted(r.work_role_id for r in uwr_rows) == sorted([wr_a, wr_b])
        # Default engagement still seeded — payload omitted ``work_engagement``.
        engagement_rows = _engagements_for(
            session, user_id=outcome.user_id, workspace_id=ctx.workspace_id
        )
        assert len(engagement_rows) == 1
        assert engagement_rows[0].engagement_kind == "payroll"

    def test_accept_writes_user_work_role_audit_rows(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        session, ctx, mailer, throttle = env
        clock = FrozenClock(_PINNED)
        wr_id = _seed_work_role(
            session,
            workspace_id=ctx.workspace_id,
            key="driver",
            name="Driver",
            clock=clock,
        )
        outcome = _invite_with_payload(
            session,
            ctx,
            mailer,
            throttle,
            email="audit@example.com",
            display_name="Auditee",
            user_work_roles=[{"work_role_id": wr_id}],
        )
        token = _extract_token_from_body(mailer.sent[0].body_text)
        acceptance = membership.consume_invite_token(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=throttle,
            settings=_TEST_SETTINGS,
            link_port=MagicLinkAdapter(session),
        )
        assert isinstance(acceptance, membership.NewUserAcceptance)
        _seed_passkey(
            session,
            user_id=acceptance.session.user_id,
            clock=FrozenClock(_PINNED),
        )
        membership.complete_invite(
            session,
            invite_id=outcome.id,
            settings=_TEST_SETTINGS,
        )

        # The user_work_role row's audit ride.
        uwrs = _user_work_roles_for(
            session,
            user_id=outcome.user_id,  # type: ignore[arg-type]
            workspace_id=ctx.workspace_id,
        )
        assert len(uwrs) == 1
        audit_rows = list(
            session.scalars(
                select(AuditLog)
                .where(AuditLog.entity_id == uwrs[0].id)
                .order_by(AuditLog.created_at.asc())
            ).all()
        )
        assert [r.action for r in audit_rows] == ["user_work_role.created"]
        diff = audit_rows[0].diff
        assert diff["work_role_id"] == wr_id
        assert diff["source"] == "invite_accept"

    def test_full_payload_lands_atomically(
        self,
        env: tuple[Session, WorkspaceContext, InMemoryMailer, Throttle],
    ) -> None:
        """End-to-end: invite carries both payloads; accept lands every row."""
        session, ctx, mailer, throttle = env
        clock = FrozenClock(_PINNED)
        wr_id = _seed_work_role(
            session,
            workspace_id=ctx.workspace_id,
            key="cleaner",
            name="Cleaner",
            clock=clock,
        )
        outcome = _invite_with_payload(
            session,
            ctx,
            mailer,
            throttle,
            email="full@example.com",
            display_name="Full",
            work_engagement={"engagement_kind": "contractor"},
            user_work_roles=[{"work_role_id": wr_id}],
        )
        token = _extract_token_from_body(mailer.sent[0].body_text)
        acceptance = membership.consume_invite_token(
            session,
            token=token,
            ip="127.0.0.1",
            throttle=throttle,
            settings=_TEST_SETTINGS,
            link_port=MagicLinkAdapter(session),
        )
        assert isinstance(acceptance, membership.NewUserAcceptance)
        _seed_passkey(
            session,
            user_id=acceptance.session.user_id,
            clock=FrozenClock(_PINNED),
        )
        membership.complete_invite(
            session,
            invite_id=outcome.id,
            settings=_TEST_SETTINGS,
        )

        assert outcome.user_id is not None
        engagements = _engagements_for(
            session, user_id=outcome.user_id, workspace_id=ctx.workspace_id
        )
        assert len(engagements) == 1
        assert engagements[0].engagement_kind == "contractor"
        uwrs = _user_work_roles_for(
            session, user_id=outcome.user_id, workspace_id=ctx.workspace_id
        )
        assert len(uwrs) == 1
        assert uwrs[0].work_role_id == wr_id

        # Accept-side audit surfaces the activated user_work_role ids.
        # New-user enrolment audits as ``user.enrolled``; the
        # existing-user grant-accept path uses ``user.grant_accepted``
        # — both share the diff shape we assert.
        invite_audit = list(
            session.scalars(
                select(AuditLog)
                .where(AuditLog.entity_id == outcome.id)
                .where(AuditLog.action == "user.enrolled")
            ).all()
        )
        assert len(invite_audit) == 1
        diff = invite_audit[0].diff
        assert diff["activated_user_work_role_ids"] == [uwrs[0].id]
