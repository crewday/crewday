"""Tests for :mod:`app.domain.agent.relay_requests` (cd-rfk94)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.adapters.db.llm.repositories import SqlAlchemyAgentRelayRequestRepository
from app.domain.agent.relay_requests import (
    AgentRelayRequestCreate,
    RelayDuplicateActive,
    close_relay,
    create_relay,
    list_open_relays_for_target,
    mark_relay_delivered,
    mark_relay_responded,
    relay_request_fingerprint,
)
from app.tenancy.current import reset_current, set_current
from tests.domain.agent.conftest import build_context, seed_user, seed_workspace

pytestmark = pytest.mark.integration


def _request(*, requester_id: str, target_id: str) -> AgentRelayRequestCreate:
    return AgentRelayRequestCreate(
        requester_user_id=requester_id,
        target_user_id=target_id,
        requester_display_label="Vincent",
        target_display_label="Maria",
        requester_scope="manager",
        requester_thread_ref=f"agent:manager:{requester_id}",
        requester_message_ref="msg-requester",
        target_scope="employee",
        request_summary="Sunday availability",
    )


def test_create_relay_persists_summary_and_open_lookup(
    db_session: Session, clock
) -> None:
    workspace = seed_workspace(db_session)
    requester_id = seed_user(db_session)
    target_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=requester_id)
    repo = SqlAlchemyAgentRelayRequestRepository(db_session)

    token = set_current(ctx)
    try:
        relay = create_relay(
            repo,
            ctx,
            _request(requester_id=requester_id, target_id=target_id),
            clock=clock,
        )

        assert relay.workspace_id == workspace.id
        assert relay.status == "open"
        assert relay.request_summary == "Sunday availability"
        assert relay.request_fingerprint == relay_request_fingerprint(
            " sunday   availability "
        )
        assert relay.requester_thread_ref == f"agent:manager:{requester_id}"
        assert relay.target_thread_ref is None

        open_relays = list_open_relays_for_target(repo, ctx, target_user_id=target_id)
        assert [row.id for row in open_relays] == [relay.id]
    finally:
        reset_current(token)


def test_mark_delivered_preserves_target_thread_correlation(
    db_session: Session, clock
) -> None:
    workspace = seed_workspace(db_session)
    requester_id = seed_user(db_session)
    target_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=requester_id)
    repo = SqlAlchemyAgentRelayRequestRepository(db_session)

    token = set_current(ctx)
    try:
        relay = create_relay(
            repo,
            ctx,
            _request(requester_id=requester_id, target_id=target_id),
            clock=clock,
        )
        clock.advance(timedelta(minutes=1))

        delivered = mark_relay_delivered(
            repo,
            ctx,
            relay_id=relay.id,
            target_thread_ref=f"agent:employee:{target_id}",
            target_message_ref="msg-target",
            clock=clock,
        )

        assert delivered.status == "open"
        assert delivered.target_thread_ref == f"agent:employee:{target_id}"
        assert delivered.target_message_ref == "msg-target"
        assert delivered.delivered_at == clock.now()
        assert delivered.requester_thread_ref == relay.requester_thread_ref
    finally:
        reset_current(token)


def test_mark_responded_closes_open_relay(db_session: Session, clock) -> None:
    workspace = seed_workspace(db_session)
    requester_id = seed_user(db_session)
    target_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=requester_id)
    repo = SqlAlchemyAgentRelayRequestRepository(db_session)

    token = set_current(ctx)
    try:
        relay = create_relay(
            repo,
            ctx,
            _request(requester_id=requester_id, target_id=target_id),
            clock=clock,
        )
        clock.advance(timedelta(minutes=3))

        responded = mark_relay_responded(
            repo,
            ctx,
            relay_id=relay.id,
            response_summary="Maria responded that she can work 2-6pm Sunday.",
            clock=clock,
        )

        assert responded.status == "answered"
        assert responded.response_summary == (
            "Maria responded that she can work 2-6pm Sunday."
        )
        assert responded.responded_at == clock.now()
        assert responded.closed_at == clock.now()
        assert list_open_relays_for_target(repo, ctx, target_user_id=target_id) == []
    finally:
        reset_current(token)


def test_duplicate_active_relay_is_prevented_until_closed(
    db_session: Session, clock
) -> None:
    workspace = seed_workspace(db_session)
    requester_id = seed_user(db_session)
    target_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=requester_id)
    repo = SqlAlchemyAgentRelayRequestRepository(db_session)
    request = _request(requester_id=requester_id, target_id=target_id)

    token = set_current(ctx)
    try:
        first = create_relay(repo, ctx, request, clock=clock)

        with pytest.raises(RelayDuplicateActive) as exc_info:
            create_relay(repo, ctx, request, clock=clock)
        assert exc_info.value.extra["agent_relay_request_id"] == first.id

        close_relay(repo, ctx, relay_id=first.id, status="cancelled", clock=clock)
        second = create_relay(repo, ctx, request, clock=clock)
        assert second.id != first.id
    finally:
        reset_current(token)


def test_duplicate_active_unique_violation_translates_to_conflict(
    db_session: Session, clock
) -> None:
    workspace = seed_workspace(db_session)
    requester_id = seed_user(db_session)
    target_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=requester_id)
    repo = SqlAlchemyAgentRelayRequestRepository(db_session)

    token = set_current(ctx)
    try:
        first = create_relay(
            repo,
            ctx,
            _request(requester_id=requester_id, target_id=target_id),
            clock=clock,
        )
        with pytest.raises(RelayDuplicateActive) as exc_info:
            repo.insert(replace(first, id="01HWA00000000000000RACE01"))

        assert exc_info.value.extra["agent_relay_request_id"] == first.id
        assert list_open_relays_for_target(repo, ctx, target_user_id=target_id) == [
            first
        ]
    finally:
        reset_current(token)


def test_workspace_isolation_for_open_target_lookup(db_session: Session, clock) -> None:
    workspace_a = seed_workspace(db_session, slug="relay-domain-a")
    workspace_b = seed_workspace(db_session, slug="relay-domain-b")
    requester_a = seed_user(db_session)
    target_a = seed_user(db_session)
    requester_b = seed_user(db_session)
    target_b = seed_user(db_session)
    repo = SqlAlchemyAgentRelayRequestRepository(db_session)
    ctx_a = build_context(workspace_a.id, slug=workspace_a.slug, actor_id=requester_a)
    ctx_b = build_context(workspace_b.id, slug=workspace_b.slug, actor_id=requester_b)

    token = set_current(ctx_a)
    try:
        relay_a = create_relay(
            repo,
            ctx_a,
            _request(requester_id=requester_a, target_id=target_a),
            clock=clock,
        )
    finally:
        reset_current(token)

    token = set_current(ctx_b)
    try:
        create_relay(
            repo,
            ctx_b,
            _request(requester_id=requester_b, target_id=target_b),
            clock=clock,
        )
    finally:
        reset_current(token)

    token = set_current(ctx_a)
    try:
        assert [
            row.id
            for row in list_open_relays_for_target(repo, ctx_a, target_user_id=target_a)
        ] == [relay_a.id]
        assert list_open_relays_for_target(repo, ctx_a, target_user_id=target_b) == []
    finally:
        reset_current(token)
