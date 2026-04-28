"""Agent turn runtime — plan loop, tool dispatch, audit on every action.

Spec: ``docs/specs/11-llm-and-agents.md`` §"Embedded agents",
§"Agent turn lifecycle", §"Conversation compaction",
§"Agent audit trail", §"The agent-first invariant",
§"Agent authority boundary".

A **turn** is the span from a user message landing on an agent
endpoint until the runtime produces the next observable outcome —
an appended :class:`~app.adapters.db.messaging.models.ChatMessage`,
a pending :class:`~app.adapters.db.llm.models.ApprovalRequest`, or
an error. The same loop serves both trigger modes:

* ``trigger="event"`` — chat-gateway / message-arrived (a user
  message exists; the thread is bound to a channel).
* ``trigger="schedule"`` — cron capability (no thread necessarily;
  a "system message" simulates the user prompt).

Per turn:

1. Emit :class:`~app.events.types.AgentTurnStarted` so every
   connected tab of the delegating user flips to a "working on it"
   state without polling.
2. Resolve the capability via
   :func:`~app.domain.llm.router.resolve_primary` (cd-k0qf) —
   ``CapabilityUnassignedError`` short-circuits to
   ``outcome=error``.
3. Mint (or accept) a delegated token for ``ctx.actor_id`` so the
   tool dispatcher acts as the delegating user. The token's label
   becomes the ``agent_label`` denormalised onto every audit row
   the turn writes.
4. Pre-flight the workspace 30-day budget envelope (cd-irng). If
   the call would overshoot, refuse — ``outcome=error`` with code
   ``budget_exceeded``; no LLM call leaves the client; no
   ``llm_usage`` row is written.
5. Loop up to ``max_iterations`` (default 8) and ``wall_clock_timeout_s``
   (default 60s):

   a. Call ``llm_client.chat(...)`` with the assembled history.
   b. Parse the response:

      * **Plain text** → write a :class:`ChatMessage` row, emit
        :class:`AgentTurnFinished` with ``outcome=replied``,
        return.
      * **Tool call** (``<tool_call name="…" input="…"/>`` text
        protocol — see :func:`_parse_tool_call`) → ask the
        dispatcher whether the tool is gated.
      * **Gated** → write an
        :class:`~app.adapters.db.llm.models.ApprovalRequest`
        row, emit :class:`AgentTurnFinished` with
        ``outcome=action``, return. The full HITL pipeline
        (cd-9ghv) consumes the row from there.
      * **Read tool** → execute via dispatcher, append the
        result to history, loop. Reads do **not** audit.
      * **Write tool** → execute via dispatcher, write one
        ``audit_log`` row attributed to ``ctx.actor_id`` (the
        delegating user, never the agent token row), append
        the result to history, loop.
   c. Iteration cap or wall-clock cap → write a friendly
      "the request was too large" reply as a :class:`ChatMessage`,
      emit :class:`AgentTurnFinished` with ``outcome=timeout``,
      return.

Audit attribution per AC (§11 "Agent audit trail"):

* ``actor_kind = "user"``
* ``actor_id = ctx.actor_id`` — the delegating user, never the
  agent token row's id.
* ``token_id = <delegated token id>``
* ``agent_label = <agent label>``
* ``agent_conversation_ref = <thread external_ref or None>``
* ``correlation_id`` — pinned for the turn so /admin/usage can
  cluster the chain of calls.

Tenant + permission isolation:

* The dispatcher invokes the API surface (or a domain function)
  through the existing tenancy middleware + role-grant gate, so a
  read against a row the delegating user can't see returns
  empty / 403. The runtime never bypasses these — it propagates
  the result to the LLM as a tool result string.
* The runtime hands the dispatcher a delegated token and the
  ``X-Agent-Channel`` / ``X-Agent-Conversation-Ref`` /
  ``X-Agent-Reason`` headers; the audit attribution then rides on
  the request that lands in the API layer.

What this module deliberately does NOT do (filed as Beads
follow-ups before coding so the carve-outs are explicit):

* **Native LLM tool-use** (cd-um36). The MVP parses tool calls
  from a free-text protocol (XML-style ``<tool_call name="…">``)
  emitted by the model. This works on every adapter today; the
  port extension (``Tool`` / ``ToolCall`` / ``LLMResponse.
  tool_calls``) lands separately so the OpenRouter wiring can be
  reviewed independently.
* **OpenAPI in-process dispatcher** (cd-z3b7). The runtime calls
  a :class:`ToolDispatcher` Protocol; the production
  implementation that walks the FastAPI router lands separately
  so the dispatcher contract can be reviewed against the agent
  surface that doesn't yet exist (no ``app/api/v1/agent/*`` today).
* **Deep conversation compaction** (cd-cn7v). The MVP caps history
  at the last 30 messages on the thread. The full §11 compaction
  story (single-summary invariant, span columns, recent-window
  floor, trigger heuristics, ``chat.compact`` capability) lands
  as a worker tick, not inside this module.
* **System prompt from llm_prompt_template** (cd-4if3). The MVP
  builds a minimal default per-scope system prompt. The
  prompt-library reader + per-workspace overrides land separately.
* **Full HITL approval pipeline** (cd-9ghv). The runtime writes
  an ``ApprovalRequest`` row and pauses; cd-9ghv is the consumer
  that wires the full state machine, the inline cards, and the
  /approvals desk.

See also ``docs/specs/03-auth-and-tokens.md`` §"Delegated tokens"
for the token contract.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import ApprovalRequest
from app.adapters.db.messaging.models import ChatChannel, ChatMessage
from app.adapters.llm.ports import ChatMessage as LlmChatMessage
from app.adapters.llm.ports import LLMClient, LLMResponse
from app.audit import write_audit
from app.domain.agent.preferences import (
    PreferenceBundle,
    blocked_action_result_body,
    is_action_blocked,
    resolve_preferences,
)
from app.domain.llm.budget import (
    BudgetExceeded,
    PricingTable,
    check_budget,
    default_pricing_table,
    estimate_cost_cents,
)
from app.domain.llm.router import (
    CapabilityUnassignedError,
    ModelPick,
    resolve_primary,
)
from app.domain.llm.usage_recorder import AgentAttribution, record
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import (
    AgentActionPending,
    AgentTurnFinished,
    AgentTurnOutcome,
    AgentTurnScope,
    AgentTurnStarted,
)
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "APPROVAL_REQUEST_TTL",
    "DEFAULT_HISTORY_CAP",
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_WALL_CLOCK_TIMEOUT_S",
    "DELEGATED_TOKEN_TTL",
    "AgentTurnError",
    "DelegatedToken",
    "GateDecision",
    "TimeoutReply",
    "TokenFactory",
    "ToolCall",
    "ToolDispatcher",
    "ToolResult",
    "TurnOutcome",
    "TurnTrigger",
    "run_turn",
]


_log = logging.getLogger(__name__)


# §11 "Agent turn lifecycle" pin: 8 tool-loop iterations per turn,
# 60s wall-clock budget. Both kept as module-level constants so the
# defaults are visible from one place and tests can dial them down
# without reaching into the function signature.
DEFAULT_MAX_ITERATIONS: Final[int] = 8
DEFAULT_WALL_CLOCK_TIMEOUT_S: Final[int] = 60

# Cap on how many recent chat-message rows the runtime threads into
# the assembled history. The full §11 compaction story (single-
# summary invariant, span columns, recent-window floor) lands in
# cd-cn7v; until then a simple "last N" floor is enough to keep the
# token budget bounded for v1 traffic.
DEFAULT_HISTORY_CAP: Final[int] = 30

# Lease size of the delegated token the runtime hands the dispatcher.
# Sized so the whole turn — even the worst-case tool loop — finishes
# well before expiry, while keeping the long-tail blast radius of a
# leaked plaintext small. Math: the iteration cap is
# ``DEFAULT_MAX_ITERATIONS`` (8) and a slow tool call lands in
# roughly 10 s, so the worst credible turn is ~80 s plus network
# jitter — bounded above by ``DEFAULT_WALL_CLOCK_TIMEOUT_S`` (60 s).
# 10 minutes is an order of magnitude over that cap, leaving
# headroom if a future change lifts the iteration cap or the LLM
# stalls. Out-of-band revocation is per-token via
# :func:`app.auth.tokens.revoke`.
DELEGATED_TOKEN_TTL: Final[timedelta] = timedelta(minutes=10)

# Default TTL for an :class:`ApprovalRequest` row the runtime mints.
# Pinned to 7 days per §11 "TTL"; the cd-9ghv worker (`approval_ttl`)
# walks the table every 15 min and flips a row whose ``expires_at``
# has passed to ``status='timed_out'``. Lifted to a module-level
# constant so the worker test + the runtime test agree on the value
# without re-reading the spec.
APPROVAL_REQUEST_TTL: Final[timedelta] = timedelta(days=7)

# Budget pre-flight projection. The router doesn't yet expose a
# per-capability default ``max_tokens`` — until cd-um36 surfaces the
# native tool-use schema, every chat call is projected at 1024
# completion tokens, matching the existing :class:`LLMClient.chat`
# default. The estimator walks per-token pricing; an unknown model
# falls back to ``(0, 0)`` per §11 "Pricing source", so the
# projection on a free-tier model is zero and the call always
# clears the envelope.
_PROJECTED_COMPLETION_TOKENS: Final[int] = 1024
# Same pre-flight projection on the prompt side. The runtime can't
# count tokens at this layer (no tokenizer port today); a rough
# 500-token estimate covers the system prompt + a typical
# 30-message history without inflating the projection so much that
# small calls trip the envelope. cd-um36's port extension is the
# right seat for a real tokenizer hook.
_PROJECTED_PROMPT_TOKENS: Final[int] = 500


# Trigger discriminator. Mirrors the §11 lifecycle pin
# (``event | schedule``) and is exported so callers (HTTP handler,
# cron worker) can pass the value verbatim from the surface that
# triggered the turn.
TurnTrigger = Literal["event", "schedule"]


# ---------------------------------------------------------------------------
# Public value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """Result of one :func:`run_turn` invocation.

    Frozen + slotted so callers can stash the value on a request
    record without aliasing risk. The four ``outcome`` branches
    partition the §11 "Agent turn lifecycle" terminal states:

    * ``replied`` — a chat-message reply landed; ``chat_message_id``
      points at the inserted row.
    * ``action`` — an approval row landed; ``approval_request_id``
      points at the inserted row.
    * ``error`` — the runtime raised (capability unassigned, budget
      refused, transport failure). Neither id is populated; the
      caller surfaces the error to the chat thread itself
      (a friendly message + a re-try affordance).
    * ``timeout`` — the iteration cap or wall-clock cap fired. The
      runtime wrote a friendly fallback :class:`ChatMessage` so the
      user always sees a response; ``chat_message_id`` carries the
      fallback row's id.

    ``tool_calls_made`` / ``llm_calls_made`` are observability
    counters — surfaced on the /admin/usage feed so the operator
    can spot a runaway capability without re-walking the audit log.
    ``started_at`` / ``ended_at`` are the same instants the paired
    SSE events carry on their payloads.
    """

    outcome: AgentTurnOutcome
    chat_message_id: str | None
    approval_request_id: str | None
    tool_calls_made: int
    llm_calls_made: int
    started_at: datetime
    ended_at: datetime
    # Pinned for the whole turn so /admin/usage can cluster the
    # chain of LLM calls + the audit row(s) they wrote. Echoed back
    # to the caller via :attr:`AgentAttribution.agent_conversation_ref`
    # on every recorded LLM usage row.
    correlation_id: str
    # Surfaced so the API layer can render a 402 / 503 shape verbatim
    # when ``outcome=error``; ``None`` on the happy path.
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class DelegatedToken:
    """Plaintext + id pair returned by :class:`TokenFactory.mint_for`.

    ``plaintext`` is the value the dispatcher places in the
    ``Authorization: Bearer …`` header it sends to the API surface.
    ``token_id`` is the row's ULID — denormalised onto every audit
    row the turn writes (per §11 "Agent audit trail" /
    ``audit_log.token_id``).
    """

    plaintext: str
    token_id: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A single tool invocation request emitted by the LLM.

    The MVP runtime parses tool calls from a free-text protocol
    (``<tool_call name="…" input="…"/>``) — see :func:`_parse_tool_call`.
    The native tool-use port extension lands in cd-um36; once that
    ships, the runtime resolves :attr:`tool_calls` off the
    :class:`LLMResponse` directly and this dataclass is the surface
    both paths feed.

    ``input`` is a parsed JSON object — the dispatcher revalidates
    against the route's request model. ``id`` is a turn-local
    identifier the dispatcher echoes back on the result so multiple
    parallel calls (future) can be paired.
    """

    id: str
    name: str
    input: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The dispatcher's return value for one :class:`ToolCall`.

    ``status_code`` mirrors the HTTP semantic (200, 403, 404, 422,
    …); the runtime renders it back into the prompt context so the
    LLM can react to a "not found" or "permission denied" without
    re-issuing the same call. ``body`` is the decoded JSON response
    — the runtime stringifies it into the appended turn.

    ``mutated`` is the dispatcher's verdict: did the call write?
    Reads (``GET``, ``--dry-run``, ``--explain``) carry ``False``
    and skip the audit write; writes (``POST``, ``PATCH``,
    ``DELETE``) carry ``True`` and the runtime emits one
    :func:`~app.audit.write_audit` row.
    """

    call_id: str
    status_code: int
    body: object
    mutated: bool


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Whether a tool call must wait for human approval (§11 HITL).

    Returned from :meth:`ToolDispatcher.is_gated`. ``gated=False``
    is the common path: dispatch immediately, audit if the result
    mutated. ``gated=True`` halts the turn — the runtime writes an
    :class:`~app.adapters.db.llm.models.ApprovalRequest` row using
    :attr:`card_summary` / :attr:`card_risk` /
    :attr:`pre_approval_source` and emits ``outcome=action``.

    ``card_summary`` / ``card_risk`` /
    :attr:`pre_approval_source` mirror the §11 ``agent_action``
    schema fields so the cd-9ghv consumer can read them off the
    row without re-resolving the route's annotation. ``gated=False``
    leaves them empty; the runtime never inspects them on the happy
    path.
    """

    gated: bool
    card_summary: str = ""
    card_risk: Literal["low", "medium", "high"] = "low"
    pre_approval_source: str = ""


