"""Shared ``application/problem+json`` response declarations for v1 routers.

Mirrors :mod:`app.api.assets._shared` (cd-pa9p) for the bounded-context
routers that don't yet ship their own ``_shared`` helper. Every 4xx
response on these routers flows through :mod:`app.api.errors` and is
serialised as RFC 7807 ``application/problem+json`` per spec
§12 "Errors", but FastAPI's default schema declares ``application/json``
+ :class:`fastapi.exceptions.HTTPValidationError` for 422 (and nothing
at all for the other 4xx codes), which trips the schemathesis contract
gate's "Undocumented Content-Type" check.

Pinning :data:`IDENTITY_PROBLEM_RESPONSES` on each identity-tagged
``APIRouter`` (via the ``responses=`` kwarg) makes the OpenAPI schema
match the runtime envelope so the gate accepts the actual response
shape.

The dict is a plain ``int → response-spec`` map; FastAPI merges these
router-level entries into every operation's ``responses`` table at
schema-build time. Per-operation overrides (e.g. an explicit 404
description) still take precedence.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "IDENTITY_PROBLEM_RESPONSES",
    "PROBLEM_JSON_CONTENT",
]


# RFC 7807 problem+json envelope. Mirrors the asset router's
# ``_PROBLEM_JSON_SCHEMA`` (app/api/assets/_shared.py) so the two
# surfaces document the same shape.
_PROBLEM_JSON_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "detail": {"type": "string"},
        "instance": {"type": "string"},
        "errors": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["type", "title", "status", "instance"],
    "additionalProperties": True,
}


PROBLEM_JSON_CONTENT: Final[dict[str, Any]] = {
    "application/problem+json": {"schema": _PROBLEM_JSON_SCHEMA},
}


def _problem(description: str) -> dict[str, Any]:
    return {"description": description, "content": PROBLEM_JSON_CONTENT}


# Default 4xx response set for identity-tagged routers. Covers every
# code an identity handler can raise (400 / 401 / 403 / 404 / 409 / 410
# / 413 / 415 / 422). 429 is left to the global
# ``_declare_rate_limit_responses`` post-processor in
# :mod:`app.api.factory` which already documents it with the canonical
# ``Retry-After`` header.
IDENTITY_PROBLEM_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    400: _problem("Bad request"),
    401: _problem("Unauthenticated"),
    403: _problem("Permission denied or CSRF mismatch"),
    404: _problem("Resource not found"),
    409: _problem("Conflict"),
    410: _problem("Gone"),
    413: _problem("Payload too large"),
    415: _problem("Unsupported media type"),
    422: _problem("Validation error"),
}
