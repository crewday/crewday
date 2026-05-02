"""Unit tests for :mod:`app.domain.identity.membership`.

Cover the pure-function helpers (validators) plus the public
exception surface without touching a DB. Integration paths live in
:mod:`tests.integration.identity.test_membership`.

The :class:`TestPruneStaleInvites` block (cd-za45) is the one
exception — the TTL sweep needs an in-memory engine so the
``Invite.state`` flip + the per-row :class:`InviteExpired` event
publish can be observed end-to-end. Mirrors the engine fixture
pattern in ``tests/unit/auth/test_signup.py::TestPruneStaleSignups``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.identity.models import Invite, User
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.identity import membership
from app.events.bus import EventBus
from app.events.types import InviteExpired


class TestPublicSurface:
    """The module's exported symbols match the spec contract."""

    def test_exports_exception_types(self) -> None:
        assert membership.InviteBodyInvalid is not None
        assert membership.InviteNotFound is not None
        assert membership.InviteStateInvalid is not None
        assert membership.InviteExpired is not None
        assert membership.InviteAlreadyAccepted is not None
        assert membership.PasskeySessionRequired is not None
        assert membership.NotAMember is not None
        assert membership.WouldOrphanOwnersGroup is not None

    def test_exports_write_member_remove_rejected_audit(self) -> None:
        # Reused from permission_groups — the membership module
        # re-exports it so the HTTP router can import both from
        # one place.
        assert membership.write_member_remove_rejected_audit is not None


class TestValueObjects:
    """Value dataclasses are frozen (immutable)."""

    def test_invite_outcome_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        outcome = membership.InviteOutcome(
            id="invite-1",
            pending_email="a@example.com",
            user_id="user-1",
            user_created=True,
        )
        with pytest.raises(FrozenInstanceError):
            # Frozen dataclass: mutation raises FrozenInstanceError.
            outcome.id = "mutated"  # type: ignore[misc]

    def test_invite_session_shape(self) -> None:
        ssn = membership.InviteSession(
            invite_id="invite-1",
            user_id="user-1",
            email_lower="a@example.com",
            display_name="A",
        )
        assert ssn.email_lower == "a@example.com"


