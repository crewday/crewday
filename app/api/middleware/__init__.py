"""HTTP middleware shared across the API surface.

Each middleware is a thin, single-purpose wrapper around
:class:`starlette.middleware.base.BaseHTTPMiddleware`. Concrete modules:

* :mod:`app.api.middleware.security_headers` — strict CSP with a
  per-request nonce, HSTS (opt-in), Permissions-Policy with a
  worker-route carve-out, and the rest of the §15 header set.
* :mod:`app.api.middleware.idempotency` — persisted replay cache for
  ``POST`` + ``Idempotency-Key`` retries (spec §12 "Idempotency").
* :mod:`app.api.middleware.request_id` — per-request id ContextVar
  binding for the structured-log seam (spec §16 "Observability /
  Logs").
* :mod:`app.api.middleware.metrics` — Prometheus HTTP histograms
  + counter (spec §16 "Observability / Metrics").
* :mod:`app.api.middleware.rate_limit` — per-token / per-IP API
  token buckets (spec §12 "Rate limiting").

See ``docs/specs/15-security-privacy.md`` §"HTTP security headers",
``docs/specs/12-rest-api.md`` §"Idempotency",
``docs/specs/16-deployment-operations.md`` §"Observability".
"""

from __future__ import annotations

from app.api.middleware.idempotency import (
    IdempotencyMiddleware,
    prune_expired_idempotency_keys,
)
from app.api.middleware.metrics import HttpMetricsMiddleware
from app.api.middleware.rate_limit import RateLimitMiddleware
from app.api.middleware.request_id import (
    REQUEST_ID_HEADER,
    RequestIdMiddleware,
    new_request_id,
)
from app.api.middleware.security_headers import (
    SecurityHeadersMiddleware,
    build_csp_header,
    build_permissions_policy,
    generate_csp_nonce,
)

__all__ = [
    "REQUEST_ID_HEADER",
    "HttpMetricsMiddleware",
    "IdempotencyMiddleware",
    "RateLimitMiddleware",
    "RequestIdMiddleware",
    "SecurityHeadersMiddleware",
    "build_csp_header",
    "build_permissions_policy",
    "generate_csp_nonce",
    "new_request_id",
    "prune_expired_idempotency_keys",
]