# ---------------------------------------------------------------------------
# Protocol seams
# ---------------------------------------------------------------------------


class TokenFactory(Protocol):
    """Mints (or returns) the delegated token the dispatcher will use.

    Production wiring calls :func:`app.auth.tokens.mint` with
    ``kind="delegated"`` and ``delegate_for_user_id=ctx.actor_id``.
    Tests stub a fake that returns a deterministic id without hitting
    the password hasher (argon2id is the slowest path in the suite).

    The factory is asked exactly once per turn — even if the LLM
    fires multiple tool calls, they all use the same token. The
    short TTL on the delegated kind (30d default per §03) is the
    long-tail safety; revocation is per-token via the existing
    :func:`~app.auth.tokens.revoke` path.
    """

    def mint_for(
        self,
        ctx: WorkspaceContext,
        *,
        agent_label: str,
        expires_at: datetime,
    ) -> DelegatedToken:
        """Return a fresh (or reused) delegated token for the turn."""
        ...


class ToolDispatcher(Protocol):
    """Invokes a tool call against the API surface.

    The production wiring (cd-z3b7) walks the FastAPI router with a
    :class:`fastapi.testclient.TestClient`, presents the delegated
    token as ``Authorization: Bearer …``, and propagates the
    ``X-Agent-Channel`` / ``X-Agent-Conversation-Ref`` /
    ``X-Agent-Reason`` headers. Tests stub a fake that bypasses the
    HTTP layer and returns canned :class:`ToolResult` instances.

    Two responsibilities:

    * :meth:`is_gated` — pre-flight: ask whether the call would
      route through the §11 approval pipeline. The runtime calls
      this BEFORE :meth:`dispatch` so a gated call never leaves
      the client.
    * :meth:`dispatch` — execute the call, return the result. Reads
      and writes both come through here; the runtime branches on
      :attr:`ToolResult.mutated` to decide whether to audit.
    """

    def is_gated(self, call: ToolCall) -> GateDecision:
        """Return the gate decision for ``call``."""
        ...

    def dispatch(
        self,
        call: ToolCall,
        *,
        token: DelegatedToken,
        headers: Mapping[str, str],
    ) -> ToolResult:
        """Execute ``call`` and return its :class:`ToolResult`."""
        ...


