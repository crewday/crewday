"""Domain service for §11 agent-mediated relay correlation rows."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol

from app.domain.errors import Conflict, NotFound, Validation
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "ACTIVE_RELAY_STATUS",
    "TERMINAL_RELAY_STATUSES",
    "AgentRelayRequestCreate",
    "AgentRelayRequestRepository",
    "AgentRelayRequestView",
    "RelayDuplicateActive",
    "RelayNotFound",
    "RelayStatus",
    "close_relay",
    "create_relay",
    "list_open_relays_for_target",
    "mark_relay_delivered",
    "mark_relay_responded",
    "relay_request_fingerprint",
]


RelayStatus = Literal["open", "answered", "expired", "cancelled", "failed"]

ACTIVE_RELAY_STATUS: Final[RelayStatus] = "open"
TERMINAL_RELAY_STATUSES: Final[tuple[RelayStatus, ...]] = (
    "answered",
    "expired",
    "cancelled",
    "failed",
)

_VISIBLE_TEXT_LIMIT: Final[int] = 8 * 1024
_SPACE_RE = re.compile(r"\s+")


class RelayDuplicateActive(Conflict):
    """An unresolved relay already covers this requester/target/question."""

    title = "Active relay already exists"
    type_name = "agent_relay_duplicate_active"


class RelayNotFound(NotFound):
    """The relay does not exist in the caller's workspace."""

    title = "Relay not found"
    type_name = "agent_relay_not_found"


@dataclass(frozen=True, slots=True)
class AgentRelayRequestCreate:
    requester_user_id: str
    target_user_id: str
    requester_display_label: str
    target_display_label: str
    requester_scope: str
    requester_thread_ref: str
    requester_message_ref: str | None
    target_scope: str
    request_summary: str
    target_thread_ref: str | None = None
    target_message_ref: str | None = None


@dataclass(frozen=True, slots=True)
class AgentRelayRequestView:
    id: str
    workspace_id: str
    requester_user_id: str | None
    target_user_id: str | None
    requester_display_label: str
    target_display_label: str
    requester_scope: str
    requester_thread_ref: str
    requester_message_ref: str | None
    target_scope: str
    target_thread_ref: str | None
    target_message_ref: str | None
    status: RelayStatus
    request_summary: str
    request_fingerprint: str
    response_summary: str | None
    created_at: datetime
    delivered_at: datetime | None
    responded_at: datetime | None
    closed_at: datetime | None


class AgentRelayRequestRepository(Protocol):
    def insert(self, row: AgentRelayRequestView) -> AgentRelayRequestView: ...

    def get_open_duplicate(
        self,
        *,
        workspace_id: str,
        requester_user_id: str,
        target_user_id: str,
        request_fingerprint: str,
    ) -> AgentRelayRequestView | None: ...

    def list_open_for_target(
        self, *, workspace_id: str, target_user_id: str
    ) -> Sequence[AgentRelayRequestView]: ...

    def get(
        self, *, workspace_id: str, relay_id: str
    ) -> AgentRelayRequestView | None: ...

    def update_delivery(
        self,
        *,
        workspace_id: str,
        relay_id: str,
        target_thread_ref: str,
        target_message_ref: str | None,
        delivered_at: datetime,
    ) -> AgentRelayRequestView: ...

    def update_response(
        self,
        *,
        workspace_id: str,
        relay_id: str,
        response_summary: str,
        responded_at: datetime,
    ) -> AgentRelayRequestView: ...

    def update_closed(
        self,
        *,
        workspace_id: str,
        relay_id: str,
        status: RelayStatus,
        closed_at: datetime,
    ) -> AgentRelayRequestView: ...


