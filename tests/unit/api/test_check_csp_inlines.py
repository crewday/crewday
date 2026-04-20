"""Unit tests for ``scripts/check_csp_inlines.py``.

The linter is the build-time gate backing the CSP's "only nonced
inlines" invariant (§15 "HTTP security headers"). These tests drive it
against fixture HTML snippets written into ``tmp_path`` so the suite
never depends on a real SPA build.

See ``docs/specs/15-security-privacy.md`` §"HTTP security headers".
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_linter() -> ModuleType:
    """Import the script under test by file path.

    The ``scripts/`` directory is not a package, so the usual
    ``import scripts.check_csp_inlines`` fails. Loading via
    :func:`importlib.util.spec_from_file_location` keeps the helper
    reachable without adding a ``scripts/__init__.py`` (which would
    bleed scripts into the importable namespace on every deployment).
    """
    module_name = "scripts_check_csp_inlines"
    if module_name in sys.modules:
        return sys.modules[module_name]

    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "check_csp_inlines.py"
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_HTML_NONCED_SCRIPT = """\
<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body>
<script nonce="abc123">window.bootstrap();</script>
</body>
</html>
"""

_HTML_UNNONCED_SCRIPT = """\
<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body>
<script>window.bootstrap();</script>
</body>
</html>
"""

_HTML_UNNONCED_STYLE = """\
<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body>
<style>body { background: red; }</style>
</body>
</html>
"""

_HTML_EXTERNAL_SCRIPT = """\
<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body>
<script src="/assets/index-abc.js" type="module"></script>
</body>
</html>
"""

_HTML_NONCED_STYLE = """\
<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body>
<style nonce="abc123">body { background: red; }</style>
</body>
</html>
"""


class TestLinter:
    """``main(argv)`` returns the correct exit code for fixture HTML."""

    def test_nonced_inline_script_exits_zero(self, tmp_path: Path) -> None:
        fixture = tmp_path / "index.html"
        fixture.write_text(_HTML_NONCED_SCRIPT, encoding="utf-8")
        linter = _load_linter()
        assert linter.main([str(fixture)]) == 0

    def test_nonced_inline_style_exits_zero(self, tmp_path: Path) -> None:
        fixture = tmp_path / "index.html"
        fixture.write_text(_HTML_NONCED_STYLE, encoding="utf-8")
        linter = _load_linter()
        assert linter.main([str(fixture)]) == 0

    def test_external_script_exits_zero(self, tmp_path: Path) -> None:
        """External ``<script src=...>`` is not in scope for this gate."""
        fixture = tmp_path / "index.html"
        fixture.write_text(_HTML_EXTERNAL_SCRIPT, encoding="utf-8")
        linter = _load_linter()
        assert linter.main([str(fixture)]) == 0

    def test_unnonced_inline_script_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fixture = tmp_path / "index.html"
        fixture.write_text(_HTML_UNNONCED_SCRIPT, encoding="utf-8")
        linter = _load_linter()
        assert linter.main([str(fixture)]) == 1
        err = capsys.readouterr().err
        assert "un-nonced inline <script>" in err

    def test_unnonced_inline_style_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fixture = tmp_path / "index.html"
        fixture.write_text(_HTML_UNNONCED_STYLE, encoding="utf-8")
        linter = _load_linter()
        assert linter.main([str(fixture)]) == 1
        err = capsys.readouterr().err
        assert "un-nonced inline <style>" in err

    def test_directory_scan_aggregates_findings(self, tmp_path: Path) -> None:
        (tmp_path / "a.html").write_text(_HTML_UNNONCED_SCRIPT, encoding="utf-8")
        (tmp_path / "b.html").write_text(_HTML_NONCED_SCRIPT, encoding="utf-8")
        (tmp_path / "nested").mkdir()
        (tmp_path / "nested" / "c.html").write_text(
            _HTML_UNNONCED_STYLE, encoding="utf-8"
        )
        linter = _load_linter()
        findings = linter.find_violations([tmp_path])
        # Two un-nonced tags across a/c; b is nonced.
        assert len(findings) == 2

    def test_missing_explicit_path_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A named path that doesn't exist is a usage error."""
        missing = tmp_path / "does-not-exist"
        linter = _load_linter()
        assert linter.main([str(missing)]) == 2
        err = capsys.readouterr().err
        assert "not a file or directory" in err

    def test_nonce_case_insensitive(self, tmp_path: Path) -> None:
        """``NONCE`` and ``Nonce`` attribute casings both pass the gate.

        HTMLParser normalises attribute names to lower case, so this
        is really a "parser behaviour" pin — a regression that swaps
        the parser would break here loudly.
        """
        fixture = tmp_path / "index.html"
        fixture.write_text(
            '<!doctype html><body><script NONCE="abc">a</script></body>',
            encoding="utf-8",
        )
        linter = _load_linter()
        assert linter.main([str(fixture)]) == 0

    def test_default_path_missing_is_not_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fresh checkout without an SPA build must not fail CI.

        The DEFAULT path (no argv) silently tolerates an absent
        directory — only an explicitly named missing path is a usage
        error. ``monkeypatch.chdir`` takes us somewhere where
        ``app/web/dist`` is guaranteed absent.
        """
        monkeypatch.chdir(tmp_path)
        linter = _load_linter()
        assert linter.main([]) == 0

    def test_findings_report_line_numbers(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``file:line`` pairs help humans find the offending inline fast."""
        fixture = tmp_path / "index.html"
        fixture.write_text(_HTML_UNNONCED_SCRIPT, encoding="utf-8")
        linter = _load_linter()
        linter.main([str(fixture)])
        err = capsys.readouterr().err
        # The unnonced ``<script>`` sits on line 5 of the fixture.
        assert f"{fixture}:5:" in err
