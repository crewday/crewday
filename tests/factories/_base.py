"""Factory-boy base pieces shared across every context's factories.

Re-exports :class:`~tests.factories.identity.WorkspaceContextFactory`
so callers can reach it via either path without caring about where
the factory physically lives.

See ``docs/specs/17-testing-quality.md`` §"Unit".
"""

from __future__ import annotations

from tests.factories.identity import WorkspaceContextFactory

__all__ = ["WorkspaceContextFactory"]
