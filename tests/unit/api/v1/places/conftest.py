"""Shared fixtures for the places HTTP router suite (cd-lzh1).

Re-exports the workspace + persona fixtures from the identity suite
(:mod:`tests.unit.api.v1.identity.conftest`) so the properties roster
tests can ride the same in-memory engine + dependency-override harness
without duplicating the seed code. Importing the fixture function and
re-binding it here is the documented pytest pattern for sharing
fixtures across sibling packages without lifting them to the project-
wide :mod:`tests.conftest` (which would slow every unrelated test
down by loading the identity tables).
"""

from __future__ import annotations

from tests.unit.api.v1.identity.conftest import (
    engine,
    factory,
    owner_ctx,
    worker_ctx,
)

__all__ = [
    "engine",
    "factory",
    "owner_ctx",
    "worker_ctx",
]
