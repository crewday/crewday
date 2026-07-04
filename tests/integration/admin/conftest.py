"""Fixtures shared by the admin integration suite.

The ``tests/integration/admin`` modules exercise host-only admin flows.
Some drive the app end-to-end and commit to the session-scoped shared
``engine``; others (workspace bootstrap) use the rollback-wrapped
``db_session`` but still *read* the shared engine with ``.scalar()``
queries that assume no stranger rows are present. Both need the shared
engine reset around each test — see
:func:`tests.integration.conftest.reset_shared_engine`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine

from tests.integration.conftest import reset_shared_engine


@pytest.fixture(autouse=True)
def _isolate_shared_engine(engine: Engine) -> Iterator[None]:
    """Reset the shared engine around every admin integration test.

    Root-cause fix for cd-pls5z: guarantees each test starts from an
    empty shared engine regardless of randomized xdist ordering, and
    leaves no committed residue behind.
    """
    reset_shared_engine(engine)
    try:
        yield
    finally:
        reset_shared_engine(engine)
