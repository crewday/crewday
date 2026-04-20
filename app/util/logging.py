"""Structured JSON logger + secret-redaction filter.

Configures the root logger so every record ships as one JSON line on
stdout with uniform fields (``time``, ``level``, ``logger``, ``msg``),
plus a redaction filter that masks credentials and PII-adjacent
patterns in both the formatted message and any ``extra=`` payload.

See ``docs/specs/15-security-privacy.md`` §"Logging and redaction" and
``docs/specs/01-architecture.md`` §"Runtime invariants" #7
("Secrets are never logged.").

The spec names ``structlog`` as the implementation; we satisfy the
invariants with the stdlib ``logging`` module instead, to keep the
dependency graph minimal. The redaction contract is identical: every
log record passes the :class:`RedactionFilter` before the handler
formats it.

Redaction depth is capped at **2 nested levels** for dicts / lists /
tuples in ``extra=`` payloads. Deeper structures are rendered via
``repr`` and then scanned as a single string — the common log path
stays predictable under load and the filter cannot become a DoS seam
via pathological nesting.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Final, TextIO

from pydantic import SecretStr

__all__ = [
    "JsonFormatter",
    "RedactionFilter",
    "reset_correlation_id",
    "set_correlation_id",
    "setup_logging",
]


_REDACTED: Final[str] = "***REDACTED***"

# Key-based redaction: any mapping key whose name matches is replaced.
# Matches substrings so ``x_authorization`` and ``api-key-header`` hit.
_SENSITIVE_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"authorization|auth|token|api[_-]?key|password|cookie|"
    r"session[_-]?id|secret|passkey|credential",
    re.IGNORECASE,
)

# Value-based patterns. Ordering matters: Bearer first (longest, most
# specific prefix), then JWT (three base64url segments), then generic
# long-hex / base64url blobs.
_BEARER_RE: Final[re.Pattern[str]] = re.compile(
    r"Bearer\s+[A-Za-z0-9._\-]+",
)
_JWT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b",
)
_HEX_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[A-Fa-f0-9]{32,}\b",
)
_BASE64URL_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[A-Za-z0-9_\-]{32,}\b",
)

# Record attrs injected by ``logging`` itself; ``extra=`` keys land on
# the LogRecord as plain attributes, so we filter these out when
# serialising. See CPython ``logging/__init__.py`` ``LogRecord``.
_RESERVED_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

# Keys reserved by our JSON envelope. If the caller's ``extra=`` tries
# to use one, it is namespaced with a leading underscore on output.
_RESERVED_OUTPUT_KEYS: Final[frozenset[str]] = frozenset(
    {"time", "level", "logger", "msg", "correlation_id", "exc_info"}
)


_CORRELATION_ID: ContextVar[str | None] = ContextVar(
    "crewday_audit_correlation_id", default=None
)


def set_correlation_id(correlation_id: str) -> Token[str | None]:
    """Bind ``correlation_id`` to the current context (per-request).

    Returns a token to pass to :func:`reset_correlation_id` when the
    scope ends. Implementation note: we use :class:`ContextVar` rather
    than ``threading.local`` so the binding survives ``asyncio`` task
    switches — one request, one correlation id, even across awaits.
    """
    return _CORRELATION_ID.set(correlation_id)


def reset_correlation_id(token: Token[str | None]) -> None:
    """Unbind the correlation id previously set via
    :func:`set_correlation_id`."""
    _CORRELATION_ID.reset(token)


def _redact_value(value: object, depth: int = 0) -> object:
    """Recursively redact ``value`` down to ``depth <= 2``.

    Strings go through the value-regex pass. Mappings get per-key
    matching plus recursive redaction of children. Lists / tuples
    recurse element-wise. ``SecretStr`` instances and other unknown
    types collapse to ``_REDACTED`` / their ``repr`` respectively.
    """
    if isinstance(value, SecretStr):
        return _REDACTED
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, bool) or value is None:
        # bool is a subclass of int; short-circuit before the int path.
        return value
    if isinstance(value, int | float):
        return value
    if depth >= 2:
        # Past the recursion cap, still scan the rendered form so a
        # deep-nested Bearer token does not slip through.
        return _redact_string(repr(value))
    if isinstance(value, dict):
        return _redact_mapping(value, depth + 1)
    if isinstance(value, list | tuple):
        redacted = [_redact_value(item, depth + 1) for item in value]
        return tuple(redacted) if isinstance(value, tuple) else redacted
    # Unknown type: render safely via repr and scan as a string.
    return _redact_string(repr(value))


def _redact_mapping(mapping: dict[object, object], depth: int) -> dict[object, object]:
    redacted: dict[object, object] = {}
    for key, raw in mapping.items():
        if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
            redacted[key] = _REDACTED
            continue
        redacted[key] = _redact_value(raw, depth)
    return redacted


def _redact_string(value: str) -> str:
    # Order matters: Bearer is a prefixed shape, so strip it first; JWT
    # next (its dot-separated form would be swallowed by the generic
    # base64url pass below); hex before base64url, since 32+ hex chars
    # are valid base64url too and the hex rule is a useful signal on
    # its own in reports.
    redacted = _BEARER_RE.sub(_REDACTED, value)
    redacted = _JWT_RE.sub(_REDACTED, redacted)
    redacted = _HEX_RE.sub(_REDACTED, redacted)
    redacted = _BASE64URL_RE.sub(_REDACTED, redacted)
    return redacted


class RedactionFilter(logging.Filter):
    """Root-handler filter that masks secrets in every record.

    Scans ``record.msg`` (after arg substitution) and any ``extra=``
    attributes for the patterns in this module. Nested structures are
    walked to depth 2; deeper values are ``repr``-rendered and scanned
    as a string — see the module docstring.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 1) Redact the formatted message. We materialise ``getMessage``
        #    once and overwrite ``msg`` / clear ``args`` so downstream
        #    formatters (including ours) see the redacted text even if
        #    they re-format. ``getMessage`` runs ``msg % args`` which can
        #    raise ``TypeError`` (wrong arg count for ``%s``-style),
        #    ``ValueError`` (bad conversion spec), ``KeyError`` (missing
        #    mapping key for ``%(name)s``), or ``IndexError`` (too few
        #    positional args). None of these should kill logging — fall
        #    back to the raw template so the record still survives.
        try:
            formatted = record.getMessage()
        except (TypeError, ValueError, KeyError, IndexError):
            formatted = str(record.msg)
        record.msg = _redact_string(formatted)
        record.args = None

        # 2) Redact extra attributes in place. ``extra=`` kwargs land
        #    directly on the record as plain attributes; anything not
        #    in ``_RESERVED_RECORD_ATTRS`` is a caller-supplied key.
        for attr in list(record.__dict__):
            if attr in _RESERVED_RECORD_ATTRS or attr.startswith("_"):
                continue
            value = record.__dict__[attr]
            if isinstance(attr, str) and _SENSITIVE_KEY_RE.search(attr):
                record.__dict__[attr] = _REDACTED
                continue
            record.__dict__[attr] = _redact_value(value)

        return True


