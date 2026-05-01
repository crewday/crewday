"""Agent runtime adapters.

The production :class:`~app.domain.agent.runtime.ToolDispatcher` lives
under :mod:`app.agent.dispatcher`; importing the symbol from here is
the convenience seam for callers that want one import.
"""

from __future__ import annotations

from app.agent.dispatcher import OpenAPIToolDispatcher, make_default_dispatcher

__all__ = [
    "OpenAPIToolDispatcher",
    "make_default_dispatcher",
]