# ---------------------------------------------------------------------------
# Errors (internal)
# ---------------------------------------------------------------------------


class AgentTurnError(RuntimeError):
    """Domain error raised by :func:`run_turn` for caller-facing failures.

    Distinct from the upstream errors (router's
    :class:`~app.domain.llm.router.CapabilityUnassignedError`,
    budget's :class:`~app.domain.llm.budget.BudgetExceeded`) — the
    runtime collapses each into a :class:`TurnOutcome` with
    ``outcome="error"`` and a structured ``error_code``, never
    re-raising the upstream type. This wrapper exists for the
    truly-unexpected case (a tool call that raised mid-dispatch with
    no recovery path) so the API layer maps the bubble to a 500
    instead of a silent cascade.
    """


class TimeoutReply(RuntimeError):
    """Internal sentinel — caught by :func:`run_turn` to flip to ``timeout``.

    Raised by :func:`_check_caps` when the iteration cap or
    wall-clock cap fires inside the loop. The error path then writes
    the friendly "request was too large" fallback chat message and
    returns ``outcome=timeout``.
    """


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def run_turn(
    ctx: WorkspaceContext,
    *,
    session: Session,
    scope: AgentTurnScope,
    thread_id: str | None,
    user_message: str,
    trigger: TurnTrigger,
    llm_client: LLMClient,
    tool_dispatcher: ToolDispatcher,
    token_factory: TokenFactory,
    agent_label: str,
    capability: str,
    pricing: PricingTable | None = None,
    event_bus: EventBus | None = None,
    clock: Clock | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    wall_clock_timeout_s: int = DEFAULT_WALL_CLOCK_TIMEOUT_S,
    history_cap: int = DEFAULT_HISTORY_CAP,
) -> TurnOutcome:
    """Run one agent turn end-to-end.

    See module docstring for the loop semantics. The arguments are:

    * ``ctx`` — the delegating user's :class:`WorkspaceContext`. The
      runtime audits and routes every tool call as this user.
    * ``session`` — the open SQLAlchemy session; the caller's UoW
      owns the transaction boundary (the runtime never commits).
    * ``scope`` — ``employee | manager | admin | task`` per §11
      "Embedded agents". Drives the system prompt and the SSE
      event payload.
    * ``thread_id`` — chat-channel id for an ``event``-trigger turn;
      ``None`` for a ``schedule``-trigger turn that has no thread.
      A ``None`` thread on an ``event`` trigger raises
      :class:`AgentTurnError` (the chat surface contract requires
      a channel for the message + reply to land on).
    * ``user_message`` — the user's text. For ``schedule`` triggers,
      the cron worker passes the synthetic system prompt
      (e.g. ``"Daily digest"``).
    * ``trigger`` — see :data:`TurnTrigger`.
    * ``llm_client`` — port-typed; the production wiring is
      :class:`~app.adapters.llm.openrouter.OpenRouterClient`,
      tests use ``EchoLLMClient`` or a richer fake.
    * ``tool_dispatcher`` — see :class:`ToolDispatcher`.
    * ``token_factory`` — see :class:`TokenFactory`.
    * ``agent_label`` — denormalised onto every audit row + every
      ``llm_usage`` row this turn writes. Mirrors the chat agent's
      label (``manager-chat-agent``, ``worker-chat-agent``,
      ``admin-chat-agent``).
    * ``capability`` — §11 capability key the router resolves
      (``chat.manager`` / ``chat.employee`` / ``chat.admin``).
    * ``pricing`` — pricing table for budget projection. ``None``
      uses :func:`default_pricing_table` (every model is unknown
      and falls back to ``(0, 0)``); production wires the real
      table from the registry.
    * ``event_bus`` — bus for the started / finished SSE events.
      Defaults to the production singleton.
    * ``clock`` — test seam; defaults to :class:`SystemClock`.
    * ``max_iterations`` / ``wall_clock_timeout_s`` /
      ``history_cap`` — caps with §11-pinned defaults; tests dial
      them down for the timeout / iteration-cap branches.

    Never raises on the §11 expected failure modes (capability
    unassigned, budget exceeded, iteration cap, wall-clock cap) —
    each becomes a :class:`TurnOutcome` with the right
    ``outcome`` / ``error_code`` so the API layer surfaces a clean
    response. A truly-unexpected failure (dispatcher raised, LLM
    adapter raised something other than the documented errors) does
    bubble — the API layer maps the cascade to 500, the operator
    sees the traceback, and the next turn re-runs from scratch.
    """
    bus = event_bus if event_bus is not None else default_event_bus
    eff_clock: Clock = clock if clock is not None else SystemClock()
    eff_pricing: PricingTable = (
        pricing if pricing is not None else default_pricing_table()
    )

    correlation_id = new_ulid(clock=eff_clock)
    started_at = eff_clock.now()

    # Validate the trigger / thread coupling early so a misuse fails
    # before any side-effect lands. The cron worker always passes
    # ``thread_id=None`` (no chat surface for a daily digest); the
    # chat-gateway always passes a concrete id (the channel the
    # message arrived on).
    if trigger == "event" and thread_id is None:
        raise AgentTurnError(
            "trigger='event' requires a chat thread_id; "
            "scheduled turns pass trigger='schedule' instead."
        )

    bus.publish(
        AgentTurnStarted(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            actor_user_id=ctx.actor_id,
            correlation_id=correlation_id,
            occurred_at=started_at,
            scope=scope,
            thread_id=thread_id,
            trigger=trigger,
            started_at=started_at,
        )
    )

    # Resolve the capability. ``CapabilityUnassignedError`` is the
    # router's typed signal that no enabled assignment exists for
    # this workspace; collapse to ``outcome=error`` with the spec
    # code.
    try:
        model_pick = resolve_primary(session, ctx, capability, clock=eff_clock)
    except CapabilityUnassignedError as exc:
        return _finish_error(
            bus=bus,
            ctx=ctx,
            scope=scope,
            thread_id=thread_id,
            trigger=trigger,
            correlation_id=correlation_id,
            started_at=started_at,
            ended_at=eff_clock.now(),
            error_code="capability_unassigned",
            error_message=str(exc),
        )

    # Budget pre-flight. A refusal short-circuits before the LLM
    # call leaves the client; no ``llm_usage`` row is written
    # (§11 "At-cap behaviour"). The estimator works on the projected
    # token counts because the runtime can't pre-tokenise the
    # prompt at this layer; a real tokenizer hook is part of cd-um36.
    projected_cost = estimate_cost_cents(
        prompt_tokens=_PROJECTED_PROMPT_TOKENS,
        max_output_tokens=_PROJECTED_COMPLETION_TOKENS,
        api_model_id=model_pick.api_model_id,
        pricing=eff_pricing,
        workspace_id=ctx.workspace_id,
    )
    try:
        check_budget(
            session,
            ctx,
            capability=capability,
            projected_cost_cents=projected_cost,
            clock=eff_clock,
        )
    except BudgetExceeded as exc:
        return _finish_error(
            bus=bus,
            ctx=ctx,
            scope=scope,
            thread_id=thread_id,
            trigger=trigger,
            correlation_id=correlation_id,
            started_at=started_at,
            ended_at=eff_clock.now(),
            error_code="budget_exceeded",
            error_message=exc.message_text,
        )

    # Mint the delegated token. The factory may reuse an existing
    # row (a long-running chat thread doesn't need a fresh argon2id
    # hash on every turn); the contract is "the dispatcher can use
    # this Bearer to act as the delegating user".
    #
    # ``DELEGATED_TOKEN_TTL_S`` (10 minutes) sizes the lease so the
    # whole turn — even a worst-case tool loop — comfortably finishes
    # before the token expires, while keeping the long-tail blast
    # radius of a leaked plaintext small. Math: the iteration cap is
    # ``DEFAULT_MAX_ITERATIONS`` (8) and a slow tool call lands in
    # ~10 seconds, so the worst credible turn is ~80 seconds plus
    # network jitter — well under the 60s wall-clock cap, and an
    # order of magnitude under the 10-minute TTL. Headroom covers a
    # future lift of the iteration cap or a slow LLM response without
    # forcing the dispatcher to deal with mid-turn token rotation.
    # Out-of-band revocation is per-token via ``app.auth.tokens.revoke``.
    token_expires_at = eff_clock.now() + DELEGATED_TOKEN_TTL
    token = token_factory.mint_for(
        ctx, agent_label=agent_label, expires_at=token_expires_at
    )

    # Resolve the channel's external_ref once so every audit row
    # the turn writes carries the same ``agent_conversation_ref``.
    # ``None`` for a scheduled turn or a thread with no external
    # binding — perfectly fine, the audit column is nullable.
    agent_conversation_ref = _resolve_external_ref(session, thread_id=thread_id)

    headers: dict[str, str] = {
        "X-Agent-Channel": _channel_header_value(scope),
        "X-Agent-Reason": f"agent.turn:{correlation_id}",
    }
    if agent_conversation_ref is not None:
        headers["X-Agent-Conversation-Ref"] = agent_conversation_ref

    attribution = AgentAttribution(
        actor_user_id=ctx.actor_id,
        token_id=token.token_id,
        agent_label=agent_label,
        agent_conversation_ref=agent_conversation_ref,
    )
    preferences = resolve_preferences(
        session,
        ctx,
        capability=capability,
        user_id=ctx.actor_id,
    )

    history = _assemble_history(
        session,
        scope=scope,
        thread_id=thread_id,
        user_message=user_message,
        history_cap=history_cap,
        preferences=preferences,
    )

    tool_calls_made = 0
    llm_calls_made = 0
    deadline = started_at + timedelta(seconds=wall_clock_timeout_s)

    try:
        for iteration in range(max_iterations):
            _check_wall_clock(eff_clock, deadline)

            response = _call_llm(
                llm_client=llm_client,
                session=session,
                ctx=ctx,
                model_pick=model_pick,
                history=history,
                capability=capability,
                correlation_id=correlation_id,
                attempt=iteration,
                attribution=attribution,
                pricing=eff_pricing,
                clock=eff_clock,
            )
            llm_calls_made += 1

            tool_call = _parse_tool_call(response.text)
            if tool_call is None:
                # Plain-text reply path: write the chat message,
                # emit ``finished``, return. The reply lands as an
                # ``agent``-labelled row pointing at the delegating
                # user (per §23 ``chat_message`` "Authored" shape).
                chat_message_id = _write_chat_reply(
                    session,
                    ctx=ctx,
                    thread_id=thread_id,
                    body_md=response.text or "",
                    clock=eff_clock,
                )
                ended_at = eff_clock.now()
                bus.publish(
                    AgentTurnFinished(
                        workspace_id=ctx.workspace_id,
                        actor_id=ctx.actor_id,
                        actor_user_id=ctx.actor_id,
                        correlation_id=correlation_id,
                        occurred_at=ended_at,
                        scope=scope,
                        thread_id=thread_id,
                        trigger=trigger,
                        finished_at=ended_at,
                        outcome="replied",
                    )
                )
                return TurnOutcome(
                    outcome="replied",
                    chat_message_id=chat_message_id,
                    approval_request_id=None,
                    tool_calls_made=tool_calls_made,
                    llm_calls_made=llm_calls_made,
                    started_at=started_at,
                    ended_at=ended_at,
                    correlation_id=correlation_id,
                )

            # Tool-call branch: gate decision first.
            tool_calls_made += 1
            if is_action_blocked(preferences, tool_call.name):
                history = _append_tool_result_to_history(
                    history,
                    tool_call,
                    ToolResult(
                        call_id=tool_call.id,
                        status_code=403,
                        body=blocked_action_result_body(tool_call.name),
                        mutated=False,
                    ),
                )
                continue
            decision = tool_dispatcher.is_gated(tool_call)
            if decision.gated:
                # The chat-trigger turn always has a delegating user
                # (``ctx.actor_id``) the inline approval card belongs
                # to; a scheduled-trigger turn has no chat surface
                # but still routes to the delegating user the cron
                # capability runs as. Both paths set ``for_user_id``
                # to ``ctx.actor_id`` — desk-only approvals (no
                # delegating user at all) are a follow-up surface
                # (cd-6bcl) that does not currently mint rows
                # through this path. Per-user agent approval mode
                # (§11 "Per-user agent approval mode") is not yet
                # threaded into the runtime; ``resolved_user_mode``
                # stays ``None`` until that column lands on
                # :class:`User`.
                # The returned ``expires_at`` is recorded on the row
                # but not surfaced through :class:`TurnOutcome` today —
                # callers that need it (e.g. an HTTP handler that
                # wants to put it in the 409 envelope) re-read the
                # row. A future TurnOutcome extension can carry it
                # if a hot path emerges; bind to ``_`` so the pair-
                # return contract stays explicit at the call site.
                approval_id, _ = _write_approval_request(
                    session,
                    ctx=ctx,
                    tool_call=tool_call,
                    decision=decision,
                    correlation_id=correlation_id,
                    requester_actor_id=ctx.actor_id,
                    inline_channel=_channel_header_value(scope),
                    for_user_id=ctx.actor_id,
                    resolved_user_mode=None,
                    clock=eff_clock,
                )
                ended_at = eff_clock.now()
                # Publish the inline approval-card SSE event to the
                # delegating user's tabs. ``AgentActionPending`` is
                # ``user_scoped=True`` so the SSE transport filters
                # the fan-out to the actor whose ``actor_user_id``
                # matches; the §11 inline-card surface drops the
                # card on the wrong tabs without this event.
                bus.publish(
                    AgentActionPending(
                        workspace_id=ctx.workspace_id,
                        actor_id=ctx.actor_id,
                        actor_user_id=ctx.actor_id,
                        correlation_id=correlation_id,
                        occurred_at=ended_at,
                        approval_request_id=approval_id,
                        scope=scope,
                        thread_id=thread_id,
                    )
                )
                bus.publish(
                    AgentTurnFinished(
                        workspace_id=ctx.workspace_id,
                        actor_id=ctx.actor_id,
                        actor_user_id=ctx.actor_id,
                        correlation_id=correlation_id,
                        occurred_at=ended_at,
                        scope=scope,
                        thread_id=thread_id,
                        trigger=trigger,
                        finished_at=ended_at,
                        outcome="action",
                    )
                )
                return TurnOutcome(
                    outcome="action",
                    chat_message_id=None,
                    approval_request_id=approval_id,
                    tool_calls_made=tool_calls_made,
                    llm_calls_made=llm_calls_made,
                    started_at=started_at,
                    ended_at=ended_at,
                    correlation_id=correlation_id,
                )

            # Non-gated dispatch.
            result = tool_dispatcher.dispatch(
                tool_call,
                token=token,
                headers=headers,
            )
            if result.mutated:
                _audit_tool_call(
                    session,
                    ctx=ctx,
                    tool_call=tool_call,
                    result=result,
                    token_id=token.token_id,
                    agent_label=agent_label,
                    agent_conversation_ref=agent_conversation_ref,
                    correlation_id=correlation_id,
                    clock=eff_clock,
                )

            # Append the result to the history so the LLM can react
            # in the next iteration. We render the result as a
            # human-readable assistant turn rather than a raw JSON
            # blob so the next prompt doesn't have to re-parse it;
            # the model's free-text protocol benefits from the
            # explicit framing.
            history = _append_tool_result_to_history(history, tool_call, result)
        # Loop fell out without returning → iteration cap fired.
        raise TimeoutReply("iteration_cap")
    except TimeoutReply:
        chat_message_id = _write_chat_reply(
            session,
            ctx=ctx,
            thread_id=thread_id,
            body_md=_TIMEOUT_REPLY_TEXT,
            clock=eff_clock,
        )
        ended_at = eff_clock.now()
        bus.publish(
            AgentTurnFinished(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                actor_user_id=ctx.actor_id,
                correlation_id=correlation_id,
                occurred_at=ended_at,
                scope=scope,
                thread_id=thread_id,
                trigger=trigger,
                finished_at=ended_at,
                outcome="timeout",
            )
        )
        return TurnOutcome(
            outcome="timeout",
            chat_message_id=chat_message_id,
            approval_request_id=None,
            tool_calls_made=tool_calls_made,
            llm_calls_made=llm_calls_made,
            started_at=started_at,
            ended_at=ended_at,
            correlation_id=correlation_id,
        )


