"""HTTP middleware shared across the API surface.

Each middleware is a thin, single-purpose wrapper around
:class:`starlette.middleware.base.BaseHTTPMiddleware`. Concrete modules:

* :mod:`app.api.middleware.security_headers` — strict CSP with a
  per-request nonce, HSTS (opt-in), Permissions-Policy with a
  worker-route carve-out, and the rest of the §15 header set.

See ``docs/specs/15-security-privacy.md`` §"HTTP security headers".
"""

from __future__ import annotations

from app.api.middleware.security_headers import (
    SecurityHeadersMiddleware,
    build_csp_header,
    build_permissions_policy,
    generate_csp_nonce,
)

__all__ = [
    "SecurityHeadersMiddleware",
    "build_csp_header",
    "build_permissions_policy",
    "generate_csp_nonce",
]
