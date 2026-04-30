"""Pure-probe unit tests for :mod:`app.capabilities`.

The DB-backed tests (``refresh_settings`` against real
``deployment_setting`` rows, migration round-trips) live in
``tests/integration/test_capabilities.py``; anything that only
exercises :func:`_probe_features` or dataclass invariants stays here
so it runs without the alembic harness.

See ``docs/specs/01-architecture.md`` §"Capability registry".
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest

from app.capabilities import (
    Capabilities,
    DeploymentSettings,
    RuntimeCapabilities,
    _probe_features,
    _sqlite_has_fts5,
    probe,
)
from app.config import Settings


def _sqlite_settings(
    *,
    database_url: str = "sqlite:///:memory:",
    storage_backend: Literal["localfs", "s3"] = "localfs",
    demo_mode: bool = False,
) -> Settings:
    """Build a :class:`Settings` pinned to SQLite with selective overrides.

    ``model_construct`` bypasses env loading so host vars can't leak
    into the probe — tests own every knob that matters. Only the two
    fields the probe reads are parameterised; everything else gets
    the static defaults the real :class:`Settings` would emit.
    """
    return Settings.model_construct(
        database_url=database_url,
        bind_host="127.0.0.1",
        bind_port=8000,
        trusted_interfaces=["tailscale*"],
        allow_public_bind=False,
        data_dir=Path("."),
        public_url=None,
        smtp_host=None,
        smtp_port=587,
        smtp_user=None,
        smtp_password=None,
        openrouter_api_key=None,
        root_key=None,
        demo_mode=demo_mode,
        worker="internal",
        storage_backend=storage_backend,
    )


class TestProbeFeaturesSqlite:
    def test_sqlite_url_disables_rls_and_concurrent_writers(self) -> None:
        features = _probe_features(_sqlite_settings())
        assert features.rls is False
        assert features.concurrent_writers is False

    def test_sqlite_fulltext_matches_interpreter_build(self) -> None:
        """``fulltext_search`` must mirror the live sqlite3 FTS5 probe.

        We don't hard-code ``True``: the CPython build used in CI
        has FTS5, but an Alpine python without it must report False.
        Consistency between the public probe and the inner helper
        is what matters.
        """
        features = _probe_features(_sqlite_settings())
        assert features.fulltext_search is _sqlite_has_fts5()

    def test_sqlite_fulltext_false_when_fts5_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the live sqlite lacks FTS5, ``fulltext_search`` is False."""
        monkeypatch.setattr("app.capabilities._sqlite_has_fts5", lambda: False)
        features = _probe_features(_sqlite_settings())
        assert features.fulltext_search is False

    def test_sqlite_fulltext_true_when_fts5_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.capabilities._sqlite_has_fts5", lambda: True)
        features = _probe_features(_sqlite_settings())
        assert features.fulltext_search is True


class TestProbeFeaturesPostgres:
    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://u:p@localhost/db",
            "postgres://u:p@localhost/db",
            "postgresql+psycopg://u:p@localhost/db",
            "postgresql+asyncpg://u:p@localhost/db",
            "postgres+psycopg://u:p@localhost/db",
            # Mixed-case should still be matched — the probe lowercases.
            "PostgreSQL://u:p@localhost/db",
        ],
    )
    def test_postgres_urls_enable_rls_concurrent_writers_and_fts(
        self, url: str
    ) -> None:
        features = _probe_features(_sqlite_settings(database_url=url))
        assert features.rls is True
        assert features.concurrent_writers is True
        assert features.fulltext_search is True


