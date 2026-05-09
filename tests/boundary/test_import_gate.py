"""Boundary tests for the import-linter gate (cd-ev0).

These tests exercise the import-boundary contracts declared in
``pyproject.toml`` under ``[tool.importlinter]``. The spec is
``docs/specs/01-architecture.md`` §"Module boundaries" (rules 1-6)
and ``docs/specs/17-testing-quality.md`` §"Import boundaries".

Two scenarios:

* **Positive** — ``uv run lint-imports`` on the clean tree exits 0.
  Guards against a future change accidentally introducing a
  cross-boundary import or breaking the config.
* **Negative** — a temporary miniature ``app`` package containing a
  bad ``app.domain -> app.adapters`` import causes ``lint-imports`` to
  exit non-zero. Guards against a silent misconfiguration of the gate
  (e.g. a typo in ``source_modules`` would still report "all
  kept").

The negative test must not write generated Python files under the real
``app/`` tree. Several suite-level smoke tests scan ``app/**/*.py`` while
pytest-xdist runs unrelated modules in parallel; a transient generated
file there can race those scanners.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path

# Repository root = three levels above this file
# (tests/boundary/test_import_gate.py -> tests/boundary -> tests -> repo).
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DOMAIN_ADAPTERS_CONTRACT_NAME = "Domain forbids adapters (except ports)"

BAD_MODULE_CONTENTS: str = (
    '"""Deliberately bad import used by tests/boundary/test_import_gate.py.\n\n'
    "This file lives only inside pytest's temporary directory.\n"
    '"""\n\n'
    "from app.adapters.db.session import make_engine  # noqa: F401\n"
)


def _run_lint_imports() -> subprocess.CompletedProcess[str]:
    """Invoke ``uv run lint-imports`` from the repo root and capture output."""
    return subprocess.run(
        ["uv", "run", "lint-imports"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_lint_imports_in_sandbox(
    sandbox: Path,
) -> subprocess.CompletedProcess[str]:
    """Invoke repo-managed ``lint-imports`` against a temp project."""
    return subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPO_ROOT),
            "lint-imports",
            "--config",
            str(sandbox / "pyproject.toml"),
            "--no-cache",
        ],
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
    )


def _domain_adapters_contract() -> Mapping[str, object]:
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    importlinter = pyproject["tool"]["importlinter"]
    assert "app" in importlinter["root_packages"]
    for contract in importlinter["contracts"]:
        if contract["name"] == DOMAIN_ADAPTERS_CONTRACT_NAME:
            return contract
    raise AssertionError(
        f"Missing import-linter contract: {DOMAIN_ADAPTERS_CONTRACT_NAME}"
    )


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"Unsupported TOML test value: {value!r}")


def _write_domain_adapters_config(path: Path) -> None:
    contract = _domain_adapters_contract()
    lines = [
        "[tool.importlinter]",
        'root_packages = ["app"]',
        "include_external_packages = true",
        "",
        "[[tool.importlinter.contracts]]",
    ]
    for key in (
        "name",
        "type",
        "source_modules",
        "forbidden_modules",
        "ignore_imports",
        "unmatched_ignore_imports_alerting",
    ):
        lines.append(f"{key} = {_toml_value(contract[key])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_package(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "__init__.py").write_text("", encoding="utf-8")


def test_clean_tree_passes() -> None:
    """``lint-imports`` on the untouched repo must exit 0.

    Acts as the baseline that proves the three boundary contracts
    are satisfied today. A regression here means something in
    ``app/`` started importing across a forbidden seam.
    """
    result = _run_lint_imports()
    assert result.returncode == 0, (
        f"lint-imports unexpectedly failed on the clean tree.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_cross_boundary_import_is_rejected(tmp_path: Path) -> None:
    """A deliberately bad cross-boundary import must fail ``lint-imports``.

    Builds a minimal temporary ``app`` package with a bad
    ``app.domain.identity.bad`` module that imports from
    ``app.adapters.db.session`` — a violation of the "Domain forbids
    adapters (except ports)" contract. Exit code must be non-zero and
    the bad import must appear in stdout.
    """
    _write_domain_adapters_config(tmp_path / "pyproject.toml")
    for package in (
        tmp_path / "app",
        tmp_path / "app" / "adapters",
        tmp_path / "app" / "adapters" / "db",
        tmp_path / "app" / "domain",
        tmp_path / "app" / "domain" / "identity",
    ):
        _write_package(package)
    (tmp_path / "app" / "adapters" / "db" / "session.py").write_text(
        "def make_engine() -> None:\n    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "domain" / "identity" / "bad.py").write_text(
        BAD_MODULE_CONTENTS,
        encoding="utf-8",
    )

    result = _run_lint_imports_in_sandbox(tmp_path)

    assert result.returncode != 0, (
        "lint-imports accepted a domain -> adapters import. "
        "The boundary gate is not enforcing rule 1.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # The import-linter report names the offending edge. Assert it
    # surfaced so a future config change that silently flips the
    # contract into "skip" mode still fails the test.
    combined = result.stdout + result.stderr
    assert "app.adapters.db.session" in combined, (
        "lint-imports exited non-zero but did not report the expected "
        f"offending edge.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
