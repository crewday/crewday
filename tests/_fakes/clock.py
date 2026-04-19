"""Clock fake — re-exports :class:`FrozenClock` from :mod:`app.util.clock`.

``FrozenClock`` is already a production-quality test double (aware
UTC only, explicit ``advance`` / ``set``). We re-export it here so
every context's ``conftest.py`` imports from a single seam —
``tests._fakes`` — and the fake registry stays discoverable by
anyone reading ``tests/_fakes/``.

See ``docs/specs/17-testing-quality.md`` §"Unit".
"""

from __future__ import annotations

from app.util.clock import FrozenClock

__all__ = ["FrozenClock"]
