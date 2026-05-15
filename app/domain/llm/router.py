"""Capability → provider_model resolver (cd-k0qf).

Every LLM caller asks for a **capability** (``chat.manager``,
``expenses.autofill``, ``tasks.nl_intake``, …), never for a specific
model id. This module answers the question "which models should I
try, in what order, for this capability?" by walking deployment-level
:class:`~app.adapters.db.llm.models.LlmAssignment` rows
(priority-ascending, enabled-only) and falling through
:class:`~app.adapters.db.llm.models.LlmCapabilityInheritance` edges
when the capability itself has no enabled assignments.

Public surface:

* :class:`ModelPick` — a single rung of the resolved chain. Carries
  everything a downstream client needs to dispatch the call
  (``provider_model_id``, ``api_model_id``, ``max_tokens``,
  ``temperature``, ``extra_api_params``, ``required_capabilities``,
  ``assignment_id``). The cd-4btd registry trio
  (:class:`~app.adapters.db.llm.models.LlmProvider` /
  :class:`~app.adapters.db.llm.models.LlmModel` /
  :class:`~app.adapters.db.llm.models.LlmProviderModel`) lets the
  resolver carry **two distinct strings** here:
  ``provider_model_id`` is the
  :attr:`LlmProviderModel.id` ULID (the registry row's identity);
  ``api_model_id`` is :attr:`LlmProviderModel.api_model_id` —
  whatever the provider expects on the wire (e.g.
  ``anthropic/claude-3-5-sonnet`` on OpenRouter,
  ``claude-3-5-sonnet-20241022`` on a native adapter).
* :func:`resolve_model` — the full priority-ordered chain for a
  capability. Callers walking retryable errors iterate the list.
* :func:`resolve_primary` — head of the chain; raises
  :class:`CapabilityUnassignedError` when the chain is empty
  (after inheritance has been walked). The API layer maps the
  exception to ``503 capability_unassigned`` with a ``CRITICAL``
  audit row per §11 "Failure modes".

Implementation notes:

* **Pure read path.** No audit on resolve; observability is
  recorded on the eventual ``llm_call`` row (cd-wjpl).
* **No upstream I/O.** The resolver never touches an LLM provider
  — it reads ORM rows and decides which model to try first.
* **Cycle-safe inheritance walk.** Even though the write-path (API
  / admin UI) rejects cycles at save time with ``422
  capability_inheritance_cycle`` (§11 "Capability inheritance"), a
  dirty-import path could land a cycle in the DB and the resolver
  must not spin. We track visited children and abort after a small
  hop budget; a detected cycle is treated as "no parent" — the
  caller sees :class:`CapabilityUnassignedError` rather than a
  hang.
* **30 s in-process cache, event-invalidated.** A deployment-wide
  dict keyed by capability avoids a DB round
  trip on every chat turn. The admin / API layer that mutates
  assignments publishes :class:`~app.events.types.LlmAssignmentChanged`
  on the production bus; a module-level subscriber drops every
  cache entries on receipt, so operator
  edits land on the next call without waiting for the TTL.
  Invalidation is deployment-wide (not capability-scoped) because
  the event payload does not carry enough information to target a
  single capability's chain — an edit to an inheritance edge can
  silently change the chain of a capability two hops downstream.

* **Cross-process invalidation on Postgres.** The cache still lives
  in process memory, but Postgres deployments run
  :mod:`app.domain.llm.invalidation_bridge` at lifespan startup.
  Each worker subscribes to the local
  :class:`LlmAssignmentChanged` event, sends it through
  ``pg_notify`` on the ``llm_assignment`` channel, and
  republishes sibling notifications onto that worker's local
  :class:`~app.events.bus.EventBus`. The router subscriber above is
  therefore the only invalidation path in every process. SQLite
  remains the single-process / development path and uses the
  existing in-process bus behavior plus the 30 s TTL safety net.

* **Bus subscription at import time.** The production bus is
  wired up at the bottom of this module rather than via a lazy
  startup hook (compare :mod:`app.api.transport.sse`'s
  ``_ensure_bus_binding``). This module has exactly one handler
  to register, :class:`~app.events.bus.EventBus` is constructed at
  the import of :mod:`app.events.bus` so the subscribe call can
  never hit an uninitialised bus, and :func:`_subscribe_to_bus` is
  idempotent — so there's nothing the lazy pattern buys us that
  the simpler import-time subscribe doesn't already give. Tests
  that construct a fresh :class:`EventBus` wire it up explicitly
  via :func:`_subscribe_to_bus`.

See ``docs/specs/11-llm-and-agents.md`` §"Model assignment",
§"Capability inheritance", §"Client abstraction",
``docs/specs/02-domain-model.md`` §"LLM".
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import (
    LlmAssignment,
    LlmCapabilityInheritance,
    LlmModel,
    LlmProvider,
    LlmProviderModel,
)
from app.adapters.llm.ports import LlmThinkingLevel, LlmThinkingStrategy
from app.config import get_settings
from app.demo.guardrails import demo_free_model_pick, llm_capability_allowed_in_demo
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import LlmAssignmentChanged
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock

__all__ = [
    "CACHE_TTL_SECONDS",
    "DEFAULT_LLM_CAPABILITY",
    "DEPLOYMENT_DEFAULT_CACHE_WORKSPACE_ID",
    "CapabilityUnassignedError",
    "ModelPick",
    "invalidate_cache",
    "resolve_model",
    "resolve_primary",
]


# Cache lifetime for a deployment-level capability chain. 30 s is the
# §11-pinned value: long enough to cover bursty chat traffic (a
# worker process can complete dozens of turns inside one window
# without re-reading the DB) and short enough that an admin edit
# that *doesn't* ride the SSE invalidation path — a direct DB poke,
# a long-running process whose bus subscription predates the edit —
# lands within a user-tolerable delay. Callers that need the
# current chain inside the window use :func:`invalidate_cache`
# explicitly.
CACHE_TTL_SECONDS: int = 30
DEPLOYMENT_DEFAULT_CACHE_WORKSPACE_ID: str = "__deployment_llm_defaults__"
DEFAULT_LLM_CAPABILITY: str = "default"

# Inheritance-walk cycle guard. v1 seeds one edge (``chat.admin →
# chat.manager``); deeper chains remain rare by spec intent. The
# write-path rejects cycles; this bound is a safety net against a
# corrupt DB state, not a legitimate chain length. 16 is far beyond
# any plausible inheritance tree and well short of a runaway
# hot-loop.
_MAX_INHERITANCE_HOPS: int = 16
_DEFAULT_CHAT_CAPABILITIES: frozenset[str] = frozenset(
    {
        "chat.manager",
        "chat.employee",
        "chat.admin",
        "chat.compact",
        "chat.detect_language",
        "chat.translate",
        "feedback.moderate",
        "feedback.cluster",
    }
)
_DEFAULT_VISION_CAPABILITIES: frozenset[str] = frozenset({"expenses.autofill"})
_DEPLOYMENT_INHERITANCE: Mapping[str, str] = MappingProxyType(
    {"chat.admin": "chat.manager"}
)
_DEFAULT_REQUIRED_CAPABILITIES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        DEFAULT_LLM_CAPABILITY: ("chat", "function_calling"),
        "tasks.nl_intake": ("chat", "json_mode"),
        "tasks.assist": ("chat",),
        "digest.manager": ("chat",),
        "digest.employee": ("chat",),
        "anomaly.detect": ("chat", "json_mode"),
        "instructions.draft": ("chat",),
        "issue.triage": ("chat", "json_mode"),
        "stay.summarize": ("chat",),
        "voice.transcribe": ("audio_input",),
        "chat.manager": ("chat", "function_calling"),
        "chat.employee": ("chat", "function_calling"),
        "chat.admin": ("chat", "function_calling"),
        "chat.compact": ("chat",),
        "chat.detect_language": ("chat", "json_mode"),
        "chat.translate": ("chat",),
        "documents.ocr": ("vision",),
        "expenses.autofill": ("vision", "json_mode"),
        "feedback.moderate": ("chat", "json_mode"),
        "feedback.embed": ("embeddings",),
        "feedback.cluster": ("chat", "json_mode"),
    }
)


def _thinking_level(value: str | None) -> LlmThinkingLevel:
    if value in {"disabled", "low", "medium", "high"}:
        return cast(LlmThinkingLevel, value)
    return "disabled"


def _thinking_strategy(value: str | None) -> LlmThinkingStrategy:
    if value in {
        "none",
        "gemma_system_token",
        "glm_extra_body",
        "openrouter_extra_body",
    }:
        return cast(LlmThinkingStrategy, value)
    return "none"


@dataclass(frozen=True, slots=True)
class ModelPick:
    """One rung of a resolved fallback chain.

    ``provider_model_id`` is the
    :class:`~app.adapters.db.llm.models.LlmProviderModel.id` ULID —
    the registry row the assignment points at (cd-4btd FK).
    ``api_model_id`` is :attr:`LlmProviderModel.api_model_id` —
    what the provider expects on the wire (OpenRouter prefixes with
    a vendor; a native SDK adapter doesn't). Adapters dispatch on
    ``api_model_id``; observability + assignment edits hold
    ``provider_model_id``.

    Every field is a value; the dataclass is frozen + slotted so a
    rung can be stashed in a cache bucket and handed back to
    multiple callers without aliasing risk.
    """

    # The ``llm_provider_model.id`` row this rung resolved to.
    # Promoted from soft reference to a real FK by cd-4btd.
    provider_model_id: str
    # What the adapter sends on the wire — the provider's
    # ``api_model_id`` for this row. Distinct from
    # ``provider_model_id`` whenever the canonical model name and
    # the wire form diverge (the common case on OpenRouter).
    api_model_id: str
    # Per-call tuning; ``None`` = inherit the provider-model /
    # model default (§11 "Model assignment", "Provider / model /
    # provider-model registry").
    max_tokens: int | None
    temperature: float | None
    # Merged-last provider-layer params (``top_p``, tool hints, …).
    # Frozen inside the dataclass; callers MUST NOT mutate.
    extra_api_params: Mapping[str, Any] = field(default_factory=dict)
    thinking_level: LlmThinkingLevel = "disabled"
    thinking_strategy: LlmThinkingStrategy = "none"
    # Capability tags copied from the §11 catalogue on save
    # (``vision``, ``json_mode``, …). Adapter cross-checks the
    # target model before dispatch.
    required_capabilities: tuple[str, ...] = ()
    # The assignment row this rung was resolved from. Denormalised
    # onto the ``llm_call`` row later for chain-level observability
    # (§11 "Failure modes" ``X-LLM-Fallback-Attempts``).
    assignment_id: str = ""


class CapabilityUnassignedError(Exception):
    """No enabled assignment found for a capability, even after inheritance.

    Raised when:

    * the capability has no enabled deployment-level
      :class:`~app.adapters.db.llm.models.LlmAssignment` rows,
    * the `default` chain is absent or incompatible, and
    * no :class:`~app.adapters.db.llm.models.LlmCapabilityInheritance`
      edge leads to a capability that does, and
    * the inheritance walk either terminates at a capability with
      no parent or trips the cycle guard.

    The API layer maps this to ``503 capability_unassigned`` with a
    ``CRITICAL`` audit row (§11 "Failure modes"). Domain callers
    typically degrade gracefully — a digest worker that loses its
    capability skips the enrichment; a chat surface falls back to a
    plain acknowledgement.
    """

    def __init__(self, capability: str, workspace_id: str) -> None:
        super().__init__(
            f"Capability {capability!r} has no enabled assignment (after "
            f"inheritance walk) in workspace {workspace_id!r}."
        )
        self.capability = capability
        self.workspace_id = workspace_id


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CacheEntry:
    """One deployment-level ``capability → chain`` bucket with its TTL.

    Stored eagerly even for the empty-chain outcome so repeated
    calls against an unassigned capability don't re-walk the
    inheritance edges every time. An empty ``chain`` tuple
    therefore means "confirmed empty, re-raise
    :class:`CapabilityUnassignedError` without hitting the DB".
    """

    chain: tuple[ModelPick, ...]
    expires_at: datetime


# Module-level cache. Keyed by ``capability``. The
# lock protects mutation against the event handler (which may fire
# on a different thread once SSE fan-out lands); read paths take
# the lock too for consistent dict snapshots, but they never hold
# it across DB I/O — the pattern is "grab the bucket, release, use".
_CACHE: dict[str, _CacheEntry] = {}
_CACHE_LOCK = threading.Lock()

# Tracks which buses have been wired up to our invalidation
# handler. Tests that allocate a fresh :class:`EventBus` can call
# :func:`_subscribe_to_bus` against it; the production bus is
# subscribed at import time (bottom of module).
_SUBSCRIBED_BUSES: set[int] = set()
_SUBSCRIBED_BUSES_LOCK = threading.Lock()


def invalidate_cache(workspace_id: str | None = None) -> None:
    """Drop cache entries.

    Assignment and inheritance are deployment-level, so any concrete
    workspace id is treated as a deployment-wide invalidation too.

    Thread-safe: the lock covers both the scan and the pop so a
    concurrent write from another thread cannot leave the dict in
    a half-cleared state.
    """
    with _CACHE_LOCK:
        if workspace_id is None:
            _CACHE.clear()
            return
        _CACHE.clear()


def _on_llm_assignment_changed(event: LlmAssignmentChanged) -> None:
    """Subscribe hook: drop the deployment-level assignment cache.

    Whole-workspace invalidation (not per-capability) because the
    event payload does not name the affected capability: an edit to
    a :class:`LlmCapabilityInheritance` edge can silently change the
    chain of a capability two hops downstream, so narrowing the
    invalidation scope without a richer payload would miss cases.
    """
    invalidate_cache()


def _subscribe_to_bus(event_bus: EventBus) -> None:
    """Wire :func:`_on_llm_assignment_changed` onto ``event_bus`` once.

    Idempotent — re-subscribing the same bus during a test re-run
    would double-fire the handler, which would be harmless on a
    cache (the second drop is a no-op) but noisy in traces. Using
    ``id(event_bus)`` as the dedup key is exact under CPython and
    stable for the lifetime of the bus instance.
    """
    bus_id = id(event_bus)
    with _SUBSCRIBED_BUSES_LOCK:
        if bus_id in _SUBSCRIBED_BUSES:
            return
        _SUBSCRIBED_BUSES.add(bus_id)
    event_bus.subscribe(LlmAssignmentChanged)(_on_llm_assignment_changed)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def _load_enabled_chain(
    session: Session,
    *,
    capability: str,
    required_capabilities: tuple[str, ...] | None,
) -> tuple[list[ModelPick], bool]:
    """Read enabled assignments for ``capability`` in priority order.

    Returns an empty list when the capability has no enabled rows;
    the caller decides whether to walk inheritance from there.
    ``llm_assignment`` is deployment-level; workspace context remains
    part of usage, consent, budget, and audit paths outside model
    selection.

    cd-4btd: the JOIN through ``llm_provider_model`` lets the
    resolver surface :attr:`LlmProviderModel.api_model_id` (the
    provider's wire form) on
    :class:`ModelPick.api_model_id` while keeping the registry id
    on :class:`ModelPick.provider_model_id`. We use a plain INNER
    join because :attr:`LlmAssignment.model_id` is now a NOT NULL
    FK — every assignment must point at a registry row, so a LEFT
    join would only mask a data integrity bug rather than recover
    from one.
    """
    stmt = (
        select(LlmAssignment, LlmProviderModel, LlmModel)
        .join(
            LlmProviderModel,
            LlmAssignment.model_id == LlmProviderModel.id,
        )
        .join(LlmModel, LlmProviderModel.model_id == LlmModel.id)
        .join(LlmProvider, LlmProviderModel.provider_id == LlmProvider.id)
        .where(
            LlmAssignment.workspace_id.is_(None),
            LlmAssignment.capability == capability,
            LlmAssignment.enabled.is_(True),
            LlmProviderModel.is_enabled.is_(True),
            LlmProvider.is_enabled.is_(True),
            LlmModel.is_active.is_(True),
        )
        .order_by(LlmAssignment.priority.asc(), LlmAssignment.id.asc())
    )
    rows = session.execute(stmt).all()
    picks = []
    for assignment, provider_model, model in rows:
        required = (
            required_capabilities
            if required_capabilities is not None
            else tuple(assignment.required_capabilities or ())
        )
        if set(required).issubset(set(model.capabilities or [])):
            picks.append(_to_pick(assignment, provider_model, model))
    return picks, bool(rows)


def _to_pick(
    row: LlmAssignment, provider_model: LlmProviderModel, model: LlmModel
) -> ModelPick:
    """Map an (assignment, provider_model) pair to a frozen :class:`ModelPick`.

    JSON columns round-trip as mutable ``dict`` / ``list`` — wrap
    the params in a :class:`~types.MappingProxyType` view over a
    defensive copy, and coerce the tags to a ``tuple``, so a caller
    that mutates the mapping or the list does not retroactively
    corrupt the cache bucket every subscriber shares. The copy is
    cheap at cache-miss time (hundreds of params are unheard of) and
    removes a whole class of aliasing bug from the downstream
    adapter.

    cd-4btd surfaces :attr:`LlmProviderModel.api_model_id` directly
    on the pick. Per-call tuning (``max_tokens``, ``temperature``,
    ``extra_api_params``) still comes from the assignment — the
    operator's deployment assignment beats the deployment default.
    Promoting provider_model overrides to the pick is a
    follow-up once the spec pins the merge order; the
    :attr:`LlmProviderModel.max_tokens_override` /
    ``temperature_override`` / ``supports_*`` flags are the obvious
    candidates but every one has a "did the operator mean to
    override?" question that the v1 surface answers via the
    /admin/llm graph editor, not the resolver.
    """
    extra_copy: dict[str, Any] = (
        dict(row.extra_api_params) if row.extra_api_params else {}
    )
    extra: Mapping[str, Any] = MappingProxyType(extra_copy)
    required = tuple(row.required_capabilities or ())
    thinking_level = _thinking_level(
        row.thinking_level_override
        if row.thinking_level_override is not None
        else model.thinking_level
    )
    thinking_strategy = _thinking_strategy(
        provider_model.thinking_strategy_override or model.thinking_strategy
    )
    return ModelPick(
        provider_model_id=provider_model.id,
        api_model_id=provider_model.api_model_id,
        max_tokens=row.max_tokens,
        temperature=row.temperature,
        extra_api_params=extra,
        thinking_level=thinking_level,
        thinking_strategy=thinking_strategy,
        required_capabilities=required,
        assignment_id=row.id,
    )


def _lookup_parent_capability(
    session: Session,
    *,
    capability: str,
) -> str | None:
    """Return the parent capability via ``llm_capability_inheritance``.

    ``None`` means this capability has no inheritance edge; the
    caller raises :class:`CapabilityUnassignedError`.

    Uniqueness of ``capability`` on the inheritance table means at most one row
    matches — no tie-break rule needed.
    """
    stmt = select(LlmCapabilityInheritance.inherits_from).where(
        LlmCapabilityInheritance.workspace_id.is_(None),
        LlmCapabilityInheritance.capability == capability,
    )
    return session.execute(stmt).scalar_one_or_none()


def _resolve_chain(
    session: Session,
    *,
    workspace_id: str,
    capability: str,
) -> list[ModelPick]:
    """Walk the capability + inheritance tree until a chain is found.

    Returns the first non-empty priority-ordered chain encountered;
    an empty list if the walk terminates at a capability with no
    enabled assignments and no parent (or a cycle is detected).
    The caller turns an empty list into either a cache miss-sentinel
    or :class:`CapabilityUnassignedError` as appropriate.
    """
    # code-health: ignore[ccn] Capability inheritance fallback is an ordered walk.
    visited: set[str] = set()
    current = capability
    requested_required = _DEFAULT_REQUIRED_CAPABILITIES.get(capability, ("chat",))
    has_default_parent = capability in _DEFAULT_REQUIRED_CAPABILITIES
    hops = 0
    while True:
        if current in visited or hops >= _MAX_INHERITANCE_HOPS:
            # Cycle or runaway chain: treat as "no parent". The
            # write-path is supposed to reject cycles with
            # ``422 capability_inheritance_cycle``, so reaching
            # this branch means either a dirty-migration path or a
            # pathological inheritance tree; either way we fail
            # closed to :class:`CapabilityUnassignedError` rather
            # than hang.
            return []
        visited.add(current)
        hops += 1

        chain, has_enabled_rows = _load_enabled_chain(
            session,
            capability=current,
            required_capabilities=None if current == capability else requested_required,
        )
        if chain:
            return chain
        if has_enabled_rows:
            return []

        parent = _lookup_parent_capability(session, capability=current)
        if parent is None:
            parent = _DEPLOYMENT_INHERITANCE.get(current)
        if parent is None and current != DEFAULT_LLM_CAPABILITY and has_default_parent:
            parent = DEFAULT_LLM_CAPABILITY
        if parent is None:
            return []
        current = parent


def _cached_or_resolve(
    session: Session,
    *,
    ctx: WorkspaceContext,
    capability: str,
    clock: Clock,
) -> list[ModelPick]:
    """TTL-gated cache around :func:`_resolve_chain`.

    Returns a fresh list on each call so a caller mutating the
    returned list cannot clobber the cached tuple for the next
    caller. The cached value itself is immutable (tuple of frozen
    dataclasses).
    """
    key = capability
    now = clock.now()

    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is not None and entry.expires_at > now:
            return list(entry.chain)

    # Cache miss (or expired): resolve outside the lock — the DB
    # read can block, and other threads holding stale-but-valid
    # entries for different keys should not queue behind us.
    fresh = _resolve_chain(
        session, workspace_id=ctx.workspace_id, capability=capability
    )

    expires_at = now + timedelta(seconds=CACHE_TTL_SECONDS)
    with _CACHE_LOCK:
        # Last-writer-wins on race: two threads resolving the same
        # key simultaneously both write the same answer, so the race
        # is a small perf cost (double DB read) rather than a
        # correctness hazard. Using ``setdefault`` would strand the
        # later thread's fresher TTL; a plain assign keeps the
        # window predictable.
        _CACHE[key] = _CacheEntry(chain=tuple(fresh), expires_at=expires_at)

    return fresh


def resolve_model(
    session: Session,
    ctx: WorkspaceContext,
    capability: str,
    *,
    clock: Clock | None = None,
) -> list[ModelPick]:
    """Return the resolved fallback chain for ``capability``.

    Priority-ascending, enabled-only, with inheritance walked when
    the capability itself has no rows. Returns ``[]`` when no
    chain exists — prefer :func:`resolve_primary` when you want a
    single pick and fail-closed semantics.

    ``clock`` defaults to :class:`~app.util.clock.SystemClock`;
    tests thread a :class:`~app.util.clock.FrozenClock` through so
    TTL-advance cases are deterministic.
    """
    c = clock if clock is not None else SystemClock()
    settings = get_settings()
    if settings.demo_mode:
        if not llm_capability_allowed_in_demo(capability):
            return []
        return [demo_free_model_pick(capability=capability)]
    return _cached_or_resolve(session, ctx=ctx, capability=capability, clock=c)


def resolve_primary(
    session: Session,
    ctx: WorkspaceContext,
    capability: str,
    *,
    clock: Clock | None = None,
) -> ModelPick:
    """Return the head of the resolved chain for ``capability``.

    Raises :class:`CapabilityUnassignedError` when the chain is
    empty — the caller (API layer) maps this to
    ``503 capability_unassigned`` and writes the ``CRITICAL`` audit
    row per §11 "Failure modes".
    """
    chain = resolve_model(session, ctx, capability, clock=clock)
    if not chain:
        raise CapabilityUnassignedError(capability, ctx.workspace_id)
    return chain[0]


# ---------------------------------------------------------------------------
# Production wire-up
# ---------------------------------------------------------------------------


# Subscribe the cache-invalidation handler to the production bus at
# import time. Tests that construct a fresh :class:`EventBus`
# instance (isolation fixture pattern) can call
# :func:`_subscribe_to_bus` against it explicitly.
_subscribe_to_bus(default_event_bus)


# Kept purely for test isolation: drops the subscription set so a
# test that monkey-patches the bus can re-subscribe after reset.
# Production code must not call this.
def _reset_subscriptions_for_tests() -> None:
    """Clear the subscribed-bus dedup set.

    Paired with the usual ``EventBus._reset_for_tests`` pattern: a
    test that flips the production bus back to "empty" needs to
    tell us we are no longer wired up, or the next
    :func:`_subscribe_to_bus` call would no-op.
    """
    with _SUBSCRIBED_BUSES_LOCK:
        _SUBSCRIBED_BUSES.clear()
