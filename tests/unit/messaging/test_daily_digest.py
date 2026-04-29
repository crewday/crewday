"""Unit tests for the daily digest worker."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.llm.models import BudgetLedger, LlmUsage
from app.adapters.db.messaging.models import DigestRecord, Notification
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import Workspace
from app.adapters.llm.ports import ChatMessage, LLMResponse
from app.adapters.llm.ports import LLMUsage as PortUsage
from app.domain.llm.router import ModelPick
from app.tenancy.context import WorkspaceContext
from app.util.clock import Clock, FrozenClock
from app.util.ulid import new_ulid
from app.worker.tasks.daily_digest import send_daily_digest
from tests._fakes.mailer import InMemoryMailer

_NOW = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


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
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


def _ctx(workspace_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="smoke",
        actor_id="00000000000000000000000000",
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id="00000000000000000000000000",
        principal_kind="system",
    )


def _bootstrap(
    session: Session,
    *,
    with_ledger: bool = True,
    add_occurrence: bool = True,
    grant_role: str = "manager",
    user_email: str = "manager@example.test",
    display_name: str = "Manager",
    user_timezone: str = "UTC",
    occurrence_title: str = "Inspect pool",
) -> tuple[str, str]:
    workspace_id = new_ulid()
    user_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug="smoke",
            name="Smoke",
            plan="free",
            quota_json={},
            settings_json={},
            default_timezone="UTC",
            default_locale="en",
            default_currency="USD",
            created_at=_NOW,
        )
    )
    session.add(
        User(
            id=user_id,
            email=user_email,
            email_lower=canonicalise_email(user_email),
            display_name=display_name,
            locale=None,
            timezone=user_timezone,
            created_at=_NOW,
        )
    )
    session.flush()
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_kind="workspace",
            created_at=_NOW,
        )
    )
    if add_occurrence:
        session.add(
            Occurrence(
                id=new_ulid(),
                workspace_id=workspace_id,
                assignee_user_id=user_id if grant_role == "worker" else None,
                starts_at=_NOW.replace(hour=14),
                ends_at=_NOW.replace(hour=15),
                state="pending",
                title=occurrence_title,
                created_at=_NOW,
            )
        )
    if with_ledger:
        session.add(
            BudgetLedger(
                id=new_ulid(),
                workspace_id=workspace_id,
                period_start=_NOW - timedelta(days=30),
                period_end=_NOW + timedelta(seconds=1),
                spent_cents=0,
                cap_cents=0,
                updated_at=_NOW,
            )
        )
    session.flush()
    return workspace_id, user_id


class _DigestLLM:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[str] = []

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.prompts.append(messages[-1]["content"])
        return LLMResponse(
            text=self.text,
            usage=PortUsage(prompt_tokens=12, completion_tokens=8, total_tokens=20),
            model_id=model_id,
            finish_reason="stop",
        )

    def complete(
        self,
        *,
        model_id: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        raise AssertionError("daily digest should use chat")

    def ocr(self, *, model_id: str, image_bytes: bytes) -> str:
        raise AssertionError("daily digest should not OCR")

    def stream_chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Iterator[str]:
        raise AssertionError("daily digest should not stream")


def _models(
    session: Session,
    ctx: WorkspaceContext,
    clock: Clock,
) -> Sequence[ModelPick]:
    return (
        ModelPick(
            provider_model_id="pm_daily",
            api_model_id="test/digest",
            max_tokens=300,
            temperature=0.2,
            assignment_id="assign_daily",
        ),
    )


def test_daily_digest_uses_llm_body_and_records_digest(session: Session) -> None:
    workspace_id, user_id = _bootstrap(
        session,
        user_email="maria.manager@example.test",
        display_name="Maria Manager",
        occurrence_title="Inspect pool",
    )
    mailer = InMemoryMailer()
    llm = _DigestLLM("### Good morning\n\nYou have one thing to handle.")

    report = send_daily_digest(
        _ctx(workspace_id),
        session=session,
        mailer=mailer,
        llm=llm,
        clock=FrozenClock(_NOW),
        resolve_models=_models,
    )

    assert report.sent == 1
    assert report.llm_rendered == 1
    assert report.template_rendered == 0
    assert len(mailer.sent) == 1
    assert "Good morning" in mailer.sent[0].body_text
    assert "Inspect pool" not in llm.prompts[0]
    assert "maria.manager@example.test" not in llm.prompts[0]
    assert "Maria Manager" not in llm.prompts[0]

    digest = session.scalar(
        select(DigestRecord).where(DigestRecord.recipient_user_id == user_id)
    )
    assert digest is not None
    assert "Good morning" in digest.body_md

    notification = session.scalar(
        select(Notification).where(Notification.recipient_user_id == user_id)
    )
    assert notification is not None
    assert notification.kind == "daily_digest"
    assert session.scalar(select(LlmUsage)) is not None


def test_daily_digest_falls_back_when_llm_budget_is_blocked(session: Session) -> None:
    workspace_id, user_id = _bootstrap(session, with_ledger=False)
    mailer = InMemoryMailer()
    llm = _DigestLLM("This should not be used")

    report = send_daily_digest(
        _ctx(workspace_id),
        session=session,
        mailer=mailer,
        llm=llm,
        clock=FrozenClock(_NOW),
        resolve_models=_models,
    )

    assert report.sent == 1
    assert report.llm_rendered == 0
    assert report.template_rendered == 1
    assert len(mailer.sent) == 1
    assert "Scheduled tasks: 1" in mailer.sent[0].body_text
    assert "Inspect pool" in mailer.sent[0].body_text
    assert llm.prompts == []
    assert session.scalar(select(LlmUsage)) is None

    report = send_daily_digest(
        _ctx(workspace_id),
        session=session,
        mailer=mailer,
        llm=llm,
        clock=FrozenClock(_NOW),
        resolve_models=_models,
    )
    assert report.sent == 0
    assert report.skipped_existing == 1
    assert (
        session.scalar(
            select(DigestRecord).where(DigestRecord.recipient_user_id == user_id)
        )
        is not None
    )


def test_daily_digest_skips_empty_day_unless_always_send_empty(
    session: Session,
) -> None:
    workspace_id, user_id = _bootstrap(session, add_occurrence=False)
    mailer = InMemoryMailer()

    report = send_daily_digest(
        _ctx(workspace_id),
        session=session,
        mailer=mailer,
        clock=FrozenClock(_NOW),
    )

    assert report.sent == 0
    assert report.skipped_empty == 1
    assert mailer.sent == []
    assert session.scalar(select(DigestRecord)) is None

    report = send_daily_digest(
        _ctx(workspace_id),
        session=session,
        mailer=mailer,
        clock=FrozenClock(_NOW),
        always_send_empty=True,
    )

    assert report.sent == 1
    assert len(mailer.sent) == 1
    assert (
        session.scalar(
            select(DigestRecord).where(DigestRecord.recipient_user_id == user_id)
        )
        is not None
    )


def test_daily_digest_due_hour_uses_recipient_local_timezone(
    session: Session,
) -> None:
    workspace_id, user_id = _bootstrap(
        session,
        grant_role="worker",
        user_timezone="America/New_York",
    )
    mailer = InMemoryMailer()

    report = send_daily_digest(
        _ctx(workspace_id),
        session=session,
        mailer=mailer,
        clock=FrozenClock(datetime(2026, 4, 29, 10, 0, 0, tzinfo=UTC)),
        due_local_hour=7,
    )

    assert report.sent == 0
    assert report.skipped_not_due == 1
    assert mailer.sent == []

    report = send_daily_digest(
        _ctx(workspace_id),
        session=session,
        mailer=mailer,
        clock=FrozenClock(datetime(2026, 4, 29, 11, 0, 0, tzinfo=UTC)),
        due_local_hour=7,
    )

    assert report.sent == 1
    assert len(mailer.sent) == 1
    assert (
        session.scalar(
            select(DigestRecord).where(DigestRecord.recipient_user_id == user_id)
        )
        is not None
    )