# ---------------------------------------------------------------------------
# Internal helpers — turn lifecycle plumbing
# ---------------------------------------------------------------------------


# Friendly fallback when the iteration / wall-clock cap fires. Kept
# as a module constant so the test suite can assert on the exact
# string and product / i18n changes have one canonical site.
_TIMEOUT_REPLY_TEXT: Final[str] = (
    "I couldn't finish that request in the time I have for one turn — "
    "the conversation may be too large or the work may need more steps. "
    "Try breaking the request into smaller pieces."
)


def _finish_error(
    *,
    bus: EventBus,
    ctx: WorkspaceContext,
    scope: AgentTurnScope,
    thread_id: str | None,
    trigger: TurnTrigger,
    correlation_id: str,
    started_at: datetime,
    ended_at: datetime,
    error_code: str,
    error_message: str,
) -> TurnOutcome:
    """Emit ``agent.turn.finished(outcome=error)`` and return the outcome.

    Centralised so every error branch (capability unassigned,
    budget exceeded, future error families) emits the SSE event
    with a consistent payload and the caller surfaces the same
    typed ``error_code``.
    """
    bus.publish(
        AgentTurnFinished(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            actor_user_id=ctx.actor_id,
            correlation_id=correlation_id,
            occurred_at=ended_at,
            scope=scope,
            thread_id=thread_id,
            trigger=trigger,
            finished_at=ended_at,
            outcome="error",
        )
    )
    return TurnOutcome(
        outcome="error",
        chat_message_id=None,
        approval_request_id=None,
        tool_calls_made=0,
        llm_calls_made=0,
        started_at=started_at,
        ended_at=ended_at,
        correlation_id=correlation_id,
        error_code=error_code,
        error_message=error_message,
    )