class JsonFormatter(logging.Formatter):
    """Emit each record as a single-line JSON object.

    Fields: ``time`` (ISO-8601 UTC with ``+00:00``), ``level``,
    ``logger``, ``msg``, plus any caller-supplied ``extra=`` keys
    flattened onto the top level. A bound correlation id (see
    :func:`set_correlation_id`) appears as ``correlation_id``; an
    exception, when present, is rendered as a single string under
    ``exc_info``.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Prefer the already-redacted ``record.msg`` (set by the
        # filter). If no filter ran — e.g. tests constructing a
        # formatter directly — ``getMessage`` still returns the raw
        # string, which the downstream consumer can redact. Match the
        # filter's exception tolerance so a malformed record never
        # crashes the formatter either.
        try:
            message = record.getMessage()
        except (TypeError, ValueError, KeyError, IndexError):
            message = str(record.msg)

        payload: dict[str, object] = {
            "time": _format_timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": message,
        }

        correlation_id = _CORRELATION_ID.get()
        if correlation_id is not None:
            payload["correlation_id"] = correlation_id

        for attr, value in record.__dict__.items():
            if attr in _RESERVED_RECORD_ATTRS or attr.startswith("_"):
                continue
            out_key = f"_{attr}" if attr in _RESERVED_OUTPUT_KEYS else attr
            payload[out_key] = _json_safe(value)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=_json_safe_default)


def _format_timestamp(created: float) -> str:
    """ISO-8601 UTC with explicit offset (``+00:00``)."""
    return datetime.fromtimestamp(created, tz=UTC).isoformat()


def _json_safe(value: object) -> object:
    """Coerce ``value`` into a JSON-encodable form.

    The redactor preserves ``dict``, ``list``, ``tuple``, primitives
    and strings; everything else falls back to ``repr`` so
    ``json.dumps`` cannot raise on an unexpected type. Tuples become
    lists for JSON.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return repr(value)


def _json_safe_default(value: object) -> str:
    return repr(value)


class _CrewdayJsonHandler(logging.StreamHandler[TextIO]):
    """:class:`StreamHandler` subclass tagged for idempotent removal.

    :func:`setup_logging` only removes handlers of this exact type,
    so handlers installed by pytest capture / uvicorn / etc. survive
    a reconfigure call.
    """


def setup_logging(
    level: str = "INFO",
    *,
    stream: TextIO | None = None,
) -> None:
    """Configure the root logger with JSON output + redaction.

    Idempotent: repeated calls replace any handler installed by a
    previous invocation, keeping test runs predictable. ``stream``
    defaults to ``sys.stdout``; pass a :class:`io.StringIO` in tests.
    """
    target_stream: TextIO = stream if stream is not None else sys.stdout

    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, _CrewdayJsonHandler):
            root.removeHandler(handler)

    handler = _CrewdayJsonHandler(target_stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())

    root.addHandler(handler)
    root.setLevel(level.upper())
