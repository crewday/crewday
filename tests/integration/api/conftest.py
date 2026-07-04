"""Fixtures shared by the end-to-end API integration suite.

Every module under ``tests/integration/api`` boots the app (or a slice of
it) and drives it through the production middleware stack, which means it
*commits* seed + mutation rows to the session-scoped shared ``engine`` so
the app's own UoW sessions can read them. See
:func:`tests.integration.conftest.reset_shared_engine` for why those
commits leak across tests and why each test needs a clean slate.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine

from tests.integration.conftest import reset_shared_engine


@pytest.fixture(autouse=True)
def _isolate_shared_engine(engine: Engine) -> Iterator[None]:
    """Reset the shared engine around every committing API test.

    Runs at setup (so the test never observes another test's committed
    rows) and again at teardown (so this test's commits do not leak
    forward). This is the root-cause fix for the order-dependent flake
    tracked in cd-pls5z: without it the randomized xdist ordering lets
    committed workspaces/users/audits accumulate on the shared engine.
    """
    reset_shared_engine(engine)
    try:
        yield
    finally:
        reset_shared_engine(engine)