def _check_wall_clock(clock: Clock, deadline: datetime) -> None:
    """Raise :class:`TimeoutReply` if ``clock.now() >= deadline``.

    Wall-clock cap is checked before each LLM call; the iteration
    cap fires at the for-loop's natural exit so the two budgets are
    independent. A turn that does eight cheap iterations under the
    60s envelope returns ``timeout`` for the iteration cap; a turn
    that does two slow iterations over 60s returns ``timeout`` for
    the wall-clock cap. Both go through the same fallback path.
    """
    if clock.now() >= deadline:
        raise TimeoutReply("wall_clock_timeout")


# Header value the dispatcher echoes onto every tool call so the
# §11 inline-approval channel mapping (``web_owner_sidebar`` /
# ``web_worker_chat`` / ``web_admin_sidebar``) lines up with the
# scope. The mapping is one-to-one in v1; ``task`` reuses the
# manager channel because it's mounted on the manager surface.
_SCOPE_TO_CHANNEL: Final[Mapping[AgentTurnScope, str]] = {
    "manager": "web_owner_sidebar",
    "employee": "web_worker_chat",
    "admin": "web_admin_sidebar",
    "task": "web_owner_sidebar",
}


def _channel_header_value(scope: AgentTurnScope) -> str:
    """Return the ``X-Agent-Channel`` header value for ``scope``."""
    return _SCOPE_TO_CHANNEL[scope]