class TestGrantValidation:
    """``_validate_grants`` enforces the v1 scope + role enum.

    Private helper but exercised here so we catch shape drift early —
    the integration tests take longer to fail on a regression. The
    cross-tenant DB joins for ``property`` and ``organization`` scope
    are exercised in ``tests/integration/identity/test_membership.py``;
    this class covers the workspace-scope shape rules + the structural
    rejections that fire before any DB lookup.
    """

    _WORKSPACE_ID = "01HWA000000000000000WS001"

    def test_empty_grants_rejected(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid):
            membership._validate_grants([], workspace_id=self._WORKSPACE_ID)

    def test_unknown_scope_kind_rejected(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_grants(
                [
                    {
                        "scope_kind": "deployment",
                        "scope_id": self._WORKSPACE_ID,
                        "grant_role": "worker",
                    }
                ],
                workspace_id=self._WORKSPACE_ID,
            )
        assert "deployment" in str(exc.value)

    def test_scope_id_must_match_workspace(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_grants(
                [
                    {
                        "scope_kind": "workspace",
                        "scope_id": "different-workspace",
                        "grant_role": "worker",
                    }
                ],
                workspace_id=self._WORKSPACE_ID,
            )
        assert "scope_id" in str(exc.value)

    def test_invalid_grant_role_rejected(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_grants(
                [
                    {
                        "scope_kind": "workspace",
                        "scope_id": self._WORKSPACE_ID,
                        "grant_role": "admin",
                    }
                ],
                workspace_id=self._WORKSPACE_ID,
            )
        assert "admin" in str(exc.value)

    def test_happy_path_accepts_every_v1_role(self) -> None:
        for role in ("manager", "worker", "client", "guest"):
            membership._validate_grants(
                [
                    {
                        "scope_kind": "workspace",
                        "scope_id": self._WORKSPACE_ID,
                        "grant_role": role,
                    }
                ],
                workspace_id=self._WORKSPACE_ID,
            )

    def test_workspace_scope_rejects_scope_property_id(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_grants(
                [
                    {
                        "scope_kind": "workspace",
                        "scope_id": self._WORKSPACE_ID,
                        "grant_role": "worker",
                        "scope_property_id": "01HWA0000000000000000P001",
                    }
                ],
                workspace_id=self._WORKSPACE_ID,
            )
        assert "scope_property_id" in str(exc.value)

    def test_workspace_scope_binding_org_only_on_client(self) -> None:
        # Non-client roles cannot carry binding_org_id even at workspace
        # scope (mirrors the role_grant ``client_binding_org_scope`` CHECK).
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_grants(
                [
                    {
                        "scope_kind": "workspace",
                        "scope_id": self._WORKSPACE_ID,
                        "grant_role": "worker",
                        "binding_org_id": "01HWA00000000000000000ORG1",
                    }
                ],
                workspace_id=self._WORKSPACE_ID,
            )
        assert "binding_org_id" in str(exc.value)

    def test_property_scope_requires_scope_property_id(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_grants(
                [
                    {
                        "scope_kind": "property",
                        "scope_id": self._WORKSPACE_ID,
                        "grant_role": "worker",
                    }
                ],
                workspace_id=self._WORKSPACE_ID,
            )
        assert "scope_property_id" in str(exc.value)

    def test_property_scope_rejects_binding_org_id(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_grants(
                [
                    {
                        "scope_kind": "property",
                        "scope_id": self._WORKSPACE_ID,
                        "grant_role": "worker",
                        "scope_property_id": "01HWA0000000000000000P001",
                        "binding_org_id": "01HWA00000000000000000ORG1",
                    }
                ],
                workspace_id=self._WORKSPACE_ID,
            )
        assert "binding_org_id" in str(exc.value)

    def test_property_scope_requires_session_for_cross_check(self) -> None:
        # Pure-shape unit path: a property-scoped grant without a
        # session reaches the cross-check and surfaces a clear error.
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_grants(
                [
                    {
                        "scope_kind": "property",
                        "scope_id": self._WORKSPACE_ID,
                        "grant_role": "worker",
                        "scope_property_id": "01HWA0000000000000000P001",
                    }
                ],
                workspace_id=self._WORKSPACE_ID,
            )
        assert "database session" in str(exc.value)

    def test_organization_scope_only_for_client_grants(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_grants(
                [
                    {
                        "scope_kind": "organization",
                        "scope_id": "01HWA00000000000000000ORG1",
                        "grant_role": "manager",
                    }
                ],
                workspace_id=self._WORKSPACE_ID,
            )
        assert "client" in str(exc.value)

    def test_organization_scope_binding_must_match_scope_id(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_grants(
                [
                    {
                        "scope_kind": "organization",
                        "scope_id": "01HWA00000000000000000ORG1",
                        "grant_role": "client",
                        "binding_org_id": "01HWA00000000000000000ORG2",
                    }
                ],
                workspace_id=self._WORKSPACE_ID,
            )
        assert "binding_org_id" in str(exc.value)


class TestTimezoneNormalisation:
    """``_aware_utc`` stamps UTC on naive datetimes."""

    def test_naive_gets_utc(self) -> None:
        from datetime import datetime as _dt

        out = membership._aware_utc(_dt(2026, 1, 1, 12, 0, 0))
        assert out.tzinfo is not None

    def test_aware_stays_aware(self) -> None:
        from datetime import UTC
        from datetime import datetime as _dt

        out = membership._aware_utc(_dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
        assert out.tzinfo == UTC


class TestWorkEngagementValidation:
    """``_validate_work_engagement`` enforces the §02 supplier biconditional.

    cd-4o61. ``None`` is the legacy "no override" path; every present
    payload must have a known ``engagement_kind`` and the supplier
    column must align with that kind. Pure unit coverage so the
    domain integration test (which needs a live DB to land an invite)
    only proves the wiring.
    """

    def test_none_payload_is_a_noop(self) -> None:
        membership._validate_work_engagement(None)

    def test_payroll_without_supplier_ok(self) -> None:
        membership._validate_work_engagement({"engagement_kind": "payroll"})

    def test_contractor_without_supplier_ok(self) -> None:
        membership._validate_work_engagement(
            {"engagement_kind": "contractor", "supplier_org_id": None}
        )

    def test_agency_supplied_with_supplier_ok(self) -> None:
        membership._validate_work_engagement(
            {
                "engagement_kind": "agency_supplied",
                "supplier_org_id": "org-123",
            }
        )

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_work_engagement({"engagement_kind": "freelance"})
        assert "freelance" in str(exc.value)

    def test_agency_supplied_without_supplier_rejected(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_work_engagement({"engagement_kind": "agency_supplied"})
        assert "supplier_org_id" in str(exc.value)

    def test_payroll_with_supplier_rejected(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_work_engagement(
                {
                    "engagement_kind": "payroll",
                    "supplier_org_id": "org-leak",
                }
            )
        assert "supplier_org_id" in str(exc.value)

    def test_contractor_with_supplier_rejected(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid):
            membership._validate_work_engagement(
                {
                    "engagement_kind": "contractor",
                    "supplier_org_id": "org-leak",
                }
            )


class TestUserWorkRolesShape:
    """``_validate_user_work_roles`` early-returns on empty + shape errors.

    cd-4o61. The DB-touching cases (workspace lookup, soft-delete
    skip, foreign-workspace rejection) are exercised in
    ``tests/integration/identity/test_membership.py``; this unit
    suite covers the no-DB shape-check path so a typo in the
    shape fails loud regardless of session state.
    """

    _WORKSPACE_ID = "01HWA000000000000000WS001"

    def test_empty_list_is_a_noop(self) -> None:
        # No session call happens — passing ``None`` would crash if
        # the early return is missing.
        membership._validate_user_work_roles(
            None,  # type: ignore[arg-type]
            user_work_roles=[],
            workspace_id=self._WORKSPACE_ID,
        )

    def test_missing_work_role_id_rejected(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid) as exc:
            membership._validate_user_work_roles(
                None,  # type: ignore[arg-type]
                user_work_roles=[{}],
                workspace_id=self._WORKSPACE_ID,
            )
        assert "work_role_id" in str(exc.value)

    def test_empty_work_role_id_rejected(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid):
            membership._validate_user_work_roles(
                None,  # type: ignore[arg-type]
                user_work_roles=[{"work_role_id": ""}],
                workspace_id=self._WORKSPACE_ID,
            )

    def test_non_string_work_role_id_rejected(self) -> None:
        with pytest.raises(membership.InviteBodyInvalid):
            membership._validate_user_work_roles(
                None,  # type: ignore[arg-type]
                user_work_roles=[{"work_role_id": 42}],
                workspace_id=self._WORKSPACE_ID,
            )


# ---------------------------------------------------------------------------
# prune_stale_invites — TTL sweeper (cd-za45)
# ---------------------------------------------------------------------------


_PINNED = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
_WORKSPACE_ID = "01HWA000000000000000WS001"
_INVITER_USER_ID = "01HWAUSERID00000000000IBY1"


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with the full schema applied.

    Mirrors :class:`tests.unit.auth.test_signup.TestPruneStaleSignups`'s
    fixture — :func:`prune_stale_invites` writes against the ``invite``
    table, so the unit needs a real engine. ``Base.metadata.create_all``
    keeps the suite hermetic without an Alembic upgrade.
    """
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Yield a session with the parent ``workspace`` row seeded.

    Every ``invite`` row hard-FKs ``workspace.id`` (``ON DELETE CASCADE``);
    seeding the parent in the fixture means individual tests stay
    focused on the sweep-side state transitions instead of bookkeeping
    the FK shape.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        s.add(
            Workspace(
                id=_WORKSPACE_ID,
                slug="ttl-test",
                name="TTL Test Workspace",
                plan="free",
                quota_json={},
                created_at=_PINNED - timedelta(days=7),
            )
        )
        s.add(
            User(
                id=_INVITER_USER_ID,
                email="inviter@example.test",
                email_lower="inviter@example.test",
                display_name="Inviter",
                created_at=_PINNED - timedelta(days=30),
            )
        )
        s.flush()
        yield s


def _make_invite(
    *,
    invite_id: str,
    state: str = "pending",
    expires_at: datetime,
    workspace_id: str = _WORKSPACE_ID,
    invited_by_user_id: str | None = None,
) -> Invite:
    """Build one invite row with the bare-minimum shape.

    The PII columns are seeded with stable filler — the helper does
    not exercise the email-hash pepper (covered in the integration
    suite); the TTL sweep only reads ``state`` + ``expires_at`` + the
    fields it stamps onto the published event.
    """
    return Invite(
        id=invite_id,
        workspace_id=workspace_id,
        user_id=None,
        pending_email=f"{invite_id.lower()}@example.test",
        pending_email_lower=f"{invite_id.lower()}@example.test",
        email_hash="0" * 64,
        display_name="Invitee",
        state=state,
        grants_json=[],
        group_memberships_json=[],
        work_engagement_json=None,
        user_work_roles_json=[],
        invited_by_user_id=invited_by_user_id,
        created_at=_PINNED - timedelta(hours=48),
        expires_at=expires_at,
    )


class TestPruneStaleInvites:
    """``prune_stale_invites`` flips expired pending rows + emits one
    :class:`InviteExpired` per row.

    cd-za45. Cross-tenant under :func:`tenant_agnostic` so the worker
    sweep sees every workspace's pending queue without a
    :class:`WorkspaceContext`. The shape mirrors
    :func:`app.domain.agent.approval.expire_due` exactly so a future
    refactor of either ports back to the other (the report dataclass
    + the per-row defensive guard).
    """

    def test_flips_expired_pending_rows(self, session: Session) -> None:
        # Two pending rows past expiry → both flip. The third is in
        # the future and stays put.
        past_a = _make_invite(
            invite_id="01HWA0000000000000INVPAST1",
            expires_at=_PINNED - timedelta(hours=1),
        )
        past_b = _make_invite(
            invite_id="01HWA0000000000000INVPAST2",
            expires_at=_PINNED - timedelta(minutes=5),
        )
        future = _make_invite(
            invite_id="01HWA0000000000000INVFUTUR",
            expires_at=_PINNED + timedelta(hours=1),
        )
        session.add_all([past_a, past_b, future])
        session.flush()

        report = membership.prune_stale_invites(session=session, now=_PINNED)

        assert report.expired_count == 2
        assert set(report.expired_ids) == {past_a.id, past_b.id}
        # Flush the dirty state down so a fresh re-read from the
        # session reflects the writes (the worker commits at the tick
        # boundary; the unit tests check the in-session view directly
        # without a commit).
        session.flush()
        assert past_a.state == "expired"
        assert past_b.state == "expired"
        # The future row stays pending — the cutoff is strictly
        # ``expires_at <= now`` so a row whose expiry is later does
        # not fall in.
        assert future.state == "pending"

    def test_skips_terminal_states(self, session: Session) -> None:
        # accepted / expired / revoked rows past the cutoff stay put.
        # The defensive guard inside ``prune_stale_invites`` keeps the
        # worker from clobbering a concurrent accept that landed
        # between SELECT and UPDATE — even though the predicate would
        # already exclude these rows, the guard is the belt to the
        # WHERE clause's braces.
        accepted = _make_invite(
            invite_id="01HWA0000000000000INVACCEP",
            state="accepted",
            expires_at=_PINNED - timedelta(hours=1),
        )
        already_expired = _make_invite(
            invite_id="01HWA0000000000000INVEXPDT",
            state="expired",
            expires_at=_PINNED - timedelta(hours=1),
        )
        revoked = _make_invite(
            invite_id="01HWA0000000000000INVREVOK",
            state="revoked",
            expires_at=_PINNED - timedelta(hours=1),
        )
        session.add_all([accepted, already_expired, revoked])
        session.flush()

        report = membership.prune_stale_invites(session=session, now=_PINNED)

        assert report.expired_count == 0
        assert report.expired_ids == ()
        session.flush()
        assert accepted.state == "accepted"
        assert already_expired.state == "expired"
        assert revoked.state == "revoked"

    def test_empty_sweep_is_a_noop(self, session: Session) -> None:
        # No rows at all → returns an empty report and emits no events.
        local_bus = EventBus()
        seen: list[InviteExpired] = []
        local_bus.subscribe(InviteExpired)(seen.append)

        report = membership.prune_stale_invites(
            session=session,
            now=_PINNED,
            event_bus=local_bus,
        )

        assert report.expired_count == 0
        assert report.expired_ids == ()
        assert seen == []

    def test_publishes_one_event_per_row(self, session: Session) -> None:
        # Inject a fresh bus so the test does not depend on global
        # subscribers other unit modules may have registered against
        # the singleton bus. The handler captures the published events
        # for assertion.
        local_bus = EventBus()
        seen: list[InviteExpired] = []
        local_bus.subscribe(InviteExpired)(seen.append)

        row = _make_invite(
            invite_id="01HWA0000000000000INVEVENT",
            expires_at=_PINNED - timedelta(hours=1),
            invited_by_user_id=_INVITER_USER_ID,
        )
        session.add(row)
        session.flush()

        report = membership.prune_stale_invites(
            session=session,
            now=_PINNED,
            event_bus=local_bus,
        )

        assert report.expired_count == 1
        assert len(seen) == 1
        event = seen[0]
        assert event.invite_id == row.id
        assert event.workspace_id == row.workspace_id
        # Inviter is the actor for SSE filter routing — fall-back to
        # ``"system"`` only kicks in when the inviter row was hard-
        # deleted; here the inviter is set so we see it carried.
        assert event.actor_id == _INVITER_USER_ID
        assert event.correlation_id == row.id
        assert event.occurred_at == _PINNED

    def test_orphan_inviter_falls_back_to_system_actor(
        self,
        session: Session,
    ) -> None:
        # ``invited_by_user_id`` is a soft FK (NULL on a hard-deleted
        # inviter) — the published event must still attribute to
        # *someone* so the SSE filter has a routable actor. Falls back
        # to the ``"system"`` sentinel.
        local_bus = EventBus()
        seen: list[InviteExpired] = []
        local_bus.subscribe(InviteExpired)(seen.append)

        row = _make_invite(
            invite_id="01HWA0000000000000INVORPHN",
            expires_at=_PINNED - timedelta(hours=1),
            invited_by_user_id=None,
        )
        session.add(row)
        session.flush()

        membership.prune_stale_invites(
            session=session,
            now=_PINNED,
            event_bus=local_bus,
        )

        assert len(seen) == 1
        assert seen[0].actor_id == "system"

    def test_naive_cutoff_is_normalised_to_utc(self, session: Session) -> None:
        # ``_aware_utc`` normalises a naive datetime to UTC so a caller
        # that forgot the tzinfo (very rare — the worker passes a
        # tz-aware ``Clock.now()``) does not silently miscompare against
        # the SA-loaded ``expires_at``. Pin the boundary by passing a
        # naive cutoff and asserting the row past expiry still flips.
        row = _make_invite(
            invite_id="01HWA0000000000000INVNAIVE",
            expires_at=_PINNED - timedelta(hours=1),
        )
        session.add(row)
        session.flush()

        naive_now = _PINNED.replace(tzinfo=None)
        report = membership.prune_stale_invites(session=session, now=naive_now)

        assert report.expired_count == 1
        session.flush()
        assert row.state == "expired"
