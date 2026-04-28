"""Integration coverage for host-only ``crewday admin init``."""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.capabilities.models import DeploymentSetting
from app.admin.init import admin_init
from app.config import Settings
from app.tenancy import tenant_agnostic

pytestmark = pytest.mark.integration


def _settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-admin-init-root-key"),
        public_url="https://ops.example.test",
        demo_mode=False,
    )


def test_admin_init_is_idempotent_and_does_not_store_generated_root_key(
    db_session: Session,
) -> None:
    generated = "generated-root-key"
    first = admin_init(
        db_session,
        settings=_settings(),
        generated_root_key=generated,
    )
    second = admin_init(
        db_session,
        settings=_settings(),
        generated_root_key="should-not-repeat",
    )

    assert first.initialized is True
    assert first.generated_root_key == generated
    assert first.settings_seeded >= 5
    assert second.initialized is False
    assert second.generated_root_key is None

    with tenant_agnostic():
        rows = {
            row.key: row.value
            for row in db_session.scalars(select(DeploymentSetting)).all()
        }
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "admin.init")
        ).all()

    assert rows["admin_init_completed"] is True
    assert rows["admin_init_root_key_generated"] is True
    assert generated not in {str(value) for value in rows.values()}
    assert rows["signup_enabled"] is True
    assert len(audits) == 1
    assert audits[0].actor_kind == "system"
    assert audits[0].via == "cli"
    assert audits[0].scope_kind == "deployment"


def test_admin_init_refuses_demo_mode(db_session: Session) -> None:
    settings = Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-admin-init-root-key"),
        demo_mode=True,
    )

    with pytest.raises(RuntimeError, match="admin commands not available in demo"):
        admin_init(db_session, settings=settings, generated_root_key=None)
