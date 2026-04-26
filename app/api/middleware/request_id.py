"""Per-request id binding for the structured-log seam.

The middleware:

1. Reads the inbound ``X-Request-Id`` header. If present **and** the
   value parses as a UUID, it is reused verbatim — a chained caller
   (browser SPA → API → background worker) keeps one id end-to-end
   so log scrapes can correlate across hops.
2. If no header is present (or the inbound value does not look like
   a UUID), mints a fresh UUID4. Untrusted strings are rejected so a
   malicious client cannot stamp an arbitrary marker into our logs.
3. Binds the resolved id to :func:`app.util.logging.set_request_id`
   so :class:`~app.util.logging.JsonFormatter` stamps every record
   the downstream handler emits.
4. Echoes the resolved id back on the response as ``X-Request-Id``
   so the caller's own logs can carry the same value (the §16
   "Observability / Logs" key contract).
5. Resets the ContextVar in ``finally`` so the binding does not leak
   into the next request served by the same worker task.

The seam is intentionally separate from the
:class:`~app.tenancy.middleware.WorkspaceContextMiddleware` (which
manages the legacy audit ``correlation_id`` header) for two reasons:

* The tenancy middleware skips on bare-host paths (``/healthz``,
  ``/readyz``, signup) where a request id is still useful for
  correlating health-probe alerts with their source.
* A future refactor that splits tenancy resolution into smaller
  middlewares should not have to also re-thread the request-id
  binding.

Both headers (``X-Request-Id`` for this middleware, the audit
correlation id from the tenancy middleware) coexist on the response;
the spec keeps them as distinct concepts so a caller that needs to
correlate at the audit-trail level (which carries through to
``audit_log.correlation_id``) can do so without conflating it with
the per-HTTP-hop request id.

See ``docs/specs/16-deployment-operations.md`` §"Observability /
Logs" and ``docs/specs/15-security-privacy.md`` §"Logging and
redaction".
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.util.logging import new_request_id, reset_request_id, set_request_id

__all__ = ["REQUEST_ID_HEADER", "RequestIdMiddleware", "new_request_id"]


# Header name follows the de-facto convention (Heroku, Caddy,
# nginx-ingress all emit it as ``X-Request-Id``). Cased canonically
# on emit; HTTP header parsing is case-insensitive on the read.
REQUEST_ID_HEADER: Final[str] = "X-Request-Id"


def _coerce_inbound(value: str | None) -> str | None:
    """Return ``value`` if it looks like a UUID, else ``None``.

    The middleware reuses inbound ids only when they parse cleanly
    so an attacker cannot stamp arbitrary content into log lines.
    UUIDs are the §16 documented shape; rejecting anything else
    closes the log-injection vector while still letting a trusted
    upstream (Caddy, an API client SDK) pin a known id end-to-end.
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        # ``UUID(...)`` accepts both hyphenated and bare hex forms;
        # we re-render through :class:`UUID` to canonicalise the
        # output so downstream log scrapes see one shape regardless
        # of which variant the upstream sent.
        return str(uuid.UUID(candidate))
    except ValueError:
        return None


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Bind a per-request id ContextVar around the downstream handler.

    Mounted at the outer end of the middleware chain (just inside
    CORS) so every other middleware — including the tenancy
    middleware's own log lines — sees the bound id. The CSP /
    security-headers middleware does not depend on it; ordering
    relative to that one is purely about who sees the id in their
    own log emits.

    See module docstring for the inbound-header trust policy and
    the ContextVar lifecycle contract.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = (
            _coerce_inbound(request.headers.get(REQUEST_ID_HEADER)) or new_request_id()
        )
        token = set_request_id(request_id)
        try:
            response = await call_next(request)
        finally:
            # Always restore — even if the downstream handler raised —
            # so the ContextVar does not leak into the next request
            # served by the same worker task. The tenancy middleware
            # uses the same idiom; matching the pattern keeps the
            # cleanup discipline consistent across the chain.
            reset_request_id(token)
        # Stamp the resolved id on the response so the caller's own
        # logs can carry the same value (the §16 "Observability /
        # Logs" key contract). Set unconditionally — a downstream
        # middleware that already wrote ``X-Request-Id`` is overwritten
        # so the visible value matches what we logged.
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
