"""Boundary tests for the import-linter gate (cd-ev0).

These tests exercise the import-boundary contracts declared in
``pyproject.toml`` under ``[tool.importlinter]``. The spec is
``docs/specs/01-architecture.md`` §"Module boundaries" (rules 1-6)
and ``docs/specs/17-testing-quality.md`` §"Import boundaries".

Two scenarios:

* **Positive** — ``uv run lint-imports`` on the clean tree exits 0.
  Guards against a future change accidentally introducing a
  cross-boundary import or breaking the config.
* **Negative** — writing a deliberately bad file at
  ``app/domain/identity/_bad_cross_boundary_test.py`` that imports
  ``app.adapters.db.session`` causes ``lint-imports`` to exit
  non-zero. Guards against a silent misconfiguration of the gate
  (e.g. a typo in ``source_modules`` would still report "all
  kept").

The bad file lives inside ``app/`` only for the duration of the
negative test; the negative test itself unlinks under the lock,
and an autouse post-yield fixture unlinks again as a safety net so
a crash mid-test cannot leak the file into other test runs or,
worse, into git. Both unlinks hold the same cross-process flock as
the ``lint-imports`` invocations themselves — see the
``_exclusive_lock`` comment block for the why.
"""

from __future__ import annotations

import fcntl
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

# Repository root = three levels above this file
# (tests/boundary/test_import_gate.py -> tests/boundary -> tests -> repo).
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

# Path of the deliberately-bad fixture file written by the negative
# test. Kept at module scope so the autouse cleanup fixture can
# reference the same path the test writes to.
BAD_FILE: Path = (
    REPO_ROOT / "app" / "domain" / "identity" / "_bad_cross_boundary_test.py"
)

BAD_FILE_CONTENTS: str = (
    '"""Deliberately bad import used by tests/boundary/test_import_gate.py.\n\n'
    "This file must never be committed. If you are reading it outside a\n"
    "test run, delete it.\n"
    '"""\n\n'
    "from app.adapters.db.session import make_engine  # noqa: F401\n"
)


# Cross-process file lock that serialises both halves of the boundary
# gate. ``test_clean_tree_passes`` and ``test_cross_boundary_import_is_
# rejected`` both shell out to ``lint-imports`` against the *whole*
# ``app/`` tree, and the negative test mutates
# ``app/domain/identity/_bad_cross_boundary_test.py`` for the
# duration of its subprocess. Under ``pytest-xdist`` (default since
# the perf flip in cd-…) the two tests may execute on different
# workers concurrently, in which case worker A's clean-tree
# assertion can observe worker B's bad fixture file and flake.
#
# The lock is released only after the bad fixture file has been
# removed again, so the clean-tree test under one worker cannot
# overlap with the period in which the bad file exists on disk under
# another. Splitting / renaming the two tests was rejected because
# the cd-7qxh acceptance criterion names ``test_clean_tree_passes``
# by symbol; this lock keeps the public layout intact.
_LINT_IMPORTS_LOCK_PATH: Path = REPO_ROOT / ".pytest-import-gate.lock"


@contextmanager
def _exclusive_lock() -> Iterator[None]:
    """Yield while holding an exclusive ``fcntl.flock`` on the sentinel file.

    The boundary gate runs on Linux CI; ``fcntl`` is unavailable on
    Windows but the module isn't expected to load there. A Windows
    skip can land later if that ever changes.
    """
    with _LINT_IMPORTS_LOCK_PATH.open("a+") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


@pytest.fixture(autouse=True)
def _ensure_bad_file_absent() -> Iterator[None]:
    """Guarantee the bad fixture file is gone after every test.

    Without this, a crash mid-test (or a killed pytest process)
    would leave ``_bad_cross_boundary_test.py`` sitting inside
    ``app/domain/identity/`` — where it would both fail subsequent
    ``lint-imports`` runs and risk being committed by a careless
    ``git add``. The post-yield unlink runs **inside** the
    cross-process lock so it cannot delete a bad file the *other*
    worker's :func:`test_cross_boundary_import_is_rejected` is
    actively relying on for its in-flight ``lint-imports`` call.

    No pre-yield unlink: the only writer of ``BAD_FILE`` is
    :func:`test_cross_boundary_import_is_rejected`, and that test
    serialises its own write → lint → unlink under the same lock.
    A pre-yield unlink would have to take the lock too, doubling the
    serialisation surface and risking deadlock against the negative
    test's own outer lock.
    """
    try:
        yield
    finally:
        with _exclusive_lock():
            BAD_FILE.unlink(missing_ok=True)


def _run_lint_imports() -> subprocess.CompletedProcess[str]:
    """Invoke ``uv run lint-imports`` from the repo root and capture output."""
    return subprocess.run(
        ["uv", "run", "lint-imports"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_clean_tree_passes() -> None:
    """``lint-imports`` on the untouched repo must exit 0.

    Acts as the baseline that proves the three boundary contracts
    are satisfied today. A regression here means something in
    ``app/`` started importing across a forbidden seam.
    """
    with _exclusive_lock():
        result = _run_lint_imports()
    assert result.returncode == 0, (
        f"lint-imports unexpectedly failed on the clean tree.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_cross_boundary_import_is_rejected() -> None:
    """A deliberately bad cross-boundary import must fail ``lint-imports``.

    Writes a minimal file at
    ``app/domain/identity/_bad_cross_boundary_test.py`` that imports
    from ``app.adapters.db.session`` — a violation of the "Domain
    forbids adapters (except ports)" contract. Exit code must be
    non-zero and the bad import must appear in stdout.

    The write → lint-imports → unlink sequence runs **inside** the
    cross-process lock so the bad file is never visible on disk
    while another worker's :func:`test_clean_tree_passes` is
    executing its own ``lint-imports``.
    """
    with _exclusive_lock():
        BAD_FILE.write_text(BAD_FILE_CONTENTS, encoding="utf-8")
        try:
            result = _run_lint_imports()
        finally:
            BAD_FILE.unlink(missing_ok=True)
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