def _resolve_external_ref(session: Session, *, thread_id: str | None) -> str | None:
    """Return the channel's :attr:`ChatChannel.external_ref`, or ``None``.

    Read once per turn so every audit row + the
    :class:`AgentAttribution` carry the same value. A scheduled
    turn (``thread_id is None``) has no channel and therefore no
    external ref; the audit column is nullable, so the runtime
    leaves the field unset.
    """
    if thread_id is None:
        return None
    channel = session.get(ChatChannel, thread_id)
    if channel is None:
        return None
    return channel.external_ref


# ---------------------------------------------------------------------------
# History assembly
# ---------------------------------------------------------------------------


def _assemble_history(
    session: Session,
    *,
    scope: AgentTurnScope,
    thread_id: str | None,
    user_message: str,
    history_cap: int,
    preferences: PreferenceBundle,
) -> list[LlmChatMessage]:
    """Build the ``[system, …history, user]`` sequence the LLM sees.

    System prompt: a minimal default per scope until cd-4if3 wires
    the prompt-library reader. History: the last N
    :class:`ChatMessage` rows on the thread (oldest first), capped
    at :data:`DEFAULT_HISTORY_CAP`. The newly-arrived user message
    lands as the trailing ``user`` turn.

    A scheduled trigger has no thread → no history rows; the LLM
    sees only the system prompt + the synthetic user message.
    """
    messages: list[LlmChatMessage] = [
        {
            "role": "system",
            "content": (f"{_default_system_prompt(scope)}\n\n{preferences.text}"),
        },
    ]
    if thread_id is not None:
        messages.extend(_load_recent_history(session, thread_id, history_cap))
    messages.append({"role": "user", "content": user_message})
    return messages


def _default_system_prompt(scope: AgentTurnScope) -> str:
    """Minimal per-scope default until cd-4if3 lands.

    The tool-call protocol is documented inline so the model can
    react to it from the prompt alone — the runtime parses the
    closed shape :func:`_parse_tool_call` walks. Once cd-4if3
    surfaces the full prompt-library reader, this default becomes
    the seed copy a workspace admin can override per capability.
    """
    role_label = {
        "manager": "the manager-side chat agent for crew.day",
        "employee": "the worker-side chat agent for crew.day",
        "admin": "the deployment-admin chat agent for crew.day",
        "task": "the task chat agent for crew.day",
    }[scope]
    return (
        f"You are {role_label}. You act on behalf of the user; you "
        "never carry permissions beyond theirs. Reply in plain text, "
        "or call a tool by emitting exactly one block of the form "
        '`<tool_call name="…" input="…"/>` where ``input`` is a JSON '
        "object. After a tool call, you will see the result on the "
        "next turn and can either reply or call another tool."
    )


def _load_recent_history(
    session: Session, thread_id: str, history_cap: int
) -> list[LlmChatMessage]:
    """Return the last ``history_cap`` rows of the thread, oldest first.

    The order matters: the LLM consumes the messages chronologically
    so a fresh ``user`` turn at the end is visibly the most recent
    one. The DB query orders ``created_at DESC`` for the LIMIT to
    pick the most recent N; we then reverse so the in-memory list
    is oldest-first.

    Each row's ``author_label`` discriminator drives the role on
    the wire: the agent's prior turns become ``assistant``;
    everything else (humans, gateway-inbound) becomes ``user``.
    """
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.channel_id == thread_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(history_cap)
    )
    rows = list(session.scalars(stmt).all())
    rows.reverse()
    return [_chat_message_to_llm(row) for row in rows]


