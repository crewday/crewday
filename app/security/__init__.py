"""Process-boot security checks.

Currently exposes the public-interface bind guard (§15 "Binding
policy", §16 "Bind model"). The module is deliberately handler-free
so :mod:`app.main` and :mod:`app.cli` can both call it before Uvicorn
opens a socket.
"""

from __future__ import annotations

from app.security.bind_guard import BindGuardError, assert_bind_allowed

__all__ = ["BindGuardError", "assert_bind_allowed"]
