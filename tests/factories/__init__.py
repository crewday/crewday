"""Factory-boy factories, one module per bounded context.

Real factories land alongside their context's domain + DB models
(tracked as future cd-* tasks). For now most contexts ship a
placeholder; :mod:`tests.factories.identity` hosts a real
:class:`~app.tenancy.WorkspaceContext` factory because that primitive
already exists.

See ``docs/specs/17-testing-quality.md`` §"Unit" and §"Test data".
"""

from __future__ import annotations

from tests.factories.identity import WorkspaceContextFactory

__all__ = ["WorkspaceContextFactory"]
