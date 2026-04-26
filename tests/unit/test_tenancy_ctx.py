"""Tests for the current-request WorkspaceContext carrier.

See docs/specs/01-architecture.md §"WorkspaceContext" and
§"Tenant filter enforcement".
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from app.tenancy.context import WorkspaceContext
from app.tenancy.current import (
    _current_ctx,
    _tenant_agnostic,
    get_current,
    is_tenant_agnostic,
    reset_current,
    set_current,
    tenant_agnostic,
)


@pytest.fixture(autouse=True)
def _isolate_context_vars() -> Iterator[None]:
    """Snapshot and restore both ContextVars around each test.

    The autouse fixture keeps each test hermetic: a set_current() that
    a previous test forgot to reset_current() cannot leak into the
    next test.
    """
    ctx_token = _current_ctx.set(None)
    agn_token = _tenant_agnostic.set(False)
    try:
        yield
    finally:
        _current_ctx.reset(ctx_token)
        _tenant_agnostic.reset(agn_token)


def _make_ctx(suffix: str = "a") -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=f"01ws_{suffix}",
        workspace_slug=f"slug-{suffix}",
        actor_id=f"01us_{suffix}",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=f"01au_{suffix}",
    )


# ---------------------------------------------------------------------------
# get_current / set_current / reset_current
# ---------------------------------------------------------------------------


def test_get_current_is_none_on_fresh_task() -> None:
    assert get_current() is None


def test_set_current_then_get_current_roundtrips() -> None:
    ctx = _make_ctx()
    token = set_current(ctx)
    try:
        assert get_current() is ctx
    finally:
        reset_current(token)
    assert get_current() is None


def test_set_current_none_allowed() -> None:
    # The ContextVar is typed WorkspaceContext | None; storing None
    # explicitly is legal and must round-trip.
    token = set_current(None)
    try:
        assert get_current() is None
    finally:
        reset_current(token)


def test_reset_current_restores_previous_value() -> None:
    outer = _make_ctx("outer")
    inner = _make_ctx("inner")
    outer_token = set_current(outer)
    try:
        inner_token = set_current(inner)
        try:
            assert get_current() is inner
        finally:
            reset_current(inner_token)
        # Back to outer, not all the way to None.
        assert get_current() is outer
    finally:
        reset_current(outer_token)


# ---------------------------------------------------------------------------
# tenant_agnostic
# ---------------------------------------------------------------------------


def test_tenant_agnostic_default_is_false() -> None:
    assert is_tenant_agnostic() is False


def test_tenant_agnostic_block_sets_true_and_restores() -> None:
    assert is_tenant_agnostic() is False
    with tenant_agnostic():
        assert is_tenant_agnostic() is True
    assert is_tenant_agnostic() is False


def test_tenant_agnostic_block_restores_on_exception() -> None:
    with pytest.raises(RuntimeError, match="boom"), tenant_agnostic():
        assert is_tenant_agnostic() is True
        raise RuntimeError("boom")
    assert is_tenant_agnostic() is False


def test_tenant_agnostic_nested_restores_intermediate_state() -> None:
    # Nesting is rare but legal. The Token mechanism should restore
    # the outer value, not the module-wide default.
    assert is_tenant_agnostic() is False
    with tenant_agnostic():
        assert is_tenant_agnostic() is True
        with tenant_agnostic():
            assert is_tenant_agnostic() is True
        # Still inside the outer block — must remain True.
        assert is_tenant_agnostic() is True
    assert is_tenant_agnostic() is False


# ---------------------------------------------------------------------------
# ContextVar scoping across asyncio tasks
# ---------------------------------------------------------------------------


async def _set_and_report(
    ctx: WorkspaceContext,
    *,
    ready: asyncio.Event,
    release: asyncio.Event,
) -> WorkspaceContext | None:
    """Install ``ctx``, wait for the other task to do the same, then read.

    The fence ensures both tasks have written before either reads, so a
    broken "just use one ContextVar per thread" implementation would
    show cross-task contamination.
    """
    token = set_current(ctx)
    try:
        ready.set()
        await release.wait()
        return get_current()
    finally:
        reset_current(token)


def test_context_scopes_per_asyncio_task() -> None:
    ctx_a = _make_ctx("a")
    ctx_b = _make_ctx("b")

    async def driver() -> tuple[WorkspaceContext | None, WorkspaceContext | None]:
        a_ready = asyncio.Event()
        b_ready = asyncio.Event()
        release = asyncio.Event()

        task_a = asyncio.create_task(
            _set_and_report(ctx_a, ready=a_ready, release=release)
        )
        task_b = asyncio.create_task(
            _set_and_report(ctx_b, ready=b_ready, release=release)
        )
        await a_ready.wait()
        await b_ready.wait()
        release.set()
        seen_a, seen_b = await asyncio.gather(task_a, task_b)
        # The driver itself must remain unaffected.
        assert get_current() is None
        return seen_a, seen_b

    seen_a, seen_b = asyncio.run(driver())
    assert seen_a is ctx_a
    assert seen_b is ctx_b


# ---------------------------------------------------------------------------
# WorkspaceContext.principal_kind (cd-tvh)
# ---------------------------------------------------------------------------


class TestPrincipalKind:
    """``WorkspaceContext.principal_kind`` defaults + kwargs round-trip.

    cd-tvh introduces ``principal_kind`` so route-layer guards (e.g.
    delegated-mint refusal of token callers per §03 "Delegated tokens")
    can branch on the transport that authenticated the caller — even
    though :attr:`actor_kind` collapses session + token into ``"user"``.
    """

    def test_defaults_to_session(self) -> None:
        """Existing kwargs without ``principal_kind`` get the conservative default.

        The migration is backwards-compatible: every test fixture and
        domain helper that builds a :class:`WorkspaceContext` without
        passing ``principal_kind`` continues to behave as if it were a
        session-presented request. The default is the most-permissive
        transport, so a misconfigured caller errs on usable rather than
        silently locked out — see the field docstring.
        """
        ctx = WorkspaceContext(
            workspace_id="01HWA00000000000000000WSPA",
            workspace_slug="ws-a",
            actor_id="01HWA00000000000000000USRA",
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id="01HWA00000000000000000CRLA",
        )
        assert ctx.principal_kind == "session"

    def test_explicit_token_kind_round_trips(self) -> None:
        """``principal_kind="token"`` is preserved on construction."""
        ctx = WorkspaceContext(
            workspace_id="01HWA00000000000000000WSPB",
            workspace_slug="ws-b",
            actor_id="01HWA00000000000000000USRB",
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id="01HWA00000000000000000CRLB",
            principal_kind="token",
        )
        assert ctx.principal_kind == "token"

    def test_explicit_system_kind_round_trips(self) -> None:
        ctx = WorkspaceContext(
            workspace_id="01HWA00000000000000000WSPC",
            workspace_slug="ws-c",
            actor_id="01HWA00000000000000000USRC",
            actor_kind="system",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id="01HWA00000000000000000CRLC",
            principal_kind="system",
        )
        assert ctx.principal_kind == "system"

    def test_principal_kind_is_part_of_equality(self) -> None:
        """Two contexts that differ only on transport must NOT compare equal.

        The frozen / slots dataclass auto-generated ``__eq__`` walks
        every field; :attr:`principal_kind` is normative state because
        downstream guards (e.g. delegated-mint refusal) branch on it,
        so equality has to disagree when the transport disagrees.
        """
        common = {
            "workspace_id": "01HWA00000000000000000WSPD",
            "workspace_slug": "ws-d",
            "actor_id": "01HWA00000000000000000USRD",
            "actor_kind": "user",
            "actor_grant_role": "manager",
            "actor_was_owner_member": False,
            "audit_correlation_id": "01HWA00000000000000000CRLD",
        }
        as_session = WorkspaceContext(**common, principal_kind="session")  # type: ignore[arg-type]
        as_token = WorkspaceContext(**common, principal_kind="token")  # type: ignore[arg-type]
        assert as_session != as_token


def test_tenant_agnostic_scopes_per_asyncio_task() -> None:
    async def _flip_and_peek(
        *,
        flip: bool,
        ready: asyncio.Event,
        release: asyncio.Event,
    ) -> bool:
        if flip:
            with tenant_agnostic():
                ready.set()
                await release.wait()
                return is_tenant_agnostic()
        ready.set()
        await release.wait()
        return is_tenant_agnostic()

    async def driver() -> tuple[bool, bool]:
        flipped_ready = asyncio.Event()
        plain_ready = asyncio.Event()
        release = asyncio.Event()

        flipped_task = asyncio.create_task(
            _flip_and_peek(flip=True, ready=flipped_ready, release=release)
        )
        plain_task = asyncio.create_task(
            _flip_and_peek(flip=False, ready=plain_ready, release=release)
        )
        await flipped_ready.wait()
        await plain_ready.wait()
        release.set()
        return await asyncio.gather(flipped_task, plain_task)

    flipped, plain = asyncio.run(driver())
    assert flipped is True
    assert plain is False
