"""Unit tests for the structured-log enrichments (cd-24tp).

The logging module's existing surface (JSON envelope, redaction
filter, correlation-id ContextVar) is exercised through the
existing `tests/unit/util/test_redact.py` and the broader factory
tests. This module focuses on the new keys cd-24tp adds:

* ``request_id`` — bound by :func:`set_request_id` / cleared by
  :func:`reset_request_id`; read by :class:`JsonFormatter`.
* ``workspace_id`` — sourced from the active
  :class:`~app.tenancy.WorkspaceContext` via
  :func:`app.tenancy.current.set_current` / ``reset_current``.

Both keys must:

1. Appear at the top level of the JSON envelope when bound.
2. Be absent when unbound (no empty-string sentinel — operators
   should be able to grep ``"workspace_id":`` to find scoped
   records).
3. Survive across an ``asyncio`` task switch (ContextVar contract).
4. Not collide with caller-supplied ``extra=`` keys — a stray
   ``extra={"request_id": ...}`` is namespaced rather than
   silently overwriting the formatter's value.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import uuid
from collections.abc import Iterator

import pytest

from app.tenancy import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.util.logging import (
    JsonFormatter,
    RedactionFilter,
    get_request_id,
    new_request_id,
    reset_request_id,
    set_request_id,
    setup_logging,
)


def _emit(logger: logging.Logger, msg: str, **extra: object) -> dict[str, object]:
    """Drive ``logger`` once, return the JSON-decoded record.

    Uses an in-memory ``StringIO`` handler with the production
    formatter + filter so every assertion runs against the actual
    on-the-wire shape — no reading through the `caplog` fixture's
    pre-formatted shape.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    logger.addHandler(handler)
    saved_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        logger.info(msg, extra=extra or None)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved_level)
    payload = stream.getvalue().strip().splitlines()[-1]
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise AssertionError(f"expected JSON object, got {decoded!r}")
    return decoded


@pytest.fixture
def logger() -> Iterator[logging.Logger]:
    log = logging.getLogger(f"test.logging.{uuid.uuid4().hex}")
    log.handlers.clear()
    yield log
    log.handlers.clear()


# ---------------------------------------------------------------------------
# request_id
# ---------------------------------------------------------------------------


class TestRequestId:
    """``set_request_id`` binds; ``JsonFormatter`` stamps every record."""

    def test_unbound_request_id_is_absent_from_payload(
        self, logger: logging.Logger
    ) -> None:
        record = _emit(logger, "no rid bound")
        assert "request_id" not in record

    def test_bound_request_id_appears_at_top_level(
        self, logger: logging.Logger
    ) -> None:
        rid = "9b21c6d4-7f03-4d8c-a26a-3f0b5b88d1aa"
        token = set_request_id(rid)
        try:
            record = _emit(logger, "with rid")
        finally:
            reset_request_id(token)
        assert record["request_id"] == rid

    def test_request_id_clears_on_reset(self, logger: logging.Logger) -> None:
        token = set_request_id("9b21c6d4-7f03-4d8c-a26a-3f0b5b88d1aa")
        reset_request_id(token)
        record = _emit(logger, "after reset")
        assert "request_id" not in record

    def test_get_request_id_reads_the_bound_value(self) -> None:
        assert get_request_id() is None
        token = set_request_id("9b21c6d4-7f03-4d8c-a26a-3f0b5b88d1aa")
        try:
            assert get_request_id() == "9b21c6d4-7f03-4d8c-a26a-3f0b5b88d1aa"
        finally:
            reset_request_id(token)
        assert get_request_id() is None

    def test_request_id_survives_asyncio_task_switch(
        self, logger: logging.Logger
    ) -> None:
        """ContextVars propagate across awaits — the binding must be
        visible from inside a sub-coroutine awaited under the same
        outer scope (``asyncio.run`` copies the current context)."""

        async def emit() -> dict[str, object]:
            await asyncio.sleep(0)
            return _emit(logger, "from coroutine")

        async def driver() -> dict[str, object]:
            token = set_request_id("9b21c6d4-7f03-4d8c-a26a-3f0b5b88d1aa")
            try:
                return await emit()
            finally:
                reset_request_id(token)

        record = asyncio.run(driver())
        assert record["request_id"] == "9b21c6d4-7f03-4d8c-a26a-3f0b5b88d1aa"

    def test_caller_extra_request_id_is_namespaced_not_overwritten(
        self, logger: logging.Logger
    ) -> None:
        """An ``extra={"request_id": ...}`` collides with the
        formatter's own ``request_id`` key. The reserved-keys rule
        prefixes the colliding key with ``_`` so the bound id stays
        authoritative.
        """
        token = set_request_id("9b21c6d4-7f03-4d8c-a26a-3f0b5b88d1aa")
        try:
            record = _emit(logger, "collision", request_id="caller-supplied")
        finally:
            reset_request_id(token)
        assert record["request_id"] == "9b21c6d4-7f03-4d8c-a26a-3f0b5b88d1aa"
        assert record["_request_id"] == "caller-supplied"

    def test_new_request_id_returns_uuid_string(self) -> None:
        rid = new_request_id()
        # Round-trip through :class:`uuid.UUID` to assert shape.
        parsed = uuid.UUID(rid)
        assert str(parsed) == rid


