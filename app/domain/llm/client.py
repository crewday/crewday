"""Capability-aware LLM client wrapper (cd-weue).

The :class:`LLMClient` here is the §11 "Client abstraction" seam:
callers pass a **capability key** (``chat.manager``, ``digest.manager``,
…) and get back an :class:`LLMResult` carrying the response text,
``usage``, ``finish_reason``, ``fallback_attempts``, and the
``correlation_id`` for the ``X-Correlation-Id-Echo`` /
``X-LLM-Fallback-Attempts`` header round-trip.

What this module composes (each piece already exists):

* :func:`~app.domain.llm.router.resolve_model` — capability →
  priority-ordered fallback chain of :class:`ModelPick` rungs.
* :class:`~app.adapters.llm.ports.LLMClient` — the transport adapter
  (OpenRouter today, ``fake`` in tests). Stays sync per §01
  "Adapters"; the wrapper crosses the sync/async boundary via
  :func:`asyncio.to_thread`.
* :func:`~app.domain.llm.budget.check_budget` /
  :func:`~app.domain.llm.budget.estimate_cost_cents` — pre-flight
  envelope check + per-call cost projection (§11 "Workspace usage
  budget").
* :func:`~app.domain.llm.usage_recorder.record` — post-flight
  ``llm_usage`` row + ledger bump (§11 "Cost tracking").

Walk semantics follow §11 "Retryable errors" verbatim:

* ``5xx`` from the provider, ``429``, client-side timeout, transport
  error, and ``finish_reason`` of ``safety`` (or equivalent content
  refusal) all advance the chain — every attempted rung writes one
  ``llm_usage`` row with ``status="error"`` and ``cost_cents=0``.
* :class:`~app.domain.llm.budget.BudgetExceeded` short-circuits the
  whole walk **before** any rung dispatches (§11 "Workspace usage
  budget"); the call never leaves the client and no row is written.
* :class:`~app.adapters.llm.openrouter.LlmProviderError` (non-retryable
  ``4xx``) is terminal — that rung records a ``status="error"`` row
  and the exception re-raises, bypassing the rest of the chain.
* Chain exhaustion re-raises the last provider error and writes a
  terminal-error row for the final rung.

Adapter interface stays the same — the wrapper hands ``api_model_id``
straight through to ``adapter.chat(...)``; the adapter still doesn't
know about budget or recorder.

See ``docs/specs/11-llm-and-agents.md`` §"Client abstraction",
§"Retryable errors", §"Failure modes".
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.adapters.llm.openrouter import (
    LlmContentRefused,
    LlmProviderError,
    LlmRateLimited,
    LlmTransportError,
)
from app.adapters.llm.ports import (
    ChatMessage,
    LLMResponse,
    LLMUsage,
    Tool,
    ToolCall,
)
from app.adapters.llm.ports import (
    LLMClient as LLMAdapter,
)
from app.domain.llm.budget import (
    PricingTable,
    check_budget,
    default_pricing_table,
    estimate_cost_cents,
)
from app.domain.llm.consent import load_consent_set
from app.domain.llm.router import (
    CapabilityUnassignedError,
    ModelPick,
    resolve_model,
)
from app.domain.llm.usage_recorder import AgentAttribution, record
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.redact import ConsentSet

__all__ = [
    "LLMChainExhausted",
    "LLMClient",
    "LLMResult",
    "is_retryable_error",
]

_log = logging.getLogger(__name__)


# §11 "Retryable errors" — the closed set of finish_reason strings
# that flag a content-refusal at the model layer. The provider names
# this differently per backend; ``safety`` is the OpenAI / OpenRouter
# canonical, ``content_filter`` is the Azure-OpenAI alias the spec
# also lists. A refusal is treated like a retryable transport failure:
# the rung records an error row (zero-cost) and the chain advances.
_CONTENT_REFUSAL_REASONS: frozenset[str] = frozenset({"safety", "content_filter"})


# Per-call defaults for the budget pre-flight projection. Domain
# capabilities have historically maintained their own constants; the
# wrapper accepts an explicit ``projected_prompt_tokens`` /
# ``projected_completion_tokens`` so the caller stays the source of
# truth on its own envelope shape. The defaults match the 1024 ceiling
# the OpenRouter adapter pins on every method (``max_tokens=1024``).
_DEFAULT_PROJECTED_PROMPT_TOKENS: int = 512
_DEFAULT_PROJECTED_COMPLETION_TOKENS: int = 1024


@dataclass(frozen=True, slots=True)
class LLMResult:
    """One successful capability call.

    Mirrors the §11 "Client abstraction" :class:`LLMResult`:

    * ``text`` — the assistant's text content. Empty when the model
      returned only :attr:`tool_calls`.
    * ``tool_calls`` — native function-calling output, if the model
      emitted any. Empty tuple otherwise.
    * ``usage`` — provider-reported token counts.
    * ``model_used`` — the ``api_model_id`` of the rung that succeeded.
    * ``assignment_id`` — the ``llm_assignment.id`` ULID that resolved
      to the winning rung (for /admin/usage's "which assignment served
      this call?" surface).
    * ``finish_reason`` — the provider's verbatim reason
      (``stop`` / ``length`` / ``tool_calls`` / …).
    * ``fallback_attempts`` — how many prior rungs failed before this
      one. ``0`` = first-rung success. The API surface that wraps
      :class:`LLMClient` reads this field and sets the
      ``X-LLM-Fallback-Attempts`` response header on the way out
      (§11 "Failure modes").
    * ``correlation_id`` — the tie-id shared across every rung; the
      caller echoes it back as ``X-Correlation-Id-Echo``.
    """

    text: str
    usage: LLMUsage
    model_used: str
    assignment_id: str
    finish_reason: str
    fallback_attempts: int
    correlation_id: str
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)


class LLMChainExhausted(RuntimeError):
    """Raised when every rung in a capability chain failed without success.

    Carries the same ``fallback_attempts`` and ``correlation_id`` the
    success-path :class:`LLMResult` exposes so the API layer can fill
    the ``X-LLM-Fallback-Attempts`` and ``X-Correlation-Id-Echo``
    response headers on the failure path too (§11 "Failure modes" —
    "A chain exhausted with no success surfaces the last error to the
    caller, with ``X-LLM-Fallback-Attempts`` echoing the number of
    models tried"). The original adapter exception is preserved both
    on :attr:`last_error` and as the exception cause (``raise … from
    last_error``) so callers and log formatters can either pattern-
    match on the wrapper type or unwrap to the underlying transport /
    rate-limit / refusal error.

    Distinct from :class:`LlmContentRefused` (which is the chain-
    exhausted refusal subtype) — :class:`LLMChainExhausted` is the
    broader "every rung tried something different and none worked"
    case where the last error happens to be transport / rate-limit.
    """

    __slots__ = ("correlation_id", "fallback_attempts", "last_error")

    def __init__(
        self,
        *,
        last_error: BaseException,
        fallback_attempts: int,
        correlation_id: str,
        message: str | None = None,
    ) -> None:
        super().__init__(
            message
            or (
                f"llm chain exhausted after {fallback_attempts + 1} "
                f"attempt(s); last_error={type(last_error).__name__}: {last_error}"
            )
        )
        self.last_error = last_error
        self.fallback_attempts = fallback_attempts
        self.correlation_id = correlation_id


def is_retryable_error(exc: BaseException) -> bool:
    """Classify a provider exception as retryable per §11 "Retryable errors".

    Retryable: 5xx, 429, timeout, transport (all surface from the
    OpenRouter adapter as :class:`LlmTransportError` /
    :class:`LlmRateLimited`).

    Terminal: non-retryable 4xx (:class:`LlmProviderError`),
    :class:`~app.domain.llm.budget.BudgetExceeded` (refusal —
    short-circuits the walk),
    :class:`~app.domain.llm.router.CapabilityUnassignedError`
    (unassigned — no chain to walk).

    Anything else (programmer errors, unrelated exceptions) is
    treated as terminal so a bug does not silently burn through the
    fallback chain.
    """
    return isinstance(exc, LlmRateLimited | LlmTransportError)


class LLMClient:
    """Capability-aware async wrapper around the sync transport adapter.

    Construction takes a transport adapter (the :class:`LLMAdapter`
    port; today either :class:`~app.adapters.llm.openrouter.OpenRouterClient`
    or :class:`~app.adapters.llm.fake.FakeLLMClient`) plus an optional
    :class:`PricingTable`. The wrapper itself is stateless beyond
    those references — every :meth:`chat` invocation runs the full
    resolve → pre-flight → walk → record cycle from scratch.

    Why async: the §11 spec pins :class:`LLMClient.chat` as
    ``async def``; the FastAPI request handlers that call into agent
    runtime / digest worker / chat surfaces are themselves async.
    The transport adapter stays sync (§01 "Adapters"); the wrapper
    uses :func:`asyncio.to_thread` for the actual provider call so a
    blocking ``httpx.Client.post`` inside the adapter does not stall
    the event loop.

    Single public method (:meth:`chat`) covers the §11 ``LLMClient.chat``
    surface. OCR / streaming sit on the adapter port; capabilities
    that need them call the adapter directly (the wrapper would
    otherwise have to model two more telemetry surfaces and the only
    callers today — receipt OCR, agent runtime — already know which
    rung to pick because they walked the chain themselves).
    """

    __slots__ = ("_adapter", "_pricing")

    def __init__(
        self,
        adapter: LLMAdapter,
        *,
        pricing: PricingTable | None = None,
    ) -> None:
        self._adapter = adapter
        self._pricing = pricing if pricing is not None else default_pricing_table()

    async def chat(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        capability: str,
        messages: Sequence[ChatMessage],
        attribution: AgentAttribution,
        max_output_tokens: int | None = None,
        tools: Sequence[Tool] | None = None,
        consents: ConsentSet | None = None,
        projected_prompt_tokens: int = _DEFAULT_PROJECTED_PROMPT_TOKENS,
        projected_completion_tokens: int = _DEFAULT_PROJECTED_COMPLETION_TOKENS,
        attempt_offset: int = 0,
        clock: Clock | None = None,
    ) -> LLMResult:
        """Resolve a chain, walk it, and return the first success.

        Walk semantics:

        1. Resolve the capability's chain via
           :func:`resolve_model`. An empty chain raises
           :class:`CapabilityUnassignedError` (the API layer maps it
           to ``503 capability_unassigned`` per §11 "Failure modes").
        2. For each rung: call :func:`check_budget` with the
           per-rung projected cost. A
           :class:`BudgetExceeded` propagates immediately — the call
           never leaves the client; no usage row is written for the
           rung that refused (§11 "At-cap behaviour").
        3. Dispatch through ``adapter.chat`` on a worker thread.
        4. Treat content refusal (``finish_reason in
           {"safety", "content_filter"}``) as a retryable error: log
           a ``status="error"`` row, advance to the next rung.
        5. On retryable transport / rate / timeout error, log a
           ``status="error"`` row and advance.
        6. On non-retryable provider error, log ``status="error"``
           and re-raise immediately — no further rungs.
        7. On success, log ``status="ok"`` with the actual cost and
           return :class:`LLMResult`.
        8. Chain exhausted with no success: raise
           :class:`LLMChainExhausted` carrying the underlying
           ``last_error``, ``fallback_attempts``, and
           ``correlation_id`` so the API layer can fill the
           ``X-LLM-Fallback-Attempts`` and ``X-Correlation-Id-Echo``
           headers on the failure path. If every rung refused on
           content grounds, raise :class:`LlmContentRefused` instead
           (subclass-discriminable) so callers wanting to react to
           refusals don't have to pattern-match on a string message.

        ``correlation_id`` comes from the active
        :class:`WorkspaceContext.audit_correlation_id` (set by
        :class:`~app.tenancy.middleware.TenantMiddleware` per
        request; never minted here so the API echoes the same id the
        client passed in via ``X-Correlation-Id``).

        ``attempt_offset`` lets a caller making multiple
        ``client.chat`` calls inside one request stagger the rows
        their walks write so the recorder's
        ``(workspace_id, correlation_id, attempt)`` idempotency unique
        does not silently swallow the second call. The wrapper does
        NOT mint per-call correlation ids — §11 wants the request id
        propagated end-to-end so the audit log can stitch related
        calls together.

        Compute the next call's offset from the previous call's
        outcome — the wrapper exposes the last attempted rung index on
        both the success and failure surfaces:

        * On success: ``next_offset = result.fallback_attempts + 1``.
        * On :class:`LLMChainExhausted`:
          ``next_offset = exc.fallback_attempts + 1``.
        * On :class:`LlmContentRefused` (chain-exhausted refusal):
          ``next_offset = exc.fallback_attempts + 1``.
        * On a terminal :class:`LlmProviderError`: the rung index is
          not exposed on the bare adapter exception; the caller
          should bump by ``1`` (a single rung was consumed) or skip
          the second call entirely — a non-retryable 4xx makes the
          retry meaningless anyway.

        The offset is caller-counted on purpose: the wrapper has no
        view of how many ``client.chat`` calls a single request
        intends to make. Getting it wrong silently swallows the
        second call's row at the recorder's idempotency unique.

        Per-rung consent is loaded from
        :func:`~app.domain.llm.consent.load_consent_set` once, before
        the walk — workspace consent does not change inside a single
        capability turn, and re-reading per rung would burn an
        unnecessary DB round-trip.
        """
        c = clock if clock is not None else SystemClock()

        chain = resolve_model(session, ctx, capability, clock=c)
        if not chain:
            raise CapabilityUnassignedError(capability, ctx.workspace_id)

        correlation_id = ctx.audit_correlation_id

        # Workspace consent doesn't change inside the fallback retry
        # loop; load once and reuse so the latency stopwatch below
        # covers only the LLM call. ``consents`` kwarg explicitly
        # overrides the workspace setting (admin smoke / debug paths).
        effective_consents = (
            consents
            if consents is not None
            else load_consent_set(session, ctx.workspace_id)
        )

        last_error: BaseException | None = None
        last_was_refusal = False
        for chain_index, model_pick in enumerate(chain):
            # ``fallback_attempts`` is always the chain index so the
            # spec contract ("0 = first-rung success") holds; the row
            # ``attempt`` is offset by ``attempt_offset`` so two
            # back-to-back ``client.chat`` calls with the same
            # correlation_id can both land rows.
            attempt_index = chain_index + attempt_offset
            projected_cost = estimate_cost_cents(
                prompt_tokens=projected_prompt_tokens,
                max_output_tokens=projected_completion_tokens,
                api_model_id=model_pick.api_model_id,
                pricing=self._pricing,
                workspace_id=ctx.workspace_id,
            )
            # BudgetExceeded propagates verbatim: §11 "Workspace usage
            # budget" pauses the WHOLE workspace, not just this rung,
            # so advancing to the next rung would be wrong. No usage
            # row is written; ``check_budget`` already logs the
            # ``llm.budget_exceeded`` event.
            check_budget(
                session,
                ctx,
                capability=capability,
                projected_cost_cents=projected_cost,
                clock=c,
            )

            started = c.now()
            try:
                response = await asyncio.to_thread(
                    self._adapter.chat,
                    model_id=model_pick.api_model_id,
                    messages=list(messages),
                    max_tokens=max_output_tokens or model_pick.max_tokens or 1024,
                    temperature=(
                        model_pick.temperature
                        if model_pick.temperature is not None
                        else 0.0
                    ),
                    tools=tools,
                    consents=effective_consents,
                )
            except (LlmRateLimited, LlmTransportError) as exc:
                # Retryable: write a zero-cost error row for this
                # rung and advance. ``cost_cents=0`` keeps the
                # ledger flat (the §11 invariant
                # :class:`LlmUsage.__post_init__` enforces).
                latency_ms = _elapsed_ms(started, c.now())
                _record_terminal_error(
                    session,
                    ctx,
                    capability=capability,
                    model_pick=model_pick,
                    correlation_id=correlation_id,
                    latency_ms=latency_ms,
                    fallback_attempts=chain_index,
                    attempt=attempt_index,
                    attribution=attribution,
                    clock=c,
                )
                last_error = exc
                last_was_refusal = False
                _log.info(
                    "llm.client.retryable_error",
                    extra={
                        "event": "llm.client.retryable_error",
                        "capability": capability,
                        "workspace_id": ctx.workspace_id,
                        "correlation_id": correlation_id,
                        "attempt": attempt_index,
                        "api_model_id": model_pick.api_model_id,
                        "error_type": type(exc).__name__,
                    },
                )
                continue
            except LlmProviderError:
                # Non-retryable 4xx: write the terminal row and
                # propagate. The chain stops here — retrying with a
                # different rung will not change the request's
                # validity.
                latency_ms = _elapsed_ms(started, c.now())
                _record_terminal_error(
                    session,
                    ctx,
                    capability=capability,
                    model_pick=model_pick,
                    correlation_id=correlation_id,
                    latency_ms=latency_ms,
                    fallback_attempts=chain_index,
                    attempt=attempt_index,
                    attribution=attribution,
                    clock=c,
                )
                raise

            latency_ms = _elapsed_ms(started, c.now())

            # Content-refusal: §11 "Retryable errors" lists
            # provider-reported safety refusals as retryable. Treat
            # the same as a transport failure — zero-cost error row,
            # advance the chain. This also guards against partial
            # refusals where the model returned text plus a safety
            # finish_reason; the /admin/usage feed sees a row with
            # ``finish_reason="safety"`` for the operator to inspect.
            if response.finish_reason in _CONTENT_REFUSAL_REASONS:
                _record_terminal_error(
                    session,
                    ctx,
                    capability=capability,
                    model_pick=model_pick,
                    correlation_id=correlation_id,
                    latency_ms=latency_ms,
                    fallback_attempts=chain_index,
                    attempt=attempt_index,
                    attribution=attribution,
                    clock=c,
                    finish_reason=response.finish_reason,
                )
                # ``fallback_attempts`` / ``correlation_id`` are
                # attached on every per-rung instance so the eventual
                # chain-exhausted raise (the loop's last refusal) lands
                # with the right header values. Per-rung values are
                # overwritten if the chain advances.
                last_error = LlmContentRefused(
                    f"openrouter content refusal: finish_reason="
                    f"{response.finish_reason!r}",
                    fallback_attempts=chain_index,
                    correlation_id=correlation_id,
                )
                last_was_refusal = True
                _log.info(
                    "llm.client.content_refusal",
                    extra={
                        "event": "llm.client.content_refusal",
                        "capability": capability,
                        "workspace_id": ctx.workspace_id,
                        "correlation_id": correlation_id,
                        "attempt": attempt_index,
                        "api_model_id": model_pick.api_model_id,
                        "finish_reason": response.finish_reason,
                    },
                )
                continue

            # Success: bill the actual reported tokens and return.
            cost_cents = estimate_cost_cents(
                prompt_tokens=response.usage.prompt_tokens,
                max_output_tokens=response.usage.completion_tokens,
                api_model_id=model_pick.api_model_id,
                pricing=self._pricing,
                workspace_id=ctx.workspace_id,
            )
            record(
                session,
                ctx,
                capability=capability,
                model_pick=model_pick,
                fallback_attempts=chain_index,
                correlation_id=correlation_id,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                cost_cents=cost_cents,
                latency_ms=latency_ms,
                status="ok",
                finish_reason=response.finish_reason,
                attribution=attribution,
                attempt=attempt_index,
                clock=c,
            )
            return _build_result(
                response,
                model_pick=model_pick,
                fallback_attempts=chain_index,
                correlation_id=correlation_id,
            )

        # Chain exhausted with no success. The recorder rows for
        # every attempted rung are already on disk, so /admin/usage
        # can reconstruct the failure timeline. Wrap the last error
        # in :class:`LLMChainExhausted` so the API layer can fill the
        # ``X-LLM-Fallback-Attempts`` / ``X-Correlation-Id-Echo``
        # headers on the failure path (§11 "Failure modes" — same
        # echo contract as the success path). ``fallback_attempts``
        # is the index of the last attempted rung, matching the
        # success-path semantics.
        fallback_attempts = len(chain) - 1
        if last_error is None:
            # Defensive: a non-empty chain whose every rung was
            # skipped without raising would land us here. The walk
            # above always either advances (with ``last_error`` set)
            # or returns; this is unreachable in practice.
            raise LLMChainExhausted(  # pragma: no cover - defensive
                last_error=LlmTransportError(
                    f"llm chain exhausted with no recorded error for "
                    f"capability={capability!r}"
                ),
                fallback_attempts=fallback_attempts,
                correlation_id=correlation_id,
            )
        if last_was_refusal:
            # Every rung refused on content grounds — surface the
            # refusal as a discriminable type so callers can react
            # without parsing the message. The recorder still wrote
            # one ``status="error"`` row per refused rung (with
            # ``finish_reason`` carried for /admin/usage diagnostics).
            # The refusal instance already carries
            # ``fallback_attempts`` / ``correlation_id`` (set inside
            # the loop) so the API layer can fill the
            # ``X-LLM-Fallback-Attempts`` / ``X-Correlation-Id-Echo``
            # headers without unwrapping.
            assert isinstance(last_error, LlmContentRefused)
            raise last_error
        raise LLMChainExhausted(
            last_error=last_error,
            fallback_attempts=fallback_attempts,
            correlation_id=correlation_id,
        ) from last_error


def _record_terminal_error(
    session: Session,
    ctx: WorkspaceContext,
    *,
    capability: str,
    model_pick: ModelPick,
    correlation_id: str,
    latency_ms: int,
    fallback_attempts: int,
    attempt: int,
    attribution: AgentAttribution,
    clock: Clock,
    finish_reason: str | None = None,
) -> None:
    """Write a zero-cost ``status="error"`` row for an attempted rung.

    §11 "Cost tracking" never bills a non-successful call — the
    :class:`~app.domain.llm.budget.LlmUsage.__post_init__` invariant
    enforces ``cost_cents == 0`` whenever ``status != "ok"``. The row
    still lands so /admin/usage can show "the chain tried this rung
    and it failed" with latency + ``finish_reason`` for diagnostics.
    """
    record(
        session,
        ctx,
        capability=capability,
        model_pick=model_pick,
        fallback_attempts=fallback_attempts,
        correlation_id=correlation_id,
        prompt_tokens=0,
        completion_tokens=0,
        cost_cents=0,
        latency_ms=latency_ms,
        status="error",
        finish_reason=finish_reason,
        attribution=attribution,
        attempt=attempt,
        clock=clock,
    )


def _build_result(
    response: LLMResponse,
    *,
    model_pick: ModelPick,
    fallback_attempts: int,
    correlation_id: str,
) -> LLMResult:
    """Project an :class:`LLMResponse` onto the domain :class:`LLMResult`."""
    return LLMResult(
        text=response.text,
        usage=response.usage,
        model_used=model_pick.api_model_id,
        assignment_id=model_pick.assignment_id,
        finish_reason=response.finish_reason,
        fallback_attempts=fallback_attempts,
        correlation_id=correlation_id,
        tool_calls=response.tool_calls,
    )


def _elapsed_ms(started: datetime, ended: datetime) -> int:
    """Return milliseconds between two :class:`~datetime.datetime` instants.

    Mirrors the helper in :mod:`app.adapters.llm.openrouter` — kept
    local rather than re-imported so the wrapper has no inbound
    dependency on the OpenRouter module beyond its exception types.
    """
    delta = ended - started
    return max(0, int(delta.total_seconds() * 1000))
