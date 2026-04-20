"""Full §15 HTTP-security-headers middleware.

The middleware:

1. Generates a fresh 128-bit random nonce per request and stashes it on
   ``request.state.csp_nonce`` so downstream handlers (the SPA bootstrap
   renderer, later) can stamp the same value onto any ``<script>`` or
   ``<style>`` tag they emit inline.
2. After the downstream handler returns, writes the strict
   ``Content-Security-Policy`` header (plus the rest of the §15 set) onto
   the response. Rejections produced by middleware outer to this one
   still get stamped, because the CSP is set on the response object
   returned by ``call_next`` — whichever handler produced it.

See ``docs/specs/15-security-privacy.md`` §"HTTP security headers" and
§"Shared-origin XSS containment" for intent, §24 "CSP on demo" for the
``frame-ancestors`` carve-out.

## Threading the nonce through

The nonce lives on ``request.state`` rather than as an attribute of the
middleware instance because Starlette reuses a single middleware
instance across concurrent requests — instance state would leak nonces
between them. ``request.state`` is per-request by contract
(``BaseHTTPMiddleware`` constructs a new :class:`starlette.requests.Request`
for each dispatch, which in turn constructs a fresh ``state`` namespace).

## What we do NOT do here

* We do **not** log the nonce. It's short-lived, but if a nonce were
  ever recorded alongside request metadata it would give an attacker a
  cheap confirmation oracle for CSP bypass attempts. The structured-log
  redactor (``app.util.logging``) already redacts high-entropy base64
  runs, but we keep the nonce off every log line at the source to avoid
  relying on that safety net.
* We do **not** attempt to parse existing CSP headers downstream
  handlers may have set — we overwrite. A handler that needs its own
  policy is a future need (report-only for a specific page, for
  example); until then, single-source-of-truth wins.
"""

from __future__ import annotations

import base64
import re
import secrets
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.config import Settings

__all__ = [
    "SecurityHeadersMiddleware",
    "build_csp_header",
    "build_permissions_policy",
    "generate_csp_nonce",
]


# Paths that match this pattern get ``camera=(self)`` on
# ``Permissions-Policy`` — worker task-evidence capture needs it.
# Anywhere else, the camera is denied. We match at any depth so a
# future ``/w/<slug>/worker/task/<id>/photo`` route Just Works; the
# per-spec phrasing is "worker pages" so the anchor is the ``/worker/``
# segment under a workspace slug.
_WORKER_CAMERA_PATH_RE: Final[re.Pattern[str]] = re.compile(r"^/w/[^/]+/worker/")


# 128 bits of randomness: the CSP nonce lives for one response, and the
# attacker needs to guess it to inject an inline script. 128 bits
# cleanly exceeds the 128-bit guidance MDN / W3C cite for CSP nonces
# and is the CSP spec's recommended minimum.
_NONCE_BYTES: Final[int] = 16


def generate_csp_nonce() -> str:
    """Return a fresh CSP nonce.

    Encoded as unpadded standard base64 (not URL-safe): the CSP header
    spec accepts any base64-charset alphabet, and the ``/`` and ``+``
    characters never appear in the directive list that follows so there
    is no ambiguity. ``rstrip('=')`` strips padding, which the CSP
    parser does not require. The result is 22 ASCII characters for the
    16-byte input, short enough to stay readable in responses but long
    enough to resist brute-force guessing.

    Each call consumes fresh OS entropy via :func:`secrets.token_bytes`
    — callers rely on that contract: every request must get a unique
    value or the CSP defence collapses into a known-value replay.
    """
    return (
        base64.b64encode(secrets.token_bytes(_NONCE_BYTES)).decode("ascii").rstrip("=")
    )