# ---------------------------------------------------------------------------
# workspace_id
# ---------------------------------------------------------------------------


def _ctx(workspace_id: str) -> WorkspaceContext:
    """Build a minimal :class:`WorkspaceContext` for the formatter test.

    The actual values of the actor / role fields are irrelevant —
    only ``workspace_id`` is sourced by the formatter.

    All ULID-shaped placeholders use realistic high-entropy bodies
    (mixed crockford-base32 chars). Avoid contrived all-zero ULIDs
    here — the central PII redactor's PAN regex (Luhn over 13-19
    contiguous digits) matches a long zero tail, which would not
    affect this test directly (the formatter does not redact
    workspace_id) but would mislead a reader copying the fixture
    elsewhere.
    """
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="test-slug",
        actor_id="01KQ3HF5QR6SX6PDC4XPGGDFCA",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id="01KQ3HF5QR6SX6PDC4XPGGDFCC",
    )


class TestWorkspaceId:
    """The formatter reads :func:`app.tenancy.current.get_current`."""

    def test_unbound_workspace_id_is_absent_from_payload(
        self, logger: logging.Logger
    ) -> None:
        record = _emit(logger, "no ws bound")
        assert "workspace_id" not in record

    def test_bound_workspace_id_appears_at_top_level(
        self, logger: logging.Logger
    ) -> None:
        token = set_current(_ctx("01KQ3HF5QR6SX6PDC4XPGGDFC1"))
        try:
            record = _emit(logger, "with ws")
        finally:
            reset_current(token)
        assert record["workspace_id"] == "01KQ3HF5QR6SX6PDC4XPGGDFC1"

    def test_workspace_id_clears_when_ctx_resets(self, logger: logging.Logger) -> None:
        token = set_current(_ctx("01KQ3HF5QR6SX6PDC4XPGGDFC1"))
        reset_current(token)
        record = _emit(logger, "after reset")
        assert "workspace_id" not in record

    def test_caller_extra_workspace_id_is_namespaced_not_overwritten(
        self, logger: logging.Logger
    ) -> None:
        token = set_current(_ctx("01KQ3HF5QR6SX6PDC4XPGGDFC1"))
        try:
            record = _emit(
                logger,
                "collision",
                workspace_id="caller-supplied",
            )
        finally:
            reset_current(token)
        assert record["workspace_id"] == "01KQ3HF5QR6SX6PDC4XPGGDFC1"
        assert record["_workspace_id"] == "caller-supplied"


# ---------------------------------------------------------------------------
# Cross-key combination
# ---------------------------------------------------------------------------


class TestCombinedKeys:
    """When all enrichments are active the JSON envelope holds them all."""

    def test_all_keys_present_at_top_level(self, logger: logging.Logger) -> None:
        rid_token = set_request_id("9b21c6d4-7f03-4d8c-a26a-3f0b5b88d1aa")
        ctx_token = set_current(_ctx("01KQ3HF5QR6SX6PDC4XPGGDFC1"))
        try:
            record = _emit(logger, "full envelope")
        finally:
            reset_current(ctx_token)
            reset_request_id(rid_token)
        assert record["request_id"] == "9b21c6d4-7f03-4d8c-a26a-3f0b5b88d1aa"
        assert record["workspace_id"] == "01KQ3HF5QR6SX6PDC4XPGGDFC1"
        # The pre-existing envelope keys still appear unchanged.
        assert record["level"] == "INFO"
        assert record["msg"] == "full envelope"
        assert record["logger"] == logger.name


# ---------------------------------------------------------------------------
# setup_logging integration
# ---------------------------------------------------------------------------


class TestSetupLoggingHonoursEnrichment:
    """The end-to-end :func:`setup_logging` path keeps the new keys."""

    def test_setup_logging_emits_request_id(self) -> None:
        stream = io.StringIO()
        setup_logging(level="INFO", stream=stream)
        token = set_request_id("9b21c6d4-7f03-4d8c-a26a-3f0b5b88d1aa")
        try:
            logging.getLogger("test.setup_logging").info("hello")
        finally:
            reset_request_id(token)
        line = stream.getvalue().strip().splitlines()[-1]
        decoded = json.loads(line)
        assert decoded["request_id"] == "9b21c6d4-7f03-4d8c-a26a-3f0b5b88d1aa"