class TestProbeFeaturesUnknownBackend:
    """Unknown DB URLs (mysql, oracle, …) must not borrow SQLite FTS5."""

    def test_mysql_url_leaves_fulltext_search_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FTS5 on the interpreter's SQLite says nothing about a mysql DB.

        Pin the inner probe to ``True`` so the test catches any code
        path that silently lets SQLite's FTS5 leak across to an
        unrelated backend.
        """
        monkeypatch.setattr("app.capabilities._sqlite_has_fts5", lambda: True)
        features = _probe_features(_sqlite_settings(database_url="mysql://u:p@h/db"))
        assert features.rls is False
        assert features.concurrent_writers is False
        assert features.fulltext_search is False

    def test_oracle_url_leaves_fulltext_search_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.capabilities._sqlite_has_fts5", lambda: True)
        features = _probe_features(_sqlite_settings(database_url="oracle://u:p@h/db"))
        assert features.fulltext_search is False


class TestProbeFeaturesStorage:
    def test_s3_storage_enables_object_storage(self) -> None:
        features = _probe_features(_sqlite_settings(storage_backend="s3"))
        assert features.object_storage is True

    def test_localfs_storage_disables_object_storage(self) -> None:
        features = _probe_features(_sqlite_settings(storage_backend="localfs"))
        assert features.object_storage is False


class TestProbeFeaturesStubs:
    """The v1-stubbed feature fields stay False regardless of settings."""

    def test_stubs_are_false_on_sqlite(self) -> None:
        features = _probe_features(_sqlite_settings())
        assert features.wildcard_subdomains is False
        assert features.email_bounce_webhooks is False
        assert features.llm_voice_input is False
        assert features.postgis is False

    def test_stubs_are_false_on_postgres(self) -> None:
        features = _probe_features(
            _sqlite_settings(database_url="postgresql://u:p@h/db")
        )
        assert features.wildcard_subdomains is False
        assert features.email_bounce_webhooks is False
        assert features.llm_voice_input is False
        assert features.postgis is False


class TestProbeWithoutSession:
    def test_defaults_applied_when_session_none(self) -> None:
        caps = probe(_sqlite_settings(), session=None)
        assert isinstance(caps, Capabilities)
        assert caps.settings.signup_enabled is True
        assert caps.settings.signup_throttle_overrides == {}
        assert caps.settings.require_passkey_attestation is False
        assert caps.settings.llm_default_budget_cents_30d == 500
        # cd-055: captcha_required defaults on (SaaS default); operators
        # toggle off via deployment_setting for self-host.
        assert caps.settings.captcha_required is True
        assert caps.settings.marketplace_enabled is False
        assert caps.settings.platform_fee_default_bps == 1000
        assert caps.settings.platform_fee_currency_policy == "match_source"

    def test_features_populated_when_session_none(self) -> None:
        caps = probe(_sqlite_settings(storage_backend="s3"), session=None)
        assert caps.features.object_storage is True
        assert caps.features.rls is False

    def test_runtime_demo_mode_populated_from_settings(self) -> None:
        caps = probe(_sqlite_settings(demo_mode=True), session=None)
        assert caps.runtime.demo_mode is True
        assert caps.has("runtime.demo_mode") is True
        assert caps.has("settings.marketplace_enabled") is False
        assert caps.has("features.postgis") is False

    def test_runtime_demo_mode_defaults_false(self) -> None:
        caps = probe(_sqlite_settings(), session=None)
        assert caps.runtime == RuntimeCapabilities(demo_mode=False)
        assert caps.has("runtime.demo_mode") is False

    def test_unknown_capability_key_raises(self) -> None:
        caps = probe(_sqlite_settings(), session=None)
        with pytest.raises(KeyError):
            caps.has("runtime.not_real")


class TestProbeLogsSnapshotOnce:
    def test_boot_log_is_emitted_exactly_once(
        self,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """One INFO snapshot line per :func:`probe` call.

        The ``allow_propagated_log_capture`` fixture
        (``tests/conftest.py``) re-enables propagation for the
        capabilities logger after alembic's ``fileConfig`` flipped it
        during an earlier integration fixture — otherwise the INFO
        line never reaches the root logger and ``caplog.records`` is
        empty. See cd-0dyv for the root cause.
        """
        allow_propagated_log_capture("app.capabilities")
        caplog.set_level(logging.INFO, logger="app.capabilities")
        probe(_sqlite_settings(), session=None)
        snapshot_lines = [
            record
            for record in caplog.records
            if record.name == "app.capabilities"
            and "capabilities snapshot" in record.getMessage()
        ]
        assert len(snapshot_lines) == 1
        assert snapshot_lines[0].levelno == logging.INFO


class TestFrozenFeatures:
    def test_mutating_rls_raises_frozen_instance_error(self) -> None:
        """Frozen dataclass: direct assignment to ``rls`` must raise.

        The field name is chosen at runtime from
        :func:`dataclasses.fields` so mypy's strict read-only check
        doesn't fire on a known field name — ruff B010 is happy
        because the attribute name isn't a hard-coded constant, and
        the hard rules stay clean of ``# type: ignore``.
        """
        features = _probe_features(_sqlite_settings())
        field_name = dataclasses.fields(features)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(features, field_name, True)

    def test_mutating_any_field_raises_frozen_instance_error(self) -> None:
        """Every declared field must be immutable, not just ``rls``."""
        features = _probe_features(_sqlite_settings())
        for name in (f.name for f in dataclasses.fields(features)):
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(features, name, True)


class TestDeploymentSettingsIsMutable:
    def test_settings_instance_accepts_direct_assignment(self) -> None:
        """``DeploymentSettings`` is deliberately NOT frozen.

        :meth:`Capabilities.refresh_settings` re-points fields in
        place so callers holding a reference to :class:`Capabilities`
        observe new values without a re-lookup.
        """
        settings = DeploymentSettings()
        settings.signup_enabled = False
        assert settings.signup_enabled is False


class TestSqliteFts5Probe:
    def test_direct_probe_returns_bool(self) -> None:
        """Sanity check: the probe always returns a bool, never raises."""
        result = _sqlite_has_fts5()
        assert isinstance(result, bool)
