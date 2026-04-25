"""Unit tests for :mod:`app.auth.break_glass`.

Three surface areas:

* :func:`is_step_up_user` — manager-grant detection, owners-group
  membership detection, non-step-up worker / client / guest, the
  union (manager AND owners), and tenant-agnostic read posture (no
  ``WorkspaceContext`` is set, but the helper must still see rows).
* :func:`redeem_code` — happy-path argon2id verify + burn,
  wrong-code miss, replay (already-burnt rows are invisible), the
  multi-code walk, and isolation across users.
* :func:`check_redeem_allowed` / :func:`record_redeem_failure` /
  :func:`record_redeem_success` — the 3-fail / 1h / 24h-lockout
  contract from §15 "Step-up bypass is not a fallback".

Tests run against an in-memory SQLite engine populated from
``Base.metadata`` — same posture as ``test_recovery.py``. The
argon2id verifier runs for real (no monkey-patch); the cost
parameters are the v1 default, ~50ms/op locally, well under the
unit-test latency budget at the row counts we exercise here.

See ``docs/specs/03-auth-and-tokens.md`` §"Break-glass codes" /
§"Self-service lost-device recovery" and
``docs/specs/15-security-privacy.md`` §"Step-up bypass is not a
fallback".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.base import Base
from app.adapters.db.identity.models import BreakGlassCode
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.auth import break_glass
from app.auth.break_glass import (
    BreakGlassLockedOut,
    check_redeem_allowed,
    is_step_up_user,
    record_redeem_failure,
    record_redeem_success,
    redeem_code,
    reset_rate_limit_for_tests,
)
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

_PINNED = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture(autouse=True)
def reset_rate_limit() -> Iterator[None]:
    """Clear the per-user redemption counters between cases."""
    reset_rate_limit_for_tests()
    yield
    reset_rate_limit_for_tests()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_workspace(session: Session, *, slug: str = "ws") -> str:
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=f"{slug}-{workspace_id[:8].lower()}",
            name=slug.title(),
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


def _seed_role_grant(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
    grant_role: str,
) -> str:
    grant_id = new_ulid()
    session.add(
        RoleGrant(
            id=grant_id,
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    session.flush()
    return grant_id


def _seed_owners_group(session: Session, *, workspace_id: str) -> str:
    group_id = new_ulid()
    session.add(
        PermissionGroup(
            id=group_id,
            workspace_id=workspace_id,
            slug="owners",
            name="Owners",
            system=True,
            capabilities_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return group_id


def _add_user_to_group(
    session: Session,
    *,
    group_id: str,
    workspace_id: str,
    user_id: str,
) -> None:
    session.add(
        PermissionGroupMember(
            group_id=group_id,
            user_id=user_id,
            workspace_id=workspace_id,
            added_at=_PINNED,
            added_by_user_id=None,
        )
    )
    session.flush()


def _seed_break_glass_code(
    session: Session,
    *,
    user_id: str,
    workspace_id: str,
    plaintext: str,
    used_at: datetime | None = None,
) -> str:
    """Insert one :class:`BreakGlassCode` row with the live argon2id hasher.

    Returns the row id so tests can assert on it after a redeem walk.
    The code is hashed via the *real* argon2id verifier — this exercises
    the production path end-to-end at unit-test scope.
    """
    code_id = new_ulid()
    code_hash = break_glass._HASHER.hash(plaintext)
    with tenant_agnostic():
        session.add(
            BreakGlassCode(
                id=code_id,
                workspace_id=workspace_id,
                user_id=user_id,
                hash=code_hash,
                hash_params={
                    "time_cost": 3,
                    "memory_cost": 65536,
                    "parallelism": 4,
                },
                created_at=_PINNED,
                used_at=used_at,
                consumed_magic_link_id=None,
            )
        )
        session.flush()
    return code_id


# ---------------------------------------------------------------------------
# is_step_up_user
# ---------------------------------------------------------------------------


class TestIsStepUpUser:
    """Classification across the four populations."""

    def test_user_with_manager_grant_is_step_up(self, session: Session) -> None:
        user = bootstrap_user(session, email="m@example.com", display_name="M")
        ws_id = _seed_workspace(session)
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="manager"
        )
        assert is_step_up_user(session, user_id=user.id) is True

    def test_user_in_owners_group_is_step_up(self, session: Session) -> None:
        user = bootstrap_user(session, email="o@example.com", display_name="O")
        ws_id = _seed_workspace(session)
        # Owners-group member without ANY role_grant — pure governance
        # membership still triggers step-up per §03 step 3.
        owners_group_id = _seed_owners_group(session, workspace_id=ws_id)
        _add_user_to_group(
            session,
            group_id=owners_group_id,
            workspace_id=ws_id,
            user_id=user.id,
        )
        assert is_step_up_user(session, user_id=user.id) is True

    def test_worker_grant_only_is_not_step_up(self, session: Session) -> None:
        user = bootstrap_user(session, email="w@example.com", display_name="W")
        ws_id = _seed_workspace(session)
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="worker"
        )
        assert is_step_up_user(session, user_id=user.id) is False

    def test_client_grant_only_is_not_step_up(self, session: Session) -> None:
        user = bootstrap_user(session, email="c@example.com", display_name="C")
        ws_id = _seed_workspace(session)
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="client"
        )
        assert is_step_up_user(session, user_id=user.id) is False

    def test_user_with_no_grants_is_not_step_up(self, session: Session) -> None:
        """A bare identity row with no grants / no group membership."""
        user = bootstrap_user(session, email="b@example.com", display_name="B")
        assert is_step_up_user(session, user_id=user.id) is False

    def test_manager_grant_anywhere_triggers_stepup(self, session: Session) -> None:
        """One manager grant in any workspace is enough."""
        user = bootstrap_user(session, email="any@example.com", display_name="Any")
        ws_a = _seed_workspace(session, slug="a")
        ws_b = _seed_workspace(session, slug="b")
        _seed_role_grant(
            session, workspace_id=ws_a, user_id=user.id, grant_role="worker"
        )
        # Manager in the OTHER workspace — still flips step-up.
        _seed_role_grant(
            session, workspace_id=ws_b, user_id=user.id, grant_role="manager"
        )
        assert is_step_up_user(session, user_id=user.id) is True

    def test_owners_group_in_other_workspace_triggers_stepup(
        self, session: Session
    ) -> None:
        user = bootstrap_user(session, email="ot@example.com", display_name="OT")
        # Worker grant here — non-step-up by itself.
        ws_a = _seed_workspace(session, slug="a")
        _seed_role_grant(
            session, workspace_id=ws_a, user_id=user.id, grant_role="worker"
        )
        # Owners-group member in a DIFFERENT workspace — flips step-up.
        ws_b = _seed_workspace(session, slug="b")
        owners_b = _seed_owners_group(session, workspace_id=ws_b)
        _add_user_to_group(
            session, group_id=owners_b, workspace_id=ws_b, user_id=user.id
        )
        assert is_step_up_user(session, user_id=user.id) is True

    def test_other_users_grants_do_not_leak(self, session: Session) -> None:
        """The classifier scopes the walk to its ``user_id`` argument."""
        target = bootstrap_user(session, email="t@example.com", display_name="T")
        other = bootstrap_user(session, email="other@example.com", display_name="O")
        ws_id = _seed_workspace(session)
        # Other user is a manager; target is a bare worker.
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=other.id, grant_role="manager"
        )
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=target.id, grant_role="worker"
        )
        assert is_step_up_user(session, user_id=target.id) is False
        assert is_step_up_user(session, user_id=other.id) is True

    def test_non_owners_group_membership_does_not_trigger(
        self, session: Session
    ) -> None:
        """Only the ``owners`` slug counts — a sibling group membership doesn't."""
        user = bootstrap_user(session, email="g@example.com", display_name="G")
        ws_id = _seed_workspace(session)
        # Group with a non-owners slug — should NOT flip step-up.
        group_id = new_ulid()
        session.add(
            PermissionGroup(
                id=group_id,
                workspace_id=ws_id,
                slug="cleaners",
                name="Cleaners",
                system=False,
                capabilities_json={},
                created_at=_PINNED,
            )
        )
        session.flush()
        _add_user_to_group(
            session, group_id=group_id, workspace_id=ws_id, user_id=user.id
        )
        # Worker grant so the user has *some* presence in the workspace.
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="worker"
        )
        assert is_step_up_user(session, user_id=user.id) is False