def _chat_message_to_llm(row: ChatMessage) -> LlmChatMessage:
    """Map a :class:`ChatMessage` row onto the LLM's role-tagged shape."""
    role: Literal["user", "assistant"] = (
        "assistant" if row.author_label == "agent" else "user"
    )
    return {"role": role, "content": row.body_md}


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def _call_llm(
    *,
    llm_client: LLMClient,
    session: Session,
    ctx: WorkspaceContext,
    model_pick: ModelPick,
    history: list[LlmChatMessage],
    capability: str,
    correlation_id: str,
    attempt: int,
    attribution: AgentAttribution,
    pricing: PricingTable,
    clock: Clock,
) -> LLMResponse:
    """Dispatch one LLM call and record the usage row.

    Wraps :meth:`LLMClient.chat` with the post-flight
    :func:`app.domain.llm.usage_recorder.record` write so every
    call lands one ``llm_usage`` row attributed to the delegating
    user (per §11 "Agent audit trail" — ``actor_user_id``,
    ``token_id``, ``agent_label`` populated). The clock seam is
    threaded through so deterministic tests can pin the latency
    column without monkey-patching the OS clock.

    Latency is computed from the runtime's clock, not from the
    adapter's internal stopwatch — the adapter has its own
    observability path; we want an end-to-end measurement that
    survives a future client-side retry layer.
    """
    started = clock.now()
    response = llm_client.chat(
        model_id=model_pick.api_model_id,
        messages=history,
        max_tokens=model_pick.max_tokens or _PROJECTED_COMPLETION_TOKENS,
        temperature=model_pick.temperature
        if model_pick.temperature is not None
        else 0.0,
    )
    elapsed_ms = int((clock.now() - started).total_seconds() * 1000)
    cost_cents = estimate_cost_cents(
        prompt_tokens=response.usage.prompt_tokens,
        max_output_tokens=response.usage.completion_tokens,
        api_model_id=model_pick.api_model_id,
        pricing=pricing,
        workspace_id=ctx.workspace_id,
    )
    record(
        session,
        ctx,
        capability=capability,
        model_pick=model_pick,
        fallback_attempts=0,
        correlation_id=correlation_id,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        cost_cents=cost_cents,
        latency_ms=elapsed_ms,
        status="ok",
        finish_reason=response.finish_reason,
        attribution=attribution,
        attempt=attempt,
        clock=clock,
    )
    return response


# ---------------------------------------------------------------------------
# Tool-call parsing (text protocol; cd-um36 upgrades to native)
# ---------------------------------------------------------------------------


# The closed shape the runtime accepts for an LLM-emitted tool call.
# Kept deliberately strict: a single ``<tool_call …/>`` element with
# ``name`` and ``input`` attributes, ``input`` parseable as JSON.
# Fuzzy matches (free-text "let me call X for you", JSON without
# the wrapper, multiple calls) are treated as plain text and the
# runtime replies that text to the user — the user always sees a
# turn close, never a "the model said something I couldn't parse".
_TOOL_CALL_RE: Final[re.Pattern[str]] = re.compile(
    r'<tool_call\s+name="(?P<name>[a-zA-Z0-9_.\-]+)"\s+input=(?P<quote>[\'"])'
    r"(?P<input>.*?)(?P=quote)\s*/>",
    re.DOTALL,
)


def _parse_tool_call(text: str) -> ToolCall | None:
    """Return a :class:`ToolCall` if ``text`` is exactly one tool block.

    Rules, in order:

    * Plain prose with no ``<tool_call …/>`` block → ``None``; the
      runtime treats it as a reply.
    * **More than one** ``<tool_call …/>`` block → ``None``. The
      runtime would otherwise silently dispatch the first one and
      drop the rest, which loses observable intent. Falling back to
      plain text surfaces the raw response in the chat thread so the
      operator notices the model misbehaving (and the turn still
      closes cleanly).
    * A malformed tool block (unparseable JSON in ``input``, missing
      ``/>``, non-object root) → ``None`` — the runtime then replies
      the raw text, which is the least-surprising fallback when a
      model is having a bad day.
    * Text that wraps a single well-formed tool block (``"Sure, let
      me check: <tool_call …/>"``) → dispatch the call. The LLM has
      committed to a tool invocation; the surrounding chatter is the
      model's narration and is not lost — it is still in the
      assistant turn we appended to history. A future native
      tool-use port (cd-um36) will surface this distinction
      structurally.
    """
    matches = list(_TOOL_CALL_RE.finditer(text or ""))
    if not matches:
        return None
    if len(matches) > 1:
        # The runtime dispatches one tool call per LLM turn; a model
        # that emitted multiple is asking the runtime to invent a
        # serialisation order. We refuse explicitly so the
        # behaviour is loud-rather-than-silent: the raw text lands
        # as a reply, and an operator reading the chat sees both
        # blocks instead of a phantom dispatch of one.
        _log.debug(
            "agent.runtime.tool_call_multiple",
            extra={"matches": len(matches)},
        )
        return None
    match = matches[0]
    raw_input = match.group("input")
    # The wrapper escapes JSON's quotes by alternating between '"'
    # and "'" wrappers (``input='{"x":1}'`` vs
    # ``input="{'x':1}"``); JSON itself accepts only double quotes
    # so a single-quote wrapper carries inner doubles. We accept
    # either by walking ``json.loads`` directly — a payload that
    # uses single-quote string keys is invalid JSON and falls
    # through to ``None`` (the model will see no tool dispatch and
    # the next turn is a plain reply).
    try:
        parsed = json.loads(raw_input)
    except json.JSONDecodeError:
        _log.debug(
            "agent.runtime.tool_call_unparseable",
            extra={"raw_input_len": len(raw_input)},
        )
        return None
    if not isinstance(parsed, dict):
        return None
    return ToolCall(
        id=new_ulid(),
        name=match.group("name"),
        input=parsed,
    )


def _append_tool_result_to_history(
    history: list[LlmChatMessage],
    call: ToolCall,
    result: ToolResult,
) -> list[LlmChatMessage]:
    """Return a new history with the tool's result appended.

    The result lands as an ``assistant`` turn so the LLM sees its
    own prior call followed by the system-rendered outcome, then
    decides whether to call another tool or reply. We render the
    result as compact JSON so a forbidden read (``status_code=403``,
    empty body) and a successful read (``200``, populated body) are
    distinguishable to the model without inventing a second
    convention.
    """
    rendered = json.dumps(
        {
            "tool_call_id": result.call_id,
            "name": call.name,
            "status": result.status_code,
            "body": result.body,
        },
        default=str,
    )
    return [*history, {"role": "assistant", "content": rendered}]


