"""Shared pytest configuration for crewday tests.

The scaffolding (cd-t5xe) now provides per-context unit
``conftest.py`` files, an integration harness under
``tests/integration/conftest.py``, and shared fakes under
``tests/_fakes/``. This top-level module stays thin on purpose —
cross-cutting fixtures would encourage leakage between contexts.

Only fixtures that every context needs belong here. Right now that
is a single helper, :func:`allow_propagated_log_capture`, which
compensates for alembic's ``fileConfig`` side effect on named
loggers (see the fixture's docstring for the mechanism).

See ``docs/specs/17-testing-quality.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator

import pytest


@pytest.fixture
def allow_propagated_log_capture() -> Iterator[Callable[..., None]]:
    """Enable ``caplog`` capture for named loggers across a polluted session.

    The integration fixture's ``alembic upgrade head`` path runs
    :func:`logging.config.fileConfig` with the ``alembic.ini`` default
    ``disable_existing_loggers=True``, which flips ``propagate=False``
    and ``disabled=True`` on every logger not listed in the config.
    Pytest's ``caplog`` fixture attaches its handler to the root
    logger, so records emitted on non-propagating child loggers
    never reach the capture — the test then asserts an empty
    :attr:`~_pytest.logging.LogCaptureFixture.records` list even
    though the behaviour under test worked correctly.

    Call the yielded enabler once per logger name that the test
    expects to capture; the fixture records the prior
    ``propagate`` / ``disabled`` values and restores them during
    teardown so no state leaks into the next test. Variadic —
    enable several loggers in one call when a test captures from
    more than one namespace.

    Usage::

        def test_something(allow_propagated_log_capture, caplog):
            allow_propagated_log_capture("app.api.factory")
            with caplog.at_level(logging.WARNING, logger="app.api.factory"):
                ...

    Or as an autouse class fixture::

        @pytest.fixture(autouse=True)
        def _allow_capture(self, allow_propagated_log_capture):
            allow_propagated_log_capture("app.authz.enforce")

    See also: cd-0dyv (root cause), cd-szxw (follow-up 4th occurrence).
    """
    saved: dict[str, tuple[bool, bool]] = {}

    def enable(*names: str) -> None:
        for name in names:
            log = logging.getLogger(name)
            # Record the ORIGINAL state the first time we touch a
            # given logger so repeat ``enable`` calls inside the
            # same test don't overwrite the pre-test snapshot.
            if name not in saved:
                saved[name] = (log.propagate, log.disabled)
            log.propagate = True
            log.disabled = False

    try:
        yield enable
    finally:
        for name, (propagate, disabled) in saved.items():
            log = logging.getLogger(name)
            log.propagate = propagate
            log.disabled = disabled
