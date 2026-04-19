"""Shared pytest configuration for crewday tests.

The scaffolding task (cd-t5xe) will replace this with proper shared
fixtures and factories. Until then, this file exists to:

1. Give pytest a stable collection root, and
2. Treat an empty test suite as a successful run so CI stays green
   while we bootstrap the ``app/`` tree. Without this hook pytest
   exits with status 5 ("no tests collected"), which fails CI even
   though nothing is broken.
"""

from __future__ import annotations

import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Downgrade the "no tests collected" exit code to success (0).

    Remove this hook once the test scaffolding task (cd-t5xe) lands
    real tests.
    """
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
        session.exitstatus = pytest.ExitCode.OK
