"""Worker-side staff chat assistant facade.

Spec: ``docs/specs/11-llm-and-agents.md`` "Worker-side agent",
"Staff chat assistant".
"""

from __future__ import annotations

from collections.abc import Container, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy.orm import Session

from app.adapters.llm.ports import LLMClient
from app.domain.agent.runtime import (
    DEFAULT_HISTORY_CAP,
    DelegatedToken,
    GateDecision,
    TokenFactory,
    ToolCall,
    ToolDispatcher,
    ToolResult,
    TurnOutcome,
    TurnTrigger,
    run_turn,
)
from app.domain.llm.budget import PricingTable
from app.events.bus import EventBus
from app.tenancy import WorkspaceContext
from app.util.clock import Clock

__all__ = [
    "STAFF_CHAT_AGENT_LABEL",
    "STAFF_CHAT_CAPABILITY",
    "STAFF_CHAT_CHANNEL",
    "STAFF_CHAT_HISTORY_CAP",
    "STAFF_CHAT_SCOPE",
    "STAFF_CHAT_TOOLS",
    "StaffChatTool",
    "is_staff_chat_tool",
    "is_voice_input_enabled",
    "run_staff_chat_turn",
    "staff_chat_tool_names",
    "suggest_staff_chat_tool",
]


STAFF_CHAT_CAPABILITY: Final[str] = "chat.employee"
STAFF_CHAT_AGENT_LABEL: Final[str] = "worker-chat-agent"
STAFF_CHAT_SCOPE: Final[Literal["employee"]] = "employee"
STAFF_CHAT_CHANNEL: Final[str] = "web_worker_chat"

# Spec 11's recent-window floor is 20 turns. The runtime's default cap is
# intentionally above that floor until the dedicated compaction worker
# (cd-cn7v) owns summary rows.
STAFF_CHAT_HISTORY_CAP: Final[int] = max(DEFAULT_HISTORY_CAP, 20)


@dataclass(frozen=True, slots=True)
class StaffChatTool:
    """One worker-safe tool exposed to the staff chat assistant."""

    name: str
    method: Literal["GET", "POST", "PATCH"]
    path: str
    mutates: bool
    description: str


STAFF_CHAT_TOOLS: Final[tuple[StaffChatTool, ...]] = (
    StaffChatTool(
        name="get_tasks_today",
        method="GET",
        path="/api/v1/tasks?filter=today&assignee=me",
        mutates=False,
        description="List the calling worker's tasks due today.",
    ),
    StaffChatTool(
        name="mark_task_done",
        method="POST",
        path="/api/v1/tasks/{task_id}/complete",
        mutates=True,
        description="Mark one resolved worker task complete.",
    ),
    StaffChatTool(
        name="report_issue",
        method="POST",
        path="/api/v1/tasks/issues",
        mutates=True,
        description="Report a problem for manager triage.",
    ),
    StaffChatTool(
        name="get_instruction",
        method="GET",
        path="/api/v1/instructions/{id}",
        mutates=False,
        description="Read one instruction visible to the worker.",
    ),
    StaffChatTool(
        name="get_my_bookings",
        method="GET",
        path="/api/v1/stays/bookings?assignee=me",
        mutates=False,
        description="List the worker's visible booking assignments.",
    ),
    StaffChatTool(
        name="amend_booking",
        method="PATCH",
        path="/api/v1/stays/bookings/{id}",
        mutates=True,
        description="Request or apply an allowed change to one worker booking.",
    ),
    StaffChatTool(
        name="decline_booking",
        method="POST",
        path="/api/v1/stays/bookings/{id}/decline",
        mutates=True,
        description="Decline one assigned booking with an optional reason.",
    ),
    StaffChatTool(
        name="propose_booking",
        method="POST",
        path="/api/v1/stays/bookings/proposals",
        mutates=True,
        description="Propose availability for a booking assignment.",
    ),
    StaffChatTool(
        name="get_inventory_low",
        method="GET",
        path="/api/v1/inventory/items?status=low",
        mutates=False,
        description="List low inventory visible to the worker.",
    ),
)