# ---------------------------------------------------------------------------
# Side-effect writers — chat reply, audit, approval row
# ---------------------------------------------------------------------------


def _write_chat_reply(
    session: Session,
    *,
    ctx: WorkspaceContext,
    thread_id: str | None,
    body_md: str,
    clock: Clock,
) -> str | None:
    """Persist the agent's reply and return the row id.

    Scheduled turns (``thread_id is None``) write nothing — there is
    no chat surface to land the reply on. The caller still emits
    ``agent.turn.finished`` so the SSE shape stays uniform; the
    /admin/usage feed is the only observable surface for a
    scheduled turn's outcome.

    The row carries ``author_label="agent"`` and
    ``author_user_id=ctx.actor_id`` per §23 ``chat_message``
    "Authored" shape — the agent's reply is attributable to the
    delegating user the same way every other write is.
    """
    if thread_id is None:
        return None
    row_id = new_ulid(clock=clock)
    row = ChatMessage(
        id=row_id,
        workspace_id=ctx.workspace_id,
        channel_id=thread_id,
        author_user_id=ctx.actor_id,
        author_label="agent",
        body_md=body_md,
        attachments_json=[],
        dispatched_to_agent_at=None,
        created_at=clock.now(),
    )
    session.add(row)
    session.flush()
    return row_id


def _audit_tool_call(
    session: Session,
    *,
    ctx: WorkspaceContext,
    tool_call: ToolCall,
    result: ToolResult,
    token_id: str,
    agent_label: str,
    agent_conversation_ref: str | None,
    correlation_id: str,
    clock: Clock,
) -> None:
    """Write one ``audit_log`` row for an executed mutating tool call.

    Per §11 "Agent audit trail":

    * ``actor_kind = "user"`` (the delegating user)
    * ``actor_id = ctx.actor_id`` — never the agent token row's id.
    * ``token_id`` — the delegated token used.
    * ``agent_label`` — denormalised for display.
    * ``agent_conversation_ref`` — opaque ref to the chat thread.
    * ``correlation_id`` — pinned for the turn so /admin/usage
      can cluster the chain.

    The :func:`~app.audit.write_audit` writer carries the tenant
    fields verbatim off ``ctx``, but it does NOT yet accept the
    ``token_id`` / ``agent_label`` / ``agent_conversation_ref``
    columns directly — those denormalised columns land on the
    audit_log row via the diff payload until the cd-wjpl follow-up
    promotes them to top-level columns. Carrying them inside the
    diff today is structurally identical: the diff is JSON, the
    /admin/usage Agent Activity view (cd-wjpl) reads them out.
    The ``correlation_id`` is the canonical one — written into the
    diff so a /admin/audit query can cluster on it without
    re-deriving from the audit row's own correlation column (which
    is :attr:`WorkspaceContext.audit_correlation_id`, a different
    cursor).
    """
    write_audit(
        session,
        ctx,
        entity_kind="agent_tool_call",
        entity_id=tool_call.id,
        action=f"agent.tool.{tool_call.name}",
        diff={
            "tool_name": tool_call.name,
            "tool_call_id": tool_call.id,
            "tool_input": dict(tool_call.input),
            "status_code": result.status_code,
            "token_id": token_id,
            "agent_label": agent_label,
            "agent_conversation_ref": agent_conversation_ref,
            "agent_correlation_id": correlation_id,
        },
        clock=clock,
    )


def _write_approval_request(
    session: Session,
    *,
    ctx: WorkspaceContext,
    tool_call: ToolCall,
    decision: GateDecision,
    correlation_id: str,
    requester_actor_id: str,
    inline_channel: str,
    for_user_id: str | None,
    resolved_user_mode: str | None,
    clock: Clock,
) -> tuple[str, datetime]:
    """Persist a ``pending`` approval row and return ``(id, expires_at)``.

    The cd-9ghv consumer picks up rows in this state and walks them
    through the full HITL pipeline (notification fanout, decision
    capture, executed transition, TTL expiry). The runtime's
    responsibility ends at writing the row + publishing the inline-
    channel SSE event; the turn pauses with ``outcome=action``.

    ``action_json`` carries the resolved tool call, the gate
    decision metadata, and the turn's correlation id — the consumer
    needs all three to render the inline card and to re-dispatch
    the call after approval. The §11 column-promoted fields land as
    first-class columns:

    * ``expires_at`` — :data:`APPROVAL_REQUEST_TTL` from now
      (7 days). The cd-9ghv ``approval_ttl`` worker auto-flips a
      row past its expiry to ``status='timed_out'``.
    * ``inline_channel`` — the ``X-Agent-Channel`` value derived
      from the turn's :class:`AgentTurnScope`. Selects the SPA
      surface that renders the inline card.
    * ``for_user_id`` — the delegating user. NULL for desk-only
      approvals (no inline surface, the row only shows on the
      /approvals desk). The SSE filter on
      :class:`AgentActionPending` rides this column so the card
      lands only on the right user's tabs.
    * ``resolved_user_mode`` — snapshot of the delegating user's
      per-user agent approval mode at the instant the row was
      minted. NULL when the column has not yet been threaded down
      from the turn caller (the per-user mode column itself is a
      future cd-cm5 follow-up; the column is ready for the
      promotion).

    The :class:`ApprovalRequest` row is written under
    :func:`tenant_agnostic` because the new row's ``workspace_id``
    matches ``ctx.workspace_id`` already; the tenant filter is
    there for defensive *reads* on workspace-scoped tables, not
    for inserts whose row already carries the right column. The
    pattern matches the workspace-create call sites in
    ``app.auth.signup``.
    """
    row_id = new_ulid(clock=clock)
    created_at = clock.now()
    expires_at = created_at + APPROVAL_REQUEST_TTL
    row = ApprovalRequest(
        id=row_id,
        workspace_id=ctx.workspace_id,
        requester_actor_id=requester_actor_id,
        action_json={
            "tool_name": tool_call.name,
            "tool_call_id": tool_call.id,
            "tool_input": dict(tool_call.input),
            "card_summary": decision.card_summary,
            "card_risk": decision.card_risk,
            "pre_approval_source": decision.pre_approval_source,
            "agent_correlation_id": correlation_id,
        },
        status="pending",
        decided_by=None,
        decided_at=None,
        rationale_md=None,
        decision_note_md=None,
        result_json=None,
        expires_at=expires_at,
        inline_channel=inline_channel,
        for_user_id=for_user_id,
        resolved_user_mode=resolved_user_mode,
        created_at=created_at,
    )
    with tenant_agnostic():
        session.add(row)
        session.flush()
    return row_id, expires_at
