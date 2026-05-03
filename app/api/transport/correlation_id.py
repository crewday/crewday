"""``X-Correlation-Id`` / ``X-Correlation-Id-Echo`` HTTP header round-trip.

Spec §11 "Client abstraction" pins the contract:

  ``correlation_id`` propagated from ``X-Correlation-Id`` if present,
  else generated server-side and returned via ``X-Correlation-Id-Echo``.

Spec §02 "Correlation scope" restates the same default for
``audit_log.correlation_id``:

  defaults to the HTTP request id (generated server-side if the caller
  did not pass ``X-Correlation-Id``). A caller that wants to group
  multiple HTTP requests into one logical workflow may pass the same
  ``X-Correlation-Id`` on each.

This middleware closes the loop:

1. Reads inbound ``X-Correlation-Id``. If absent — or syntactically
   unsafe to echo back into log lines — mints a fresh ULID via
   :func:`app.util.ulid.new_ulid`.
2. Stashes the resolved id on ``request.state.correlation_id`` so
   downstream middleware (notably
   :class:`app.tenancy.middleware.WorkspaceContextMiddleware`) and
   handlers can read one canonical value rather than re-parsing the
   header themselves.
3. Writes ``X-Correlation-Id-Echo`` on the outgoing response.

The middleware MUST be mounted **outer** of
:class:`~app.tenancy.middleware.WorkspaceContextMiddleware`, so that
``request.state.correlation_id`` exists by the time WCM resolves the
:class:`~app.tenancy.WorkspaceContext` (which carries the same value
through to ``audit_log.correlation_id`` and ``llm_usage.correlation_id``
via the ctx's ``audit_correlation_id`` field).

Validation policy:

* ULID-shaped values (the canonical case the server would itself mint)
  pass through verbatim.
* Other printable-ASCII values up to :data:`MAX_INBOUND_LENGTH`
  characters pass through too — observability headers are best-effort
  and a chained agent SDK may pin a domain-specific token (e.g. a
  conversation ULID prefixed with a label).
* CRLF / NUL / other control characters cause the header to be
  ignored (NOT 400 — observability is best-effort) and a fresh ULID
  is minted instead. This is the same defence-in-depth posture
  :func:`app.api.errors._sanitize_header_value` takes for echoes from
  error envelopes; minting fresh closes the log-injection vector at
  the source.

The new ``X-Correlation-Id-Echo`` header is the spec-named outbound;
the existing :class:`~app.api.middleware.request_id.RequestIdMiddleware`
keeps emitting ``X-Request-Id`` (a per-hop log-correlation handle) and
:class:`~app.tenancy.middleware.WorkspaceContextMiddleware` keeps
emitting ``X-Request-Id`` mirrored from this state. They coexist by
design.

**Known gap** — unhandled exception responses. Starlette's
``ServerErrorMiddleware`` is mounted *outside* user-registered
middleware in the request-time stack: an unhandled exception escaping
the handler is converted to a synthesised 500 by ``ServerErrorMiddleware``
before this middleware can stamp the echo. The legacy ``X-Request-Id``
plumbing has the same gap. ``HTTPException`` and explicit-status
``Response`` returns do round-trip the echo (FastAPI's inner
``ExceptionMiddleware`` converts them to a normal Response that this
middleware sees on the way out). See
``tests/unit/api/transport/test_correlation_id.py::TestEchoOnNonOkResponses``.

See ``docs/specs/11-llm-and-agents.md`` §"Client abstraction" and
``docs/specs/02-domain-model.md`` §"Correlation scope".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.util.ulid import new_ulid

__all__ = [
    "CORRELATION_ID_ECHO_HEADER",
    "CORRELATION_ID_HEADER",
    "CORRELATION_ID_STATE_ATTR",
    "MAX_INBOUND_LENGTH",
    "CorrelationIdMiddleware",
]


# Inbound header — the spec §11 / §02 name a chained caller stamps.
CORRELATION_ID_HEADER: Final[str] = "X-Correlation-Id"

# Outbound header — distinct from the inbound name so a caller that
# round-trips a request through several hops can always tell "what I
# sent" from "what the server resolved" (§11 "Client abstraction").
CORRELATION_ID_ECHO_HEADER: Final[str] = "X-Correlation-Id-Echo"

# Attribute the middleware writes on ``request.state``. Module
# constant so downstream readers (the tenancy middleware, the
# usage-recorder seam, future correlation-aware loggers) import the
# exact string instead of inlining it.
CORRELATION_ID_STATE_ATTR: Final[str] = "correlation_id"

# Maximum accepted inbound length. Generous (256 chars) so a chained
# caller can stamp a domain-specific token (a conversation id, a
# request hash, a UUID with a workflow prefix) without the middleware
# silently rejecting it. Anything longer is treated as malformed and
# replaced with a fresh ULID.
MAX_INBOUND_LENGTH: Final[int] = 256


def _coerce_inbound(value: str | None) -> str | None:
    """Return ``value`` if it is safe to echo, else ``None``.

    ``None`` is returned for the common cases — header absent, header
    empty, value too long, value containing characters that would
    break HTTP serialisation or open a log-injection vector. The
    caller treats ``None`` as "mint a fresh ULID".

    The accepted shape is: 1..:data:`MAX_INBOUND_LENGTH` printable
    ASCII characters (codepoints 0x20..0x7E). Tab, CR, LF, NUL, and
    other control bytes are rejected; non-ASCII is rejected too —
    HTTP/1.1 header values are technically opaque bytes, but
    constraining to printable ASCII keeps the wire safe across every
    proxy + log scraper without surprising anyone.
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if len(candidate) > MAX_INBOUND_LENGTH:
        return None
    # Printable-ASCII gate. ``str.isprintable`` accepts non-ASCII
    # printable codepoints too (e.g. accented characters), so we add
    # an explicit ASCII bound. The two together reject CR/LF/NUL,
    # control bytes, and bytes a downstream proxy might mangle.
    if not candidate.isascii() or not candidate.isprintable():
        return None
    return candidate


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind a correlation id around the downstream handler.

    Mounts **outer** of
    :class:`~app.tenancy.middleware.WorkspaceContextMiddleware` so the
    tenancy resolver (and anything else that runs inside) sees the id
    on ``request.state``.

    The middleware is stateless across requests — every dispatch
    recomputes the id and writes it to ``request.state``. There is no
    ContextVar to reset; downstream code that wants the id reads it
    off ``request.state`` (HTTP-bound) or off
    ``WorkspaceContext.audit_correlation_id`` (workspace-scoped
    handlers and audit writers).
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        inbound = _coerce_inbound(request.headers.get(CORRELATION_ID_HEADER))
        correlation_id = inbound if inbound is not None else new_ulid()
        # Stash on ``request.state`` so the tenancy middleware (and
        # any other middleware that runs inside this one) can read
        # the resolved value without re-parsing the header. The
        # attribute is unconditionally set — downstream code can
        # rely on it always being present when the middleware is
        # installed.
        setattr(request.state, CORRELATION_ID_STATE_ATTR, correlation_id)
        response = await call_next(request)
        # Stamp the spec-named echo header on the response. We use a
        # distinct name from the inbound so a chained caller can
        # always tell "what I sent" from "what the server resolved";
        # set unconditionally so a downstream middleware that wrote
        # the same name is overwritten with the canonical value.
        response.headers[CORRELATION_ID_ECHO_HEADER] = correlation_id
        return response
