"""Tests for host-only ``crewday admin`` overrides."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from crewday._overrides import admin


def test_admin_init_demo_refusal_happens_before_migrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Demo mode must refuse before the command touches the database."""
    migrations_called = False

    def run_migrations() -> None:
        nonlocal migrations_called
        migrations_called = True

    monkeypatch.setattr(
        admin,
        "_load_app_admin",
        lambda: SimpleNamespace(
            ADMIN_DEMO_REFUSAL="admin commands not available in demo"
        ),
    )
    monkeypatch.setattr(admin, "_make_uow", lambda: object())
    monkeypatch.setattr(admin, "_settings", lambda: SimpleNamespace(demo_mode=True))
    monkeypatch.setattr(admin, "_run_migrations", run_migrations)

    result = CliRunner().invoke(admin.init, [])

    assert result.exit_code == 5
    assert "admin commands not available in demo" in result.output
    assert migrations_called is False
