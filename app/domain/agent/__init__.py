"""Agent runtime — embedded chat agents (cd-nyvm).

A turn is the span from a user message landing on an agent endpoint
until the agent produces the next observable outcome — an appended
chat message, a pending approval card, or an error. The runtime
brackets every turn with two SSE events
(:class:`~app.events.types.AgentTurnStarted` /
:class:`~app.events.types.AgentTurnFinished`), routes the LLM call
through the workspace's 30-day budget envelope (§11 "Workspace usage
budget"), dispatches tool calls against the OpenAPI surface using a
delegated token (§03 "Delegated tokens"), and writes one
:class:`~app.adapters.db.audit.models.AuditLog` row per executed
mutation attributed to the **delegating user** (§11 "Agent audit
trail").

Public surface lives in :mod:`app.domain.agent.runtime`:

* :class:`~app.domain.agent.runtime.TurnOutcome` — frozen value
  describing what happened in a turn (outcome, ids, counts, span).
* :func:`~app.domain.agent.runtime.run_turn` — the entry point.
* :class:`~app.domain.agent.runtime.ToolCall` /
  :class:`~app.domain.agent.runtime.ToolResult` /
  :class:`~app.domain.agent.runtime.GateDecision` — dispatch
  envelope shapes the runtime hands to a :class:`ToolDispatcher`.
* :class:`~app.domain.agent.runtime.ToolDispatcher` /
  :class:`~app.domain.agent.runtime.TokenFactory` — Protocol seams
  the runtime depends on. The MVP wires unit tests against fakes;
  the OpenAPI in-process dispatcher is filed as cd-z3b7.

See ``docs/specs/11-llm-and-agents.md`` §"Embedded agents",
§"Agent turn lifecycle", §"Agent audit trail".
"""
