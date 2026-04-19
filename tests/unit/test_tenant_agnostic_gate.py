"""Tests for ``scripts/check_tenant_agnostic.py``.

The script is the CI gate that forces every :func:`tenant_agnostic` call
site to carry a ``# justification:`` comment. Tests cover:

* a clean file with a justified call exits 0,
* an unjustified call exits 1 with the offender on stderr,
* a bare import of ``tenant_agnostic`` (identifier only, no parentheses)
  does not trip the grep,
* a ``def tenant_agnostic`` definition line is skipped so the function's
  own source file does not produce a false positive,
* a justification on the call line itself is accepted,
* a justification 1 or 2 lines above the call is accepted,
* a justification 3 lines above is rejected (outside the window).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_tenant_agnostic.py"


def _run(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_justified_call_passes(tmp_path: Path) -> None:
    target = tmp_path / "ok.py"
    target.write_text(
        "# justification: deployment admin export\nwith tenant_agnostic():\n    pass\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_unjustified_call_fails(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text("with tenant_agnostic():\n    pass\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "bad.py:1" in result.stderr


def test_justification_on_call_line_passes(tmp_path: Path) -> None:
    target = tmp_path / "inline.py"
    target.write_text(
        "with tenant_agnostic():  # justification: admin export\n    pass\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_justification_one_line_above_passes(tmp_path: Path) -> None:
    target = tmp_path / "one_above.py"
    target.write_text(
        "x = 1\n# justification: auth bootstrap\nwith tenant_agnostic():\n    pass\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_justification_two_lines_above_passes(tmp_path: Path) -> None:
    target = tmp_path / "two_above.py"
    target.write_text(
        "# justification: auth bootstrap\nx = 1\nwith tenant_agnostic():\n    pass\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_justification_three_lines_above_fails(tmp_path: Path) -> None:
    target = tmp_path / "three_above.py"
    target.write_text(
        "# justification: too far away\n"
        "x = 1\n"
        "y = 2\n"
        "with tenant_agnostic():\n"
        "    pass\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "three_above.py:4" in result.stderr


def test_bare_import_is_not_flagged(tmp_path: Path) -> None:
    target = tmp_path / "import_only.py"
    target.write_text("from app.tenancy import tenant_agnostic\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_def_line_is_skipped(tmp_path: Path) -> None:
    target = tmp_path / "definition.py"
    target.write_text(
        "def tenant_agnostic():\n"
        "    '''stand-in for the real definition.'''\n"
        "    return None\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_excludes_tests_directory(tmp_path: Path) -> None:
    """Paths under ``tests/`` are excluded from the gate."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    target = tests_dir / "use.py"
    target.write_text("with tenant_agnostic():\n    pass\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_excludes_mocks_directory(tmp_path: Path) -> None:
    mocks_dir = tmp_path / "mocks"
    mocks_dir.mkdir()
    target = mocks_dir / "use.py"
    target.write_text("with tenant_agnostic():\n    pass\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_nonexistent_path_exits_usage_error(tmp_path: Path) -> None:
    bogus = tmp_path / "does-not-exist"
    result = _run(bogus)
    assert result.returncode == 2
    assert "does not exist" in result.stderr


def test_mixed_file_reports_only_unjustified(tmp_path: Path) -> None:
    target = tmp_path / "mixed.py"
    target.write_text(
        "# justification: fine\n"
        "with tenant_agnostic():\n"
        "    pass\n"
        "\n"
        "with tenant_agnostic():\n"
        "    pass\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "mixed.py:5" in result.stderr
    assert "mixed.py:2" not in result.stderr


@pytest.mark.parametrize(
    "variant",
    [
        "# justification: foo",
        "# JUSTIFICATION: foo",
        "# Justification : foo",
        "#justification: foo",
    ],
)
def test_justification_comment_variants(tmp_path: Path, variant: str) -> None:
    target = tmp_path / "variant.py"
    target.write_text(f"{variant}\nwith tenant_agnostic():\n    pass\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
