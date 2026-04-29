"""Unit tests for the demo-mode bootstrap guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.api.factory import DemoModeRefused, _enforce_demo_guard
from app.config import Settings


def _settings(
    *,
    demo_mode: bool,
    public_url: str | None = "https://demo.crew.day",
    database_url: str = "sqlite+aiosqlite:///data/demo.db",
    denylist: list[str] | None = None,
) -> Settings:
    return Settings.model_construct(
        database_url=database_url,
        data_dir=Path("."),
        public_url=public_url,
        demo_mode=demo_mode,
        demo_db_denylist=list(denylist or []),
    )


def test_demo_mode_off_does_not_apply_demo_guards() -> None:
    cfg = _settings(
        demo_mode=False,
        public_url="https://ops.example.com",
        database_url="postgresql://prod/crewday",
        denylist=["postgresql://prod/crewday"],
    )
    _enforce_demo_guard(cfg)


def test_demo_mode_allows_crewday_subdomain_and_unlisted_database() -> None:
    cfg = _settings(demo_mode=True, public_url="https://demo.crew.day")
    _enforce_demo_guard(cfg)


@pytest.mark.parametrize(
    "public_url",
    [
        None,
        "",
        "https://crew.day",
        "https://ops.example.com",
        "https://demo.crew.day.evil.example",
    ],
)
def test_demo_mode_refuses_non_demo_public_url(public_url: str | None) -> None:
    cfg = _settings(demo_mode=True, public_url=public_url)
    with pytest.raises(DemoModeRefused, match="CREWDAY_PUBLIC_URL"):
        _enforce_demo_guard(cfg)


def test_demo_mode_refuses_database_url_in_denylist() -> None:
    database_url = "postgresql+asyncpg://crewday:secret@prod/crewday"
    cfg = _settings(
        demo_mode=True,
        database_url=database_url,
        denylist=["sqlite:///other.db", database_url],
    )
    with pytest.raises(DemoModeRefused, match="CREWDAY_DEMO_DB_DENYLIST"):
        _enforce_demo_guard(cfg)