# ---------------------------------------------------------------------------
# redeem_code
# ---------------------------------------------------------------------------


class TestRedeemCode:
    """Argon2id verify walk + atomic burn."""

    def test_correct_code_is_burnt_and_id_returned(self, session: Session) -> None:
        user = bootstrap_user(session, email="r@example.com", display_name="R")
        ws_id = _seed_workspace(session)
        code_id = _seed_break_glass_code(
            session,
            user_id=user.id,
            workspace_id=ws_id,
            plaintext="alpha-bravo-charlie",
        )
        result = redeem_code(
            session,
            user_id=user.id,
            plaintext_code="alpha-bravo-charlie",
            now=_PINNED,
        )
        assert result == code_id
        # Row burnt: ``used_at`` populated. SQLite's
        # ``DateTime(timezone=True)`` drops tzinfo on round-trip — we
        # compare on the naive replacement so the test is portable.
        with tenant_agnostic():
            burnt = session.get(BreakGlassCode, code_id)
        assert burnt is not None
        assert burnt.used_at is not None
        assert burnt.used_at.replace(tzinfo=UTC) == _PINNED

    def test_wrong_code_returns_none_and_does_not_burn(self, session: Session) -> None:
        user = bootstrap_user(session, email="w@example.com", display_name="W")
        ws_id = _seed_workspace(session)
        code_id = _seed_break_glass_code(
            session,
            user_id=user.id,
            workspace_id=ws_id,
            plaintext="real-code",
        )
        result = redeem_code(
            session,
            user_id=user.id,
            plaintext_code="WRONG",
            now=_PINNED,
        )
        assert result is None
        # Row untouched.
        with tenant_agnostic():
            row = session.get(BreakGlassCode, code_id)
        assert row is not None
        assert row.used_at is None

    def test_already_burnt_code_returns_none(self, session: Session) -> None:
        user = bootstrap_user(session, email="a@example.com", display_name="A")
        ws_id = _seed_workspace(session)
        # Pre-burn the row by setting ``used_at``.
        _seed_break_glass_code(
            session,
            user_id=user.id,
            workspace_id=ws_id,
            plaintext="spent",
            used_at=_PINNED - timedelta(hours=1),
        )
        result = redeem_code(
            session,
            user_id=user.id,
            plaintext_code="spent",
            now=_PINNED,
        )
        # Even though the plaintext matches the hash, the partial
        # filter excludes burnt rows from the walk.
        assert result is None

    def test_walk_picks_correct_code_from_set(self, session: Session) -> None:
        """Multiple unused codes — the verifier picks the matching one."""
        user = bootstrap_user(session, email="m@example.com", display_name="M")
        ws_id = _seed_workspace(session)
        ids = [
            _seed_break_glass_code(
                session,
                user_id=user.id,
                workspace_id=ws_id,
                plaintext=f"code-{i}",
            )
            for i in range(4)
        ]
        # Redeem the third code.
        result = redeem_code(
            session,
            user_id=user.id,
            plaintext_code="code-2",
            now=_PINNED,
        )
        assert result == ids[2]
        # Other rows still unused.
        with tenant_agnostic():
            for idx, row_id in enumerate(ids):
                row = session.get(BreakGlassCode, row_id)
                assert row is not None
                if idx == 2:
                    assert row.used_at is not None
                    assert row.used_at.replace(tzinfo=UTC) == _PINNED
                else:
                    assert row.used_at is None

    def test_other_user_code_does_not_match(self, session: Session) -> None:
        """A code issued to user A cannot be redeemed against user B."""
        a = bootstrap_user(session, email="a@example.com", display_name="A")
        b = bootstrap_user(session, email="b@example.com", display_name="B")
        ws_id = _seed_workspace(session)
        # Code belongs to A.
        _seed_break_glass_code(
            session,
            user_id=a.id,
            workspace_id=ws_id,
            plaintext="shared-plaintext",
        )
        # B submits the SAME plaintext — should miss because the walk
        # filters by ``user_id``.
        result = redeem_code(
            session,
            user_id=b.id,
            plaintext_code="shared-plaintext",
            now=_PINNED,
        )
        assert result is None

    def test_no_codes_for_user_returns_none(self, session: Session) -> None:
        user = bootstrap_user(session, email="empty@example.com", display_name="E")
        result = redeem_code(
            session,
            user_id=user.id,
            plaintext_code="anything",
            now=_PINNED,
        )
        assert result is None

    def test_replay_after_burn_returns_none(self, session: Session) -> None:
        """Once burnt, the same plaintext + same user → miss."""
        user = bootstrap_user(session, email="r@example.com", display_name="R")
        ws_id = _seed_workspace(session)
        _seed_break_glass_code(
            session,
            user_id=user.id,
            workspace_id=ws_id,
            plaintext="burn-me",
        )
        first = redeem_code(
            session,
            user_id=user.id,
            plaintext_code="burn-me",
            now=_PINNED,
        )
        assert first is not None
        # Second call with the same plaintext — already burnt.
        second = redeem_code(
            session,
            user_id=user.id,
            plaintext_code="burn-me",
            now=_PINNED + timedelta(seconds=1),
        )
        assert second is None


