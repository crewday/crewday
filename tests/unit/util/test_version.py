"""Unit tests for :func:`app.util.version.resolve_package_version`.

Both branches matter — the installed-package path is exercised by the
``/version`` integration test against real wiring, but the
``PackageNotFoundError`` fallback is hard to reach without monkeypatching
since the dev container has the package installed editable. We pin both
here so a future refactor of the helper can't silently drop either.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError

import pytest

import app.util.version as version_module
from app.util.version import resolve_package_version


class TestResolvePackageVersion:
    def test_returns_installed_version_when_package_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Happy path — the helper forwards the importlib.metadata answer."""

        def _fake(name: str) -> str:
            assert name == "crewday"
            return "1.2.3"

        monkeypatch.setattr(version_module, "_pkg_version", _fake)
        assert resolve_package_version("0.0.0+unknown") == "1.2.3"

    def test_returns_fallback_when_package_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``PackageNotFoundError`` surfaces as the caller's sentinel."""

        def _raise(_name: str) -> str:
            raise PackageNotFoundError("crewday")

        monkeypatch.setattr(version_module, "_pkg_version", _raise)
        assert resolve_package_version("0.0.0+unknown") == "0.0.0+unknown"
        assert resolve_package_version("unknown") == "unknown"