def relay_request_fingerprint(summary: str) -> str:
    """Return the duplicate-detection key for a relay-safe summary."""
    normalized = _SPACE_RE.sub(" ", summary.casefold()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def create_relay(
    repo: AgentRelayRequestRepository,
    ctx: WorkspaceContext,
    request: AgentRelayRequestCreate,
    *,
    clock: Clock | None = None,
) -> AgentRelayRequestView:
    """Create an open relay unless the same unresolved ask already exists."""
    _validate_create(request)
    fingerprint = relay_request_fingerprint(request.request_summary)
    duplicate = repo.get_open_duplicate(
        workspace_id=ctx.workspace_id,
        requester_user_id=request.requester_user_id,
        target_user_id=request.target_user_id,
        request_fingerprint=fingerprint,
    )
    if duplicate is not None:
        raise RelayDuplicateActive(
            "an open relay already exists for this requester, target, and question",
            extra={"agent_relay_request_id": duplicate.id},
        )

    now = (clock or SystemClock()).now()
    return repo.insert(
        AgentRelayRequestView(
            id=new_ulid(clock=clock),
            workspace_id=ctx.workspace_id,
            requester_user_id=request.requester_user_id,
            target_user_id=request.target_user_id,
            requester_display_label=request.requester_display_label.strip(),
            target_display_label=request.target_display_label.strip(),
            requester_scope=request.requester_scope.strip(),
            requester_thread_ref=request.requester_thread_ref.strip(),
            requester_message_ref=_clean_optional(request.requester_message_ref),
            target_scope=request.target_scope.strip(),
            target_thread_ref=_clean_optional(request.target_thread_ref),
            target_message_ref=_clean_optional(request.target_message_ref),
            status=ACTIVE_RELAY_STATUS,
            request_summary=request.request_summary.strip(),
            request_fingerprint=fingerprint,
            response_summary=None,
            created_at=now,
            delivered_at=None,
            responded_at=None,
            closed_at=None,
        )
    )


def list_open_relays_for_target(
    repo: AgentRelayRequestRepository, ctx: WorkspaceContext, *, target_user_id: str
) -> Sequence[AgentRelayRequestView]:
    if not target_user_id.strip():
        raise Validation("target_user_id is required")
    return repo.list_open_for_target(
        workspace_id=ctx.workspace_id,
        target_user_id=target_user_id,
    )


def mark_relay_delivered(
    repo: AgentRelayRequestRepository,
    ctx: WorkspaceContext,
    *,
    relay_id: str,
    target_thread_ref: str,
    target_message_ref: str | None,
    clock: Clock | None = None,
) -> AgentRelayRequestView:
    if not target_thread_ref.strip():
        raise Validation("target_thread_ref is required")
    _require_open(repo, ctx, relay_id)
    return repo.update_delivery(
        workspace_id=ctx.workspace_id,
        relay_id=relay_id,
        target_thread_ref=target_thread_ref.strip(),
        target_message_ref=_clean_optional(target_message_ref),
        delivered_at=(clock or SystemClock()).now(),
    )


def mark_relay_responded(
    repo: AgentRelayRequestRepository,
    ctx: WorkspaceContext,
    *,
    relay_id: str,
    response_summary: str,
    clock: Clock | None = None,
) -> AgentRelayRequestView:
    if not response_summary.strip():
        raise Validation("response_summary is required")
    if len(response_summary) > _VISIBLE_TEXT_LIMIT:
        raise Validation("response_summary is too long")
    _require_open(repo, ctx, relay_id)
    return repo.update_response(
        workspace_id=ctx.workspace_id,
        relay_id=relay_id,
        response_summary=response_summary.strip(),
        responded_at=(clock or SystemClock()).now(),
    )


def close_relay(
    repo: AgentRelayRequestRepository,
    ctx: WorkspaceContext,
    *,
    relay_id: str,
    status: Literal["expired", "cancelled", "failed"],
    clock: Clock | None = None,
) -> AgentRelayRequestView:
    _require_open(repo, ctx, relay_id)
    return repo.update_closed(
        workspace_id=ctx.workspace_id,
        relay_id=relay_id,
        status=status,
        closed_at=(clock or SystemClock()).now(),
    )


def _require_open(
    repo: AgentRelayRequestRepository, ctx: WorkspaceContext, relay_id: str
) -> AgentRelayRequestView:
    if not relay_id.strip():
        raise Validation("relay_id is required")
    row = repo.get(workspace_id=ctx.workspace_id, relay_id=relay_id)
    if row is None:
        raise RelayNotFound(f"relay {relay_id!r} not found")
    if row.status != ACTIVE_RELAY_STATUS:
        raise Conflict(
            f"relay {relay_id!r} is in state {row.status!r}",
            extra={"agent_relay_request_id": row.id, "status": row.status},
        )
    return row


def _validate_create(request: AgentRelayRequestCreate) -> None:
    required = {
        "requester_user_id": request.requester_user_id,
        "target_user_id": request.target_user_id,
        "requester_display_label": request.requester_display_label,
        "target_display_label": request.target_display_label,
        "requester_scope": request.requester_scope,
        "requester_thread_ref": request.requester_thread_ref,
        "target_scope": request.target_scope,
        "request_summary": request.request_summary,
    }
    missing = [field for field, value in required.items() if not value.strip()]
    if missing:
        raise Validation(
            "relay request is missing required fields",
            errors=[
                {"loc": [field], "msg": "field is required", "type": "missing"}
                for field in missing
            ],
        )
    if request.requester_user_id == request.target_user_id:
        raise Validation("requester and target must be different users")
    for field, value in (("request_summary", request.request_summary),):
        if len(value) > _VISIBLE_TEXT_LIMIT:
            raise Validation(f"{field} is too long")


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
