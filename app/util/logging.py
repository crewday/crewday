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

**Single source of truth for redaction rules:** every pattern lives
in :mod:`app.util.redact`. This module's filter is a thin wrapper
that invokes :func:`app.util.redact.redact` with
``scope="log"``. Maintainers adding a new PII regex or key-name
rule touch one place; the filter picks it up automatically.

Because the logging filter is a hot-path component (every record,
every process), it retains its existing public API
(:class:`RedactionFilter`, :class:`JsonFormatter`,
:func:`setup_logging`, :func:`set_correlation_id`,
:func:`reset_correlation_id`). Downstream code imports these names
directly.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Final, TextIO

from app.util.redact import (
    ConsentSet,
    redact,
    scrub_string,
)

__all__ = [
    "JsonFormatter",
    "RedactionFilter",
    "get_request_id",
    "new_request_id",
    "reset_correlation_id",
    "reset_request_id",
    "set_correlation_id",
    "set_request_id",
    "setup_logging",
]

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
    {
        "time",
        "level",
        "logger",
        "msg",
        "correlation_id",
        "request_id",
        "workspace_id",
        "exc_info",
    }
)


_CORRELATION_ID: ContextVar[str | None] = ContextVar(
    "crewday_audit_correlation_id", default=None
)

# Per-request id, distinct from ``correlation_id`` (which is
# audit-scoped and travels with the audit-trail chain). The request
# id is a per-HTTP-request UUID; the §16 spec calls it ``request_id``
# in the JSON-log key list. The middleware in
# :mod:`app.api.middleware.request_id` accepts an inbound
# ``X-Request-Id`` header so a client correlating across services
# can pin the same id end-to-end. Without an inbound header, the
# middleware mints a fresh UUID4. Worker / scheduler ticks bind a
# request id of their own (the wrap-job seam in
# :mod:`app.worker.scheduler`) so a structured-log scrape can
# correlate every line a single tick emits.
_REQUEST_ID: ContextVar[str | None] = ContextVar("crewday_request_id", default=None)


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


def set_request_id(request_id: str) -> Token[str | None]:
    """Bind ``request_id`` to the current context (per-request).

    Symmetric to :func:`set_correlation_id`. The HTTP middleware
    pairs every :func:`set_request_id` with a
    :func:`reset_request_id` in a ``finally`` block; subprocess /
    worker callers use the same idiom around their tick body.
    """
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    """Unbind the request id previously set via :func:`set_request_id`."""
    _REQUEST_ID.reset(token)


def get_request_id() -> str | None:
    """Return the request id bound to the current context, or ``None``.

    Used by :class:`JsonFormatter` to stamp every record with the
    active request id; also exported so call sites that need to
    propagate the id across an out-of-process boundary (subprocess
    spawn, message queue) can read it without reaching into the
    private :data:`_REQUEST_ID` ContextVar.
    """
    return _REQUEST_ID.get()


def new_request_id() -> str:
    """Return a fresh UUID4 string suitable for ``X-Request-Id``.

    Centralised here so the HTTP middleware, the worker's tick
    wrapper, and any future subprocess seam all mint the same shape
    without duplicating the :mod:`uuid` import.
    """
    return str(uuid.uuid4())


def _get_workspace_id() -> str | None:
    """Return the workspace id from the current :class:`WorkspaceContext`.

    Imported lazily so the logging module stays importable from
    boot-time code paths that run before tenancy wiring is in place
    (the bind guard, settings validation, ``app.main``'s pre-
    factory imports). The :class:`WorkspaceContext` lookup itself
    is a single ContextVar read — cheap enough to hit on every log
    record.
    """
    # Lazy import: ``app.tenancy.current`` pulls in the dataclass
    # that depends on no DB seam, but we still defer to avoid a
    # circular dependency once the tenancy package grows
    # logging-aware diagnostics of its own.
    from app.tenancy.current import get_current

    ctx = get_current()
    return ctx.workspace_id if ctx is not None else None


class RedactionFilter(logging.Filter):
    """Root-handler filter that masks secrets in every record.

    Delegates the heavy lifting to :func:`app.util.redact.redact`
    (scope ``"log"``), which owns the canonical regex + key-name
    rule set. The filter is responsible only for:

    * Materialising :meth:`LogRecord.getMessage` (with the same
      exception tolerance the old hand-rolled filter had).
    * Running the message string through
      :func:`~app.util.redact.scrub_string` so in-message Bearer
      tokens / JWTs / hex blobs still get caught.
    * Walking ``extra=`` attributes in place through the central
      redactor so the JSON formatter sees an already-scrubbed
      record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 1) Redact the formatted message. ``getMessage`` runs
        #    ``msg % args`` which can raise ``TypeError`` (wrong arg
        #    count for ``%s``-style), ``ValueError`` (bad conversion
        #    spec), ``KeyError`` (missing mapping key for ``%(name)s``),
        #    or ``IndexError`` (too few positional args). None of
        #    these should kill logging — fall back to the raw template
        #    so the record still survives.
        try:
            formatted = record.getMessage()
        except (TypeError, ValueError, KeyError, IndexError):
            formatted = str(record.msg)
        record.msg = scrub_string(formatted)
        record.args = None

        # 2) Redact extra attributes in place. ``extra=`` kwargs land
        #    directly on the record as plain attributes; anything not
        #    in ``_RESERVED_RECORD_ATTRS`` is a caller-supplied key.
        #    We pass each value through the central redactor with
        #    ``scope="log"`` so the same rule set applies.
        for attr in list(record.__dict__):
            if attr in _RESERVED_RECORD_ATTRS or attr.startswith("_"):
                continue
            # Mimic the mapping-level sensitive-key rule on the flat
            # ``extra=`` namespace: ``{"token": ...}`` must redact
            # the value regardless of its shape.
            value = record.__dict__[attr]
            record.__dict__[attr] = _redact_record_attr(attr, value)

        return True


def _redact_record_attr(attr: str, value: object) -> object:
    """Redact a single ``extra=`` attribute.

    Wraps the central redactor so the flat record namespace benefits
    from the same key-name rules as nested dicts: a caller passing
    ``extra={"token": ...}`` still sees the value replaced. We
    synthesise a single-key mapping and extract the redacted value.
    """
    synthesised = {attr: value}
    redacted = redact(synthesised, scope="log", consents=ConsentSet.none())
    if not isinstance(redacted, dict):
        # redact() preserves dict type, but narrow for mypy.
        return redacted
    return redacted[attr]


class JsonFormatter(logging.Formatter):
    """Emit each record as a single-line JSON object.

    Fields: ``time`` (ISO-8601 UTC with ``+00:00``), ``level``,
    ``logger``, ``msg``, plus any caller-supplied ``extra=`` keys
    flattened onto the top level. A bound correlation id (see
    :func:`set_correlation_id`) appears as ``correlation_id``; the
    per-request id bound by the HTTP middleware (see
    :func:`set_request_id`) appears as ``request_id``; the active
    :class:`WorkspaceContext`'s workspace id (when one is in scope)
    appears as ``workspace_id``. An exception, when present, is
    rendered as a single string under ``exc_info``.

    The §16 spec ("Observability / Logs") names ``request_id`` and
    ``workspace_id`` as standard keys; emitting them at the
    formatter rather than at every call site keeps the contract
    single-sourced — a new caller cannot forget to stamp them.
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

        request_id = _REQUEST_ID.get()
        if request_id is not None:
            payload["request_id"] = request_id

        workspace_id = _get_workspace_id()
        if workspace_id is not None:
            payload["workspace_id"] = workspace_id

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
