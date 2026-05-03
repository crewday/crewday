"""Per-IP aggregate LLM spend cap and ownership verification seams."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import SignupAttempt
from app.adapters.db.llm.models import BudgetLedger
from app.adapters.db.llm.models import LlmUsage as LlmUsageRow
from app.adapters.db.workspace.models import Workspace
from app.auth import passkey as passkey_module
from app.auth import signup
from app.auth._throttle import Throttle
from app.config import Settings
from app.domain.llm.budget import (
    IP_BUDGET_EXCEEDED_MESSAGE,
    BudgetExceeded,
    check_budget,
    normalize_signup_ip_key,
)
from app.domain.plans import FREE_TIER_DEFAULTS
from app.services.workspace.ownership_verification import (
    WorkspaceVerificationMismatch,
    consume_ownership_verification,
    request_ownership_verification,
)
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)
_CAPABILITY = "chat.manager"
_IP_CAP = 3 * FREE_TIER_DEFAULTS["llm_budget_cents_30d"]


@dataclass
class _RecordingMailer:
    sent: list[tuple[tuple[str, ...], str, str]] = field(default_factory=list)

    def send(
        self,
        *,
        to: Sequence[str],
        subject: str,
        body_text: str,
        body_html: str | None = None,
        headers: Mapping[str, str] | None = None,
        reply_to: str | None = None,
    ) -> str:
        del body_html, headers, reply_to
        self.sent.append((tuple(to), subject, body_text))
        return "msg-test"


def _settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("per-ip-budget-root-key"),
        public_url="https://crew.day",
    )


def _ctx(workspace: Workspace, *, actor_id: str | None = None) -> WorkspaceContext:
    return build_workspace_context(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor_id or new_ulid(),
        actor_was_owner_member=True,
    )


def _seed_workspace(
    session: Session,
    *,
    slug: str,
    signup_ip: str | None,
    verification_state: str = "unverified",
) -> Workspace:
    workspace = Workspace(
        id=new_ulid(),
        slug=slug,
        name=slug,
        plan="free",
        quota_json={},
        verification_state=verification_state,
        signup_ip=signup_ip,
        signup_ip_key=normalize_signup_ip_key(signup_ip),
        created_at=_NOW,
    )
    with tenant_agnostic():
        session.add(workspace)
        session.flush()
    return workspace


def _seed_ledger(session: Session, *, workspace_id: str, cap_cents: int) -> None:
    session.add(
        BudgetLedger(
            id=new_ulid(),
            workspace_id=workspace_id,
            period_start=_NOW - timedelta(days=30),
            period_end=_NOW,
            spent_cents=0,
            cap_cents=cap_cents,
            updated_at=_NOW,
        )
    )
    session.flush()


def _seed_usage(
    session: Session,
    *,
    workspace_id: str,
    cost_cents: int,
    created_at: datetime,
    status: str = "ok",
) -> None:
    session.add(
        LlmUsageRow(
            id=new_ulid(),
            workspace_id=workspace_id,
            capability=_CAPABILITY,
            provider_model_id="01HWA00000000000000000MDL0",
            tokens_in=0,
            tokens_out=0,
            cost_cents=cost_cents,
            latency_ms=0,
            status=status,
            correlation_id=new_ulid(),
            attempt=0,
            created_at=created_at,
        )
    )
    session.flush()


def _extract_token(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("https://"):
            return stripped.rsplit("/", 1)[-1]
    raise AssertionError(f"magic-link body did not contain a URL: {body!r}")


def test_normalize_signup_ip_key_ipv4_exact_ipv6_64() -> None:
    assert normalize_signup_ip_key("203.0.113.7") == "203.0.113.7"
    assert (
        normalize_signup_ip_key("2001:db8:abcd:1234:ffff::99")
        == "2001:db8:abcd:1234::/64"
    )
    assert normalize_signup_ip_key("") is None
    assert normalize_signup_ip_key("not-an-ip") is None


def test_complete_signup_uses_signup_start_ip(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_ip = "203.0.113.44"
    completion_ip = "198.51.100.99"
    attempt_id = new_ulid()
    with tenant_agnostic():
        db_session.add(
            SignupAttempt(
                id=attempt_id,
                email_lower="owner-start-ip@example.com",
                email_hash="e" * 64,
                desired_slug="start-ip-ws",
                ip_hash="a" * 64,
                signup_ip=start_ip,
                created_at=_NOW,
                expires_at=_NOW + timedelta(minutes=15),
                verified_at=_NOW,
                completed_at=None,
                workspace_id=None,
            )
        )
        db_session.flush()

    monkeypatch.setattr(
        passkey_module,
        "register_finish_signup",
        lambda *args, **kwargs: None,
    )

    completed = signup.complete_signup(
        db_session,
        signup_attempt_id=attempt_id,
        display_name="Owner",
        timezone="UTC",
        challenge_id="challenge-test",
        passkey_payload={},
        ip=completion_ip,
        settings=_settings(),
        now=_NOW,
    )

    workspace = db_session.scalar(
        select(Workspace).where(Workspace.id == completed.workspace_id)
    )
    assert workspace is not None
    assert workspace.signup_ip == start_ip
    assert workspace.signup_ip_key == "203.0.113.44"


def test_per_ip_cap_refuses_matching_unverified_pool(db_session: Session) -> None:
    clock = FrozenClock(_NOW)
    current = _seed_workspace(
        db_session, slug="ip-pool-current", signup_ip="203.0.113.10"
    )
    sibling = _seed_workspace(
        db_session,
        slug="ip-pool-sibling",
        signup_ip="203.0.113.10",
        verification_state="email_verified",
    )
    verified = _seed_workspace(
        db_session,
        slug="ip-pool-verified",
        signup_ip="203.0.113.10",
        verification_state="human_verified",
    )
    no_key = _seed_workspace(db_session, slug="ip-pool-no-key", signup_ip=None)
    _seed_ledger(db_session, workspace_id=current.id, cap_cents=10_000)

    _seed_usage(
        db_session,
        workspace_id=sibling.id,
        cost_cents=_IP_CAP - 5,
        created_at=_NOW - timedelta(days=1),
    )
    _seed_usage(
        db_session,
        workspace_id=verified.id,
        cost_cents=10_000,
        created_at=_NOW - timedelta(days=1),
    )
    _seed_usage(
        db_session,
        workspace_id=no_key.id,
        cost_cents=10_000,
        created_at=_NOW - timedelta(days=1),
    )
    _seed_usage(
        db_session,
        workspace_id=sibling.id,
        cost_cents=10_000,
        created_at=_NOW - timedelta(days=31),
    )
    _seed_usage(
        db_session,
        workspace_id=sibling.id,
        cost_cents=10_000,
        created_at=_NOW - timedelta(days=1),
        status="refused",
    )

    ctx = _ctx(current)
    check_budget(
        db_session,
        ctx,
        capability=_CAPABILITY,
        projected_cost_cents=5,
        clock=clock,
    )
    with pytest.raises(BudgetExceeded) as excinfo:
        check_budget(
            db_session,
            ctx,
            capability=_CAPABILITY,
            projected_cost_cents=6,
            clock=clock,
        )

    exc = excinfo.value
    assert exc.error_code == "ip_budget_exceeded"
    assert exc.message_text == IP_BUDGET_EXCEEDED_MESSAGE
    assert exc.to_dict()["error"] == "payment_required"
    assert exc.to_dict()["error_code"] == "ip_budget_exceeded"
    assert exc.to_dict()["message"] == IP_BUDGET_EXCEEDED_MESSAGE


def test_per_ip_cap_takes_precedence_over_workspace_cap(db_session: Session) -> None:
    clock = FrozenClock(_NOW)
    current = _seed_workspace(
        db_session, slug="ip-pool-precedence", signup_ip="203.0.113.11"
    )
    sibling = _seed_workspace(
        db_session, slug="ip-pool-precedence-sibling", signup_ip="203.0.113.11"
    )
    _seed_ledger(db_session, workspace_id=current.id, cap_cents=1)
    _seed_usage(
        db_session,
        workspace_id=sibling.id,
        cost_cents=_IP_CAP,
        created_at=_NOW - timedelta(days=1),
    )

    with pytest.raises(BudgetExceeded) as excinfo:
        check_budget(
            db_session,
            _ctx(current),
            capability=_CAPABILITY,
            projected_cost_cents=1,
            clock=clock,
        )

    assert excinfo.value.error_code == "ip_budget_exceeded"
    assert excinfo.value.to_dict()["error"] == "payment_required"


def test_human_verified_workspace_is_removed_from_ip_pool(
    db_session: Session,
) -> None:
    clock = FrozenClock(_NOW)
    workspace = _seed_workspace(
        db_session,
        slug="ip-pool-human",
        signup_ip="203.0.113.20",
        verification_state="human_verified",
    )
    sibling = _seed_workspace(
        db_session, slug="ip-pool-human-sibling", signup_ip="203.0.113.20"
    )
    _seed_ledger(db_session, workspace_id=workspace.id, cap_cents=10_000)
    _seed_usage(
        db_session,
        workspace_id=sibling.id,
        cost_cents=_IP_CAP,
        created_at=_NOW - timedelta(days=1),
    )

    check_budget(
        db_session,
        _ctx(workspace),
        capability=_CAPABILITY,
        projected_cost_cents=1,
        clock=clock,
    )


def test_ownership_verification_promotes_and_removes_from_pool(
    db_session: Session,
) -> None:
    clock = FrozenClock(_NOW)
    owner = bootstrap_user(
        db_session,
        email="owner-verify@example.com",
        display_name="Owner Verify",
        clock=clock,
    )
    workspace = bootstrap_workspace(
        db_session,
        slug="verify-ip-pool",
        name="Verify IP Pool",
        owner_user_id=owner.id,
        clock=clock,
    )
    sibling = _seed_workspace(
        db_session, slug="verify-ip-pool-sibling", signup_ip="2001:db8:aaaa:1::1"
    )
    workspace.signup_ip = "2001:db8:aaaa:1::99"
    workspace.signup_ip_key = normalize_signup_ip_key(workspace.signup_ip)
    workspace.verification_state = "unverified"
    db_session.flush()
    _seed_ledger(db_session, workspace_id=workspace.id, cap_cents=10_000)
    _seed_usage(
        db_session,
        workspace_id=sibling.id,
        cost_cents=_IP_CAP,
        created_at=_NOW - timedelta(days=1),
    )

    ctx = _ctx(workspace, actor_id=owner.id)
    with pytest.raises(BudgetExceeded):
        check_budget(
            db_session,
            ctx,
            capability=_CAPABILITY,
            projected_cost_cents=1,
            clock=clock,
        )

    mailer = _RecordingMailer()
    dispatch = request_ownership_verification(
        db_session,
        ctx,
        ip="198.51.100.7",
        mailer=mailer,
        base_url="https://crew.day",
        throttle=Throttle(),
        settings=_settings(),
        clock=clock,
    )
    dispatch.deliver()
    token = _extract_token(mailer.sent[0][2])

    state = consume_ownership_verification(
        db_session,
        ctx,
        token=token,
        ip="198.51.100.7",
        throttle=Throttle(),
        settings=_settings(),
        clock=clock,
    )

    assert state == "human_verified"
    assert workspace.verification_state == "human_verified"
    check_budget(
        db_session,
        ctx,
        capability=_CAPABILITY,
        projected_cost_cents=1,
        clock=clock,
    )


def test_ownership_verification_mismatch_does_not_consume_link(
    db_session: Session,
) -> None:
    clock = FrozenClock(_NOW)
    owner = bootstrap_user(
        db_session,
        email="owner-mismatch@example.com",
        display_name="Owner Mismatch",
        clock=clock,
    )
    first = bootstrap_workspace(
        db_session,
        slug="verify-first",
        name="Verify First",
        owner_user_id=owner.id,
        clock=clock,
    )
    second = bootstrap_workspace(
        db_session,
        slug="verify-second",
        name="Verify Second",
        owner_user_id=owner.id,
        clock=clock,
    )
    first.verification_state = "unverified"
    second.verification_state = "unverified"
    db_session.flush()

    mailer = _RecordingMailer()
    settings = _settings()
    dispatch = request_ownership_verification(
        db_session,
        _ctx(first, actor_id=owner.id),
        ip="198.51.100.7",
        mailer=mailer,
        base_url="https://crew.day",
        throttle=Throttle(),
        settings=settings,
        clock=clock,
    )
    dispatch.deliver()
    token = _extract_token(mailer.sent[0][2])

    with pytest.raises(WorkspaceVerificationMismatch):
        consume_ownership_verification(
            db_session,
            _ctx(second, actor_id=owner.id),
            token=token,
            ip="198.51.100.7",
            throttle=Throttle(),
            settings=settings,
            clock=clock,
        )

    state = consume_ownership_verification(
        db_session,
        _ctx(first, actor_id=owner.id),
        token=token,
        ip="198.51.100.7",
        throttle=Throttle(),
        settings=settings,
        clock=clock,
    )
    assert state == "human_verified"
