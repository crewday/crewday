"""Tests for the workspace-scoped table registry.

See docs/specs/01-architecture.md §"Tenant filter enforcement".
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from app.tenancy import registry
from app.tenancy.registry import (
    _reset_for_tests,
    is_scoped,
    register,
    scoped_tables,
)


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    """Wipe the module-level registry between tests.

    The registry is process-global by design (migrations register at
    import time). Tests must not leak state into each other.
    """
    _reset_for_tests()
    try:
        yield
    finally:
        _reset_for_tests()


def test_is_scoped_false_before_register() -> None:
    assert is_scoped("task") is False


def test_register_marks_table_scoped() -> None:
    register("task")
    assert is_scoped("task") is True


def test_register_is_idempotent() -> None:
    register("task")
    register("task")
    register("task")
    assert is_scoped("task") is True
    assert len(scoped_tables()) == 1


def test_is_scoped_unknown_returns_false() -> None:
    register("task")
    assert is_scoped("anything_not_registered") is False


def test_scoped_tables_returns_frozenset() -> None:
    register("task")
    register("shift")
    tables = scoped_tables()
    assert isinstance(tables, frozenset)
    assert tables == frozenset({"task", "shift"})


def test_scoped_tables_snapshot_is_independent() -> None:
    register("task")
    snapshot = scoped_tables()
    # Mutating the registry after the snapshot must not change the
    # snapshot — it's an immutable frozenset, but also a *copy* so
    # the caller can reason about it stably.
    register("shift")
    assert snapshot == frozenset({"task"})
    assert scoped_tables() == frozenset({"task", "shift"})


def test_concurrent_registration_is_safe() -> None:
    # Smoke test: 8 worker threads each register a distinct table.
    # The threading.Lock around the set write prevents torn updates.
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            barrier.wait()
            register(f"t{i}")
        except BaseException as exc:
            with errors_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i,), daemon=True) for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive(), "worker thread hung"

    assert errors == []
    assert scoped_tables() == frozenset({f"t{i}" for i in range(8)})


def test_module_level_state_is_reset_between_tests() -> None:
    # Twin test: proves the autouse fixture clears the set. If the
    # previous test's entries leaked, this assertion would fail.
    assert scoped_tables() == frozenset()
    register("one")
    assert registry.scoped_tables() == frozenset({"one"})
