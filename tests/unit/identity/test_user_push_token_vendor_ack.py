"""Vendor-ack disable + retention tests for native push tokens."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, UserPushToken, canonicalise_email
from app.adapters.db.identity.repositories import SqlAlchemyUserPushTokenRepository
from app.adapters.db.session import make_engine
from app.domain.identity.push_tokens import register
from app.domain.privacy import rotate_operational_logs
from app.tenancy import tenant_agnostic
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.jobs.messaging_native_push import handle_native_push_vendor_ack

_NOW = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
_AGNOSTIC_WORKSPACE_ID = "00000000000000000000000000"


def _load_all_models() -> None:
    import importlib
    import pkgutil

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


def _user(s: Session) -> str:
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email="push-owner@example.com",
            email_lower=canonicalise_email("push-owner@example.com"),
            display_name="Push Owner",
            created_at=_NOW,
        )
    )
    s.flush()
    return user_id


def _register_token(
    session: Session,
    *,
    user_id: str,
    token: str,
    device_label: str | None,
) -> str:
    view = register(
        SqlAlchemyUserPushTokenRepository(session),
        user_id=user_id,
        platform="android",
        token=token,
        device_label=device_label,
        clock=FrozenClock(_NOW - timedelta(days=100)),
    )
    return view.id


def _disabled_audits(session: Session) -> list[AuditLog]:
    with tenant_agnostic():
        return list(
            session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.workspace_id == _AGNOSTIC_WORKSPACE_ID,
                    AuditLog.action == "user_push_token.disabled",
                )
                .order_by(AuditLog.created_at.asc())
            ).all()
        )


def test_vendor_ack_disables_audits_once_and_retention_purges_old_rows(
    session: Session,
    tmp_path: Path,
) -> None:
    user_id = _user(session)
    old_token_id = _register_token(
        session, user_id=user_id, token="fcm-old", device_label="Pixel"
    )
    recent_token_id = _register_token(
        session, user_id=user_id, token="fcm-recent", device_label="Tablet"
    )
    active_token_id = _register_token(
        session, user_id=user_id, token="fcm-active", device_label="Phone"
    )
    session.commit()

    old_disabled_at = _NOW - timedelta(days=91)
    assert handle_native_push_vendor_ack(
        session,
        token_id=old_token_id,
        vendor_reason="unregistered",
        clock=FrozenClock(old_disabled_at),
    )
    assert not handle_native_push_vendor_ack(
        session,
        token_id=old_token_id,
        vendor_reason="unregistered",
        clock=FrozenClock(old_disabled_at + timedelta(minutes=1)),
    )
    assert handle_native_push_vendor_ack(
        session,
        token_id=recent_token_id,
        vendor_reason="invalid",
        clock=FrozenClock(_NOW - timedelta(days=10)),
    )
    assert not handle_native_push_vendor_ack(
        session,
        token_id=active_token_id,
        vendor_reason="token_unauthenticated",
        clock=FrozenClock(_NOW),
    )
    session.flush()

    audits = _disabled_audits(session)
    assert len(audits) == 2
    first_diff = audits[0].diff
    assert isinstance(first_diff, dict)
    assert first_diff == {
        "user_id": user_id,
        "platform": "android",
        "device_label": "Pixel",
        "vendor_reason": "unregistered",
    }
    assert "token" not in first_diff

    results = rotate_operational_logs(
        session,
        data_dir=tmp_path,
        clock=FrozenClock(_NOW),
    )
    session.commit()

    assert session.get(UserPushToken, old_token_id) is None
    assert session.get(UserPushToken, recent_token_id) is not None
    active = session.get(UserPushToken, active_token_id)
    assert active is not None
    assert active.disabled_at is None
    assert any(
        result.table == "user_push_token" and result.archived_rows == 1
        for result in results
    )
    assert not (tmp_path / "archive" / "user_push_token.jsonl.gz").exists()
