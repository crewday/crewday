"""Shared pytest configuration for crewday tests.

The scaffolding (cd-t5xe) now provides per-context unit
``conftest.py`` files, an integration harness under
``tests/integration/conftest.py``, and shared fakes under
``tests/_fakes/``. This top-level module is intentionally thin —
cross-cutting fixtures would encourage leakage between contexts.

See ``docs/specs/17-testing-quality.md``.
"""

from __future__ import annotations