def build_csp_header(
    nonce: str,
    *,
    demo_mode: bool,
    demo_frame_ancestors: str | None,
) -> str:
    """Compose the ``Content-Security-Policy`` directive string.

    Kept as a pure function so tests can pin the exact serialisation
    without a live middleware. The directive ordering matches the spec
    prose: ``default-src`` first, then resource-specific sources, then
    the framing / navigation / base-URI set, then ``object-src``.

    Demo carve-out (§24 "CSP on demo"): when ``demo_mode`` is on and
    ``demo_frame_ancestors`` is set, the ``frame-ancestors 'none'``
    directive is replaced with the supplied allowlist value so the demo
    iframe can be embedded on the marketing site. Everywhere else,
    ``frame-ancestors 'none'`` stays hard — any same-origin framing
    attempt is refused at the browser layer.
    """
    frame_ancestors = "'none'"
    if demo_mode and demo_frame_ancestors:
        frame_ancestors = demo_frame_ancestors

    directives = [
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}'",
        f"style-src 'self' 'nonce-{nonce}'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        f"frame-ancestors {frame_ancestors}",
        "form-action 'self'",
        "base-uri 'self'",
        "object-src 'none'",
    ]
    return "; ".join(directives)


def build_permissions_policy(path: str) -> str:
    """Compose the ``Permissions-Policy`` header for a given request path.

    Worker routes (``/w/<slug>/worker/...``) are the only surface that
    asks the browser for camera access (task-evidence capture). Every
    other route denies the camera outright. ``geolocation`` is denied
    everywhere — the v0 clock-in geofence is gone in v1 (§15
    "Data-minimisation note"). ``microphone`` and ``payment`` are
    denied anywhere; we currently have no use case for either, and the
    §15 prose is a hard "deny everywhere except the task-evidence
    surface".

    The ``(self)`` form allows the top-level document's origin and
    nothing else — the browser will refuse to fanout the capability
    into cross-origin iframes even on the worker page.
    """
    camera = "(self)" if _WORKER_CAMERA_PATH_RE.match(path) else "()"
    return f"camera={camera}, geolocation=(), microphone=(), payment=()"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Stamp the full §15 header set onto every response.

    Constructor takes the settings explicitly rather than reaching into
    ``request.app.state`` at dispatch time — the factory wires us once
    per process, and every runtime knob (``demo_mode``,
    ``demo_frame_ancestors``, ``hsts_enabled``) is a process-wide
    constant. Pulling values from a request-time lookup would force a
    branch on ``hasattr(request.app.state, 'settings')`` that the
    factory's wiring already makes redundant.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings,
    ) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        nonce = generate_csp_nonce()
        # Downstream handlers read this to stamp their bootstrap
        # <script nonce="..."> tag. Set BEFORE ``call_next`` so the
        # handler sees it.
        request.state.csp_nonce = nonce

        response = await call_next(request)

        response.headers["Content-Security-Policy"] = build_csp_header(
            nonce,
            demo_mode=self._settings.demo_mode,
            demo_frame_ancestors=self._settings.demo_frame_ancestors,
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = build_permissions_policy(
            request.url.path
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Defence-in-depth alongside CSP ``frame-ancestors``: older
        # browsers that ignore CSP still honour the legacy header.
        #
        # Demo carve-out (§24 "CSP on demo"): when an allowlist of
        # ``frame-ancestors`` origins is in force, ``X-Frame-Options``
        # MUST NOT be set — the legacy header has no way to express
        # a multi-origin allowlist and every browser that honours both
        # would fall back to the most-restrictive DENY, defeating the
        # point of the demo carve-out. The spec is explicit: "``X-Frame-
        # Options`` is not set on demo responses — the CSP
        # ``frame-ancestors`` directive supersedes".
        if not (self._settings.demo_mode and self._settings.demo_frame_ancestors):
            response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"

        if self._settings.hsts_enabled:
            # 2-year max-age + preload: once TLS is verified, an
            # operator flips ``hsts_enabled = True`` and the header
            # starts protecting every subsequent visit. ``preload`` +
            # ``includeSubDomains`` are required for submission to
            # browser preload lists (hstspreload.org). The spec pins
            # this exact value so operators have a single predictable
            # HSTS posture — deployments that want a shorter ramp-up
            # must opt in via the flag once they are confident.
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )

        return response