# ---------------------------------------------------------------------------
# Redemption rate limit
# ---------------------------------------------------------------------------


class TestRedemptionRateLimit:
    """Spec §03 → "Redemption rate limit": 3 fails/1h → 24h lockout."""

    def test_no_failures_does_not_lock_out(self) -> None:
        check_redeem_allowed(user_id="u1", now=_PINNED)

    def test_two_failures_does_not_lock_out(self) -> None:
        record_redeem_failure(user_id="u1", now=_PINNED)
        record_redeem_failure(user_id="u1", now=_PINNED + timedelta(seconds=1))
        # Still allowed.
        check_redeem_allowed(user_id="u1", now=_PINNED + timedelta(seconds=2))

    def test_third_failure_locks_out_24h(self) -> None:
        record_redeem_failure(user_id="u1", now=_PINNED)
        record_redeem_failure(user_id="u1", now=_PINNED + timedelta(seconds=1))
        record_redeem_failure(user_id="u1", now=_PINNED + timedelta(seconds=2))
        with pytest.raises(BreakGlassLockedOut):
            check_redeem_allowed(user_id="u1", now=_PINNED + timedelta(seconds=3))
        # Still locked at 23h59m.
        with pytest.raises(BreakGlassLockedOut):
            check_redeem_allowed(
                user_id="u1",
                now=_PINNED + timedelta(hours=23, minutes=59),
            )

    def test_lockout_clears_after_24h(self) -> None:
        record_redeem_failure(user_id="u1", now=_PINNED)
        record_redeem_failure(user_id="u1", now=_PINNED)
        record_redeem_failure(user_id="u1", now=_PINNED)
        # Lockout expires exactly at +24h+1s.
        check_redeem_allowed(
            user_id="u1",
            now=_PINNED + timedelta(hours=24, seconds=1),
        )

    def test_failures_outside_window_do_not_count(self) -> None:
        """Failures more than 1h old fall out of the rolling window."""
        # Old failures.
        record_redeem_failure(user_id="u1", now=_PINNED - timedelta(hours=2))
        record_redeem_failure(user_id="u1", now=_PINNED - timedelta(hours=2))
        # One fresh failure: total in-window count is 1, not 3.
        record_redeem_failure(user_id="u1", now=_PINNED)
        check_redeem_allowed(user_id="u1", now=_PINNED + timedelta(seconds=1))

    def test_success_clears_failures(self) -> None:
        record_redeem_failure(user_id="u1", now=_PINNED)
        record_redeem_failure(user_id="u1", now=_PINNED)
        record_redeem_success(user_id="u1")
        # Counter reset — adding 2 more failures should NOT trip lockout.
        record_redeem_failure(user_id="u1", now=_PINNED + timedelta(seconds=1))
        record_redeem_failure(user_id="u1", now=_PINNED + timedelta(seconds=2))
        check_redeem_allowed(user_id="u1", now=_PINNED + timedelta(seconds=3))

    def test_lockout_is_per_user(self) -> None:
        """One user's lockout doesn't affect a sibling user."""
        record_redeem_failure(user_id="u1", now=_PINNED)
        record_redeem_failure(user_id="u1", now=_PINNED)
        record_redeem_failure(user_id="u1", now=_PINNED)
        # u1 is locked.
        with pytest.raises(BreakGlassLockedOut):
            check_redeem_allowed(user_id="u1", now=_PINNED + timedelta(seconds=1))
        # u2 is fine.
        check_redeem_allowed(user_id="u2", now=_PINNED + timedelta(seconds=1))

    def test_after_lockout_expires_counter_starts_fresh(self) -> None:
        """The rolling window is cleared on lockout flip — the user has to
        earn the next lockout from scratch.
        """
        # Trigger first lockout.
        for _ in range(3):
            record_redeem_failure(user_id="u1", now=_PINNED)
        # Wait it out.
        post_lockout = _PINNED + timedelta(hours=24, seconds=1)
        check_redeem_allowed(user_id="u1", now=post_lockout)
        # 2 more failures — should NOT trip a second lockout (counter
        # was cleared on the flip; only 2/3 in the window now).
        record_redeem_failure(user_id="u1", now=post_lockout)
        record_redeem_failure(user_id="u1", now=post_lockout + timedelta(seconds=1))
        check_redeem_allowed(user_id="u1", now=post_lockout + timedelta(seconds=2))
