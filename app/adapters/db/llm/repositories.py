"""SQLAlchemy repositories for LLM / agent persistence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import AgentRelayRequest
from app.domain.agent.relay_requests import (
    AgentRelayRequestRepository,
    AgentRelayRequestView,
    RelayDuplicateActive,
    RelayNotFound,
    RelayStatus,
)
from app.util.clock import aware_utc as _as_utc

__all__ = ["SqlAlchemyAgentRelayRequestRepository"]

_ACTIVE_RELAY_UNIQUE_HINTS: tuple[str, ...] = (
    "agent_relay_request.workspace_id",
    "agent_relay_request.requester_user_id",
    "agent_relay_request.target_user_id",
    "agent_relay_request.request_fingerprint",
)


def _to_relay_view(row: AgentRelayRequest) -> AgentRelayRequestView:
    status = _relay_status(row.status)
    return AgentRelayRequestView(
        id=row.id,
        workspace_id=row.workspace_id,
        requester_user_id=row.requester_user_id,
        target_user_id=row.target_user_id,
        requester_display_label=row.requester_display_label,
        target_display_label=row.target_display_label,
        requester_scope=row.requester_scope,
        requester_thread_ref=row.requester_thread_ref,
        requester_message_ref=row.requester_message_ref,
        target_scope=row.target_scope,
        target_thread_ref=row.target_thread_ref,
        target_message_ref=row.target_message_ref,
        status=status,
        request_summary=row.request_summary,
        request_fingerprint=row.request_fingerprint,
        response_summary=row.response_summary,
        created_at=_as_utc(row.created_at),
        delivered_at=_as_utc(row.delivered_at) if row.delivered_at else None,
        responded_at=_as_utc(row.responded_at) if row.responded_at else None,
        closed_at=_as_utc(row.closed_at) if row.closed_at else None,
    )


def _relay_status(value: str) -> RelayStatus:
    if value == "open":
        return "open"
    if value == "answered":
        return "answered"
    if value == "expired":
        return "expired"
    if value == "cancelled":
        return "cancelled"
    if value == "failed":
        return "failed"
    raise ValueError(f"unknown relay status {value!r}")


def _is_active_relay_unique_violation(exc: IntegrityError) -> bool:
    message = str(exc.orig)
    return "uq_agent_relay_request_active_question" in message or all(
        hint in message for hint in _ACTIVE_RELAY_UNIQUE_HINTS
    )


class SqlAlchemyAgentRelayRequestRepository(AgentRelayRequestRepository):
    """SA-backed relay correlation repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def insert(self, row: AgentRelayRequestView) -> AgentRelayRequestView:
        db_row = AgentRelayRequest(
            id=row.id,
            workspace_id=row.workspace_id,
            requester_user_id=row.requester_user_id,
            target_user_id=row.target_user_id,
            requester_display_label=row.requester_display_label,
            target_display_label=row.target_display_label,
            requester_scope=row.requester_scope,
            requester_thread_ref=row.requester_thread_ref,
            requester_message_ref=row.requester_message_ref,
            target_scope=row.target_scope,
            target_thread_ref=row.target_thread_ref,
            target_message_ref=row.target_message_ref,
            status=row.status,
            request_summary=row.request_summary,
            request_fingerprint=row.request_fingerprint,
            response_summary=row.response_summary,
            created_at=row.created_at,
            delivered_at=row.delivered_at,
            responded_at=row.responded_at,
            closed_at=row.closed_at,
        )
        nested = self._session.begin_nested()
        self._session.add(db_row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            nested.rollback()
            if _is_active_relay_unique_violation(exc):
                duplicate = self.get_open_duplicate(
                    workspace_id=row.workspace_id,
                    requester_user_id=row.requester_user_id or "",
                    target_user_id=row.target_user_id or "",
                    request_fingerprint=row.request_fingerprint,
                )
                raise RelayDuplicateActive(
                    "an open relay already exists for this requester, target, "
                    "and question",
                    extra={
                        "agent_relay_request_id": duplicate.id
                        if duplicate is not None
                        else row.id
                    },
                ) from exc
            raise
        nested.commit()
        return _to_relay_view(db_row)

    def get_open_duplicate(
        self,
        *,
        workspace_id: str,
        requester_user_id: str,
        target_user_id: str,
        request_fingerprint: str,
    ) -> AgentRelayRequestView | None:
        stmt = select(AgentRelayRequest).where(
            AgentRelayRequest.workspace_id == workspace_id,
            AgentRelayRequest.requester_user_id == requester_user_id,
            AgentRelayRequest.target_user_id == target_user_id,
            AgentRelayRequest.request_fingerprint == request_fingerprint,
            AgentRelayRequest.status == "open",
        )
        row = self._session.scalars(stmt).first()
        return _to_relay_view(row) if row is not None else None

    def list_open_for_target(
        self, *, workspace_id: str, target_user_id: str
    ) -> Sequence[AgentRelayRequestView]:
        stmt = (
            select(AgentRelayRequest)
            .where(
                AgentRelayRequest.workspace_id == workspace_id,
                AgentRelayRequest.target_user_id == target_user_id,
                AgentRelayRequest.status == "open",
            )
            .order_by(AgentRelayRequest.created_at.asc(), AgentRelayRequest.id.asc())
        )
        return [_to_relay_view(row) for row in self._session.scalars(stmt).all()]

    def get(self, *, workspace_id: str, relay_id: str) -> AgentRelayRequestView | None:
        row = self._load_optional(workspace_id=workspace_id, relay_id=relay_id)
        return _to_relay_view(row) if row is not None else None

    def update_delivery(
        self,
        *,
        workspace_id: str,
        relay_id: str,
        target_thread_ref: str,
        target_message_ref: str | None,
        delivered_at: datetime,
    ) -> AgentRelayRequestView:
        row = self._load_required(workspace_id=workspace_id, relay_id=relay_id)
        row.target_thread_ref = target_thread_ref
        row.target_message_ref = target_message_ref
        row.delivered_at = delivered_at
        self._session.flush()
        return _to_relay_view(row)

    def update_response(
        self,
        *,
        workspace_id: str,
        relay_id: str,
        response_summary: str,
        responded_at: datetime,
    ) -> AgentRelayRequestView:
        row = self._load_required(workspace_id=workspace_id, relay_id=relay_id)
        row.status = "answered"
        row.response_summary = response_summary
        row.responded_at = responded_at
        row.closed_at = responded_at
        self._session.flush()
        return _to_relay_view(row)

    def update_closed(
        self,
        *,
        workspace_id: str,
        relay_id: str,
        status: RelayStatus,
        closed_at: datetime,
    ) -> AgentRelayRequestView:
        row = self._load_required(workspace_id=workspace_id, relay_id=relay_id)
        row.status = status
        row.closed_at = closed_at
        self._session.flush()
        return _to_relay_view(row)

    def _load_optional(
        self, *, workspace_id: str, relay_id: str
    ) -> AgentRelayRequest | None:
        stmt = select(AgentRelayRequest).where(
            AgentRelayRequest.workspace_id == workspace_id,
            AgentRelayRequest.id == relay_id,
        )
        return self._session.scalars(stmt).first()

    def _load_required(self, *, workspace_id: str, relay_id: str) -> AgentRelayRequest:
        row = self._load_optional(workspace_id=workspace_id, relay_id=relay_id)
        if row is None:
            raise RelayNotFound(f"relay {relay_id!r} not found")
        return row