_STAFF_CHAT_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    tool.name for tool in STAFF_CHAT_TOOLS
)


def staff_chat_tool_names() -> frozenset[str]:
    """Return the closed set of worker-safe tool names."""

    return _STAFF_CHAT_TOOL_NAMES


def is_staff_chat_tool(name: str) -> bool:
    """Return whether ``name`` is in the worker chat catalog."""

    return name in _STAFF_CHAT_TOOL_NAMES


def is_voice_input_enabled(
    workspace_settings: Mapping[str, object],
    assigned_capabilities: Container[str],
) -> bool:
    """Return whether worker voice input may be accepted for this workspace."""

    return (
        workspace_settings.get("voice.enabled") is True
        and "voice.transcribe" in assigned_capabilities
    )


def suggest_staff_chat_tool(message: str) -> ToolCall | None:
    """Map obvious staff-chat utterances onto the catalog's safest tool.

    This deterministic hint is deliberately narrow; the normal LLM turn
    still handles open-ended wording through :func:`run_staff_chat_turn`.
    """

    normalized = " ".join(message.lower().split())
    if not normalized:
        return None
    if (
        "what's on my plate" in normalized
        or "whats on my plate" in normalized
        or "what is on my plate" in normalized
        or "tasks today" in normalized
    ):
        return ToolCall(id="staff-chat-suggested", name="get_tasks_today", input={})
    if normalized.startswith("mark ") and (
        " done" in normalized or " complete" in normalized
    ):
        subject = normalized.removeprefix("mark ").replace(" done", "")
        subject = subject.replace(" complete", "").strip()
        return ToolCall(
            id="staff-chat-suggested",
            name="mark_task_done",
            input={"query": subject},
        )
    return None


def run_staff_chat_turn(
    ctx: WorkspaceContext,
    *,
    session: Session,
    thread_id: str,
    user_message: str,
    trigger: TurnTrigger,
    llm_client: LLMClient,
    tool_dispatcher: ToolDispatcher,
    token_factory: TokenFactory,
    pricing: PricingTable | None = None,
    event_bus: EventBus | None = None,
    clock: Clock | None = None,
    max_iterations: int = 8,
    wall_clock_timeout_s: int = 60,
) -> TurnOutcome:
    """Run one worker chat assistant turn with the staff-only catalog."""

    return run_turn(
        ctx,
        session=session,
        scope=STAFF_CHAT_SCOPE,
        thread_id=thread_id,
        user_message=user_message,
        trigger=trigger,
        llm_client=llm_client,
        tool_dispatcher=_StaffChatDispatcher(tool_dispatcher),
        token_factory=token_factory,
        agent_label=STAFF_CHAT_AGENT_LABEL,
        capability=STAFF_CHAT_CAPABILITY,
        pricing=pricing,
        event_bus=event_bus,
        clock=clock,
        max_iterations=max_iterations,
        wall_clock_timeout_s=wall_clock_timeout_s,
        history_cap=STAFF_CHAT_HISTORY_CAP,
    )


@dataclass(slots=True)
class _StaffChatDispatcher:
    """Catalog guard that fail-closes before an out-of-scope tool executes."""

    inner: ToolDispatcher

    def is_gated(self, call: ToolCall) -> GateDecision:
        if not is_staff_chat_tool(call.name):
            return GateDecision(gated=False)
        return self.inner.is_gated(call)

    def dispatch(
        self,
        call: ToolCall,
        *,
        token: DelegatedToken,
        headers: Mapping[str, str],
    ) -> ToolResult:
        if not is_staff_chat_tool(call.name):
            return ToolResult(
                call_id=call.id,
                status_code=403,
                body={"error": "staff_chat_tool_forbidden", "tool": call.name},
                mutated=False,
            )
        return self.inner.dispatch(call, token=token, headers=headers)
