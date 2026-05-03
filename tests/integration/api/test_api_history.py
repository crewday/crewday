"""Integration coverage for ``GET /history`` (cd-wnsr).

The aggregator returns the SPA's ``HistoryPayload`` shape verbatim — a
single object carrying ``{tab, tasks[], expenses[], leaves[],
chats[]}``. These tests drive the full router → DB chain and assert:

* 401 surfaces from :func:`current_workspace_context` for anonymous
  callers.
* 422 surfaces from FastAPI's Pydantic ``Literal`` validation for
  unknown ``tab`` values.
* Each of the four tabs returns the rows the mock filter rule lets
  through:

  - ``tasks`` — caller's ``Occurrence`` rows in ``{completed, skipped}``.
  - ``expenses`` — caller's ``ExpenseClaim`` rows in
    ``{approved, reimbursed, rejected}``.
  - ``leaves`` — caller's ``Leave`` rows with ``status='approved'`` and
    ``ends_at < today (UTC)``.
  - ``chats`` — always ``[]`` (archive surface not yet built).

* Cross-user isolation: rows belonging to a peer never leak into the
  caller's history (the service keys on ``ctx.actor_id`` and ignores
  any caller-supplied user pointer).
* Cap enforcement: a tab with more than :data:`_TAB_CAP` matching rows
  trims to the newest 50.

See ``app/api/v1/history.py`` for the route, ``mocks/app/main.py:3539-3562``
for the filter reference the production filter mirrors, and
``docs/specs/12-rest-api.md`` §"Self-service shortcuts" for the spec
row.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.expenses.models import ExpenseClaim
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.time.models import Leave
from app.adapters.db.workspace.models import UserWorkspace, WorkEngagement
from app.api import deps as api_deps
from app.api.errors import add_exception_handlers
from app.api.v1.history import _TAB_CAP, build_history_router
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration


# Pinned in the past so ``ends_at < today (UTC)`` holds for any leaf
# we want to surface as history. Tests that need to seed a "future"
# leave (for cross-tab assertions) add a positive ``timedelta``.
_PAST = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(session: Session, ctx: WorkspaceContext | None) -> TestClient:
    """Mount the history router on a throwaway app pinned to ``ctx``.

    Passing ``ctx=None`` skips the dep override so the original
    :func:`current_workspace_context` runs and surfaces the anonymous
    401 — letting us assert the no-auth path without touching the
    full identity middleware.
    """
    app = FastAPI()
    add_exception_handlers(app)
    app.include_router(build_history_router())

    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[api_deps.db_session] = override_db
    if ctx is not None:

        def override_ctx() -> WorkspaceContext:
            return ctx

        app.dependency_overrides[api_deps.current_workspace_context] = override_ctx
    return TestClient(app, raise_server_exceptions=False)


def _seed_workspace(
    session: Session,
) -> tuple[WorkspaceContext, WorkspaceContext, str, str, str, str, str]:
    """Seed a workspace with two workers + their work engagements.

    Returns ``(caller_ctx, peer_ctx, workspace_id, caller_user_id,
    peer_user_id, caller_eng_id, peer_eng_id)``. Both users carry
    ``UserWorkspace`` + ``RoleGrant`` rows so the listing seam treats
    them as members; each carries a ``WorkEngagement`` so
    :class:`ExpenseClaim` rows can ride a real FK target.
    """
    suffix = new_ulid().lower()
    owner = bootstrap_user(
        session,
        email=f"history-owner-{suffix}@example.com",
        display_name="History Owner",
    )
    caller = bootstrap_user(
        session,
        email=f"history-caller-{suffix}@example.com",
        display_name="History Caller",
    )
    peer = bootstrap_user(
        session,
        email=f"history-peer-{suffix}@example.com",
        display_name="History Peer",
    )
    workspace = bootstrap_workspace(
        session,
        slug=f"history-{suffix}",
        name="History WS",
        owner_user_id=owner.id,
    )
    for user_id in (caller.id, peer.id):
        session.add(
            UserWorkspace(
                user_id=user_id,
                workspace_id=workspace.id,
                source="workspace_grant",
                added_at=_PAST,
            )
        )
        session.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace.id,
                user_id=user_id,
                grant_role="worker",
                scope_property_id=None,
                created_at=_PAST,
                created_by_user_id=owner.id,
            )
        )
    caller_eng_id = new_ulid()
    session.add(
        WorkEngagement(
            id=caller_eng_id,
            user_id=caller.id,
            workspace_id=workspace.id,
            engagement_kind="payroll",
            supplier_org_id=None,
            pay_destination_id=None,
            reimbursement_destination_id=None,
            started_on=_PAST.date(),
            archived_on=None,
            notes_md="",
            created_at=_PAST,
            updated_at=_PAST,
        )
    )
    peer_eng_id = new_ulid()
    session.add(
        WorkEngagement(
            id=peer_eng_id,
            user_id=peer.id,
            workspace_id=workspace.id,
            engagement_kind="payroll",
            supplier_org_id=None,
            pay_destination_id=None,
            reimbursement_destination_id=None,
            started_on=_PAST.date(),
            archived_on=None,
            notes_md="",
            created_at=_PAST,
            updated_at=_PAST,
        )
    )
    session.flush()
    caller_ctx = build_workspace_context(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=caller.id,
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
    )
    peer_ctx = build_workspace_context(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=peer.id,
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
    )
    return (
        caller_ctx,
        peer_ctx,
        workspace.id,
        caller.id,
        peer.id,
        caller_eng_id,
        peer_eng_id,
    )


def _seed_property(session: Session, workspace_id: str) -> str:
    """Seed a property + workspace mapping; return the property id."""
    suffix = new_ulid().lower()
    property_id = f"prop_history_{suffix}"
    session.add(
        Property(
            id=property_id,
            name=f"History Villa {suffix}",
            kind="str",
            address="1 History Lane",
            address_json={"line1": "1 History Lane", "city": "Nice", "country": "FR"},
            country="FR",
            timezone="Europe/Paris",
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_PAST,
            updated_at=_PAST,
            deleted_at=None,
        )
    )
    session.add(
        PropertyWorkspace(
            property_id=property_id,
            workspace_id=workspace_id,
            label="History Villa",
            membership_role="owner_workspace",
            share_guest_identity=True,
            status="active",
            created_at=_PAST,
        )
    )
    session.flush()
    return property_id


def _seed_occurrence(
    session: Session,
    *,
    workspace_id: str,
    assignee_user_id: str | None,
    state: str,
    title: str,
    property_id: str | None,
    starts_at: datetime,
    created_by_user_id: str,
) -> str:
    """Seed an :class:`Occurrence` row in the requested ``state``."""
    occurrence_id = new_ulid()
    completed_at = starts_at + timedelta(hours=1) if state == "completed" else None
    session.add(
        Occurrence(
            id=occurrence_id,
            workspace_id=workspace_id,
            schedule_id=None,
            template_id=None,
            property_id=property_id,
            assignee_user_id=assignee_user_id,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            scheduled_for_local=f"{starts_at.date().isoformat()}T12:00",
            originally_scheduled_for=f"{starts_at.date().isoformat()}T12:00",
            state=state,
            cancellation_reason=None,
            completed_at=completed_at,
            completed_by_user_id=assignee_user_id if state == "completed" else None,
            skipped_reason="not needed" if state == "skipped" else None,
            title=title,
            description_md="",
            priority="normal",
            photo_evidence="disabled",
            duration_minutes=60,
            area_id=None,
            unit_id=None,
            expected_role_id=None,
            linked_instruction_ids=[],
            inventory_consumption_json={},
            is_personal=False,
            created_by_user_id=created_by_user_id,
            created_at=starts_at,
        )
    )
    session.flush()
    return occurrence_id


def _seed_claim(
    session: Session,
    *,
    workspace_id: str,
    work_engagement_id: str,
    state: str,
    vendor: str,
    decided_by: str | None = None,
    purchased_at: datetime | None = None,
) -> str:
    """Seed an :class:`ExpenseClaim` row in the requested ``state``.

    Approval / reimbursement snapshot columns are only populated when
    the state requires them — the model's CHECK constraints don't bite,
    but populating them keeps the row legal against the §09 invariants
    so cd-9guk audits don't trip on test data.
    """
    claim_id = new_ulid()
    purchased = purchased_at or _PAST
    submitted_at = purchased + timedelta(hours=1) if state != "draft" else None
    decided_at = (
        purchased + timedelta(hours=2)
        if state in {"approved", "rejected", "reimbursed"}
        else None
    )
    reimbursed_at = purchased + timedelta(hours=3) if state == "reimbursed" else None
    session.add(
        ExpenseClaim(
            id=claim_id,
            workspace_id=workspace_id,
            work_engagement_id=work_engagement_id,
            submitted_at=submitted_at,
            vendor=vendor,
            purchased_at=purchased,
            currency="EUR",
            total_amount_cents=1500,
            exchange_rate_to_default=(
                Decimal("1.0") if state in {"approved", "reimbursed"} else None
            ),
            owed_destination_id=None,
            owed_currency=("EUR" if state in {"approved", "reimbursed"} else None),
            owed_amount_cents=(1500 if state in {"approved", "reimbursed"} else None),
            owed_exchange_rate=(
                Decimal("1.0") if state in {"approved", "reimbursed"} else None
            ),
            owed_rate_source=(
                "manual" if state in {"approved", "reimbursed"} else None
            ),
            category="supplies",
            property_id=None,
            note_md="",
            llm_autofill_json=None,
            autofill_confidence_overall=None,
            state=state,
            decided_by=decided_by if state != "draft" else None,
            decided_at=decided_at,
            decision_note_md=None,
            reimbursement_destination_id=None,
            reimbursed_at=reimbursed_at,
            reimbursed_via="bank" if state == "reimbursed" else None,
            reimbursed_by=decided_by if state == "reimbursed" else None,
            created_at=purchased,
            deleted_at=None,
        )
    )
    session.flush()
    return claim_id


def _seed_leave(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
    status: str,
    starts_at: datetime,
    ends_at: datetime,
    decided_by: str | None = None,
    kind: str = "vacation",
) -> str:
    """Seed a :class:`Leave` row with the given ``status`` window."""
    leave_id = f"leave_history_{new_ulid().lower()}"
    session.add(
        Leave(
            id=leave_id,
            workspace_id=workspace_id,
            user_id=user_id,
            kind=kind,
            starts_at=starts_at,
            ends_at=ends_at,
            status=status,
            reason_md="time off",
            decided_by=decided_by,
            decided_at=ends_at if status == "approved" else None,
            created_at=starts_at,
        )
    )
    session.flush()
    return leave_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_anonymous_caller_surfaces_401(db_session: Session) -> None:
    """Skipping the ctx dep override falls through to the real
    :func:`current_workspace_context`, which raises ``Unauthorized``
    when no session cookie is present."""
    client = _client(db_session, ctx=None)

    response = client.get("/history?tab=tasks")

    assert response.status_code == 401, response.text


def test_unknown_tab_surfaces_422(db_session: Session) -> None:
    """FastAPI's Pydantic ``Literal`` validation rejects unknown tabs."""
    caller_ctx, _peer_ctx, *_ = _seed_workspace(db_session)
    client = _client(db_session, caller_ctx)

    response = client.get("/history?tab=quux")

    assert response.status_code == 422, response.text


def test_tasks_tab_returns_completed_and_skipped_only(
    db_session: Session,
) -> None:
    """Tasks tab surfaces ``completed`` + ``skipped`` rows only."""
    caller_ctx, _peer_ctx, workspace_id, caller_id, _peer_id, _eng, _peer_eng = (
        _seed_workspace(db_session)
    )
    property_id = _seed_property(db_session, workspace_id)

    completed_id = _seed_occurrence(
        db_session,
        workspace_id=workspace_id,
        assignee_user_id=caller_id,
        state="completed",
        title="Pool clean done",
        property_id=property_id,
        starts_at=_PAST,
        created_by_user_id=caller_id,
    )
    skipped_id = _seed_occurrence(
        db_session,
        workspace_id=workspace_id,
        assignee_user_id=caller_id,
        state="skipped",
        title="Skipped towel swap",
        property_id=property_id,
        starts_at=_PAST + timedelta(minutes=5),
        created_by_user_id=caller_id,
    )
    # Pending row must NOT show up — outside the history filter.
    _seed_occurrence(
        db_session,
        workspace_id=workspace_id,
        assignee_user_id=caller_id,
        state="pending",
        title="Still pending",
        property_id=property_id,
        starts_at=_PAST + timedelta(minutes=10),
        created_by_user_id=caller_id,
    )
    db_session.commit()

    client = _client(db_session, caller_ctx)
    response = client.get("/history?tab=tasks")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tab"] == "tasks"
    task_ids = {row["id"] for row in body["tasks"]}
    assert task_ids == {completed_id, skipped_id}
    assert body["chats"] == []


def test_expenses_tab_returns_decided_states_only(
    db_session: Session,
) -> None:
    """Expenses tab surfaces ``approved`` / ``reimbursed`` / ``rejected``."""
    caller_ctx, _peer_ctx, workspace_id, _caller_id, _peer_id, eng_id, _peer_eng = (
        _seed_workspace(db_session)
    )
    # Seed one claim per history-eligible state plus one ``submitted``
    # row to assert it's filtered out.
    approved_id = _seed_claim(
        db_session,
        workspace_id=workspace_id,
        work_engagement_id=eng_id,
        state="approved",
        vendor="Approved vendor",
        decided_by=_caller_id,
    )
    reimbursed_id = _seed_claim(
        db_session,
        workspace_id=workspace_id,
        work_engagement_id=eng_id,
        state="reimbursed",
        vendor="Reimbursed vendor",
        decided_by=_caller_id,
    )
    rejected_id = _seed_claim(
        db_session,
        workspace_id=workspace_id,
        work_engagement_id=eng_id,
        state="rejected",
        vendor="Rejected vendor",
        decided_by=_caller_id,
    )
    _seed_claim(
        db_session,
        workspace_id=workspace_id,
        work_engagement_id=eng_id,
        state="submitted",
        vendor="Still submitted",
    )
    db_session.commit()

    client = _client(db_session, caller_ctx)
    response = client.get("/history?tab=expenses")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tab"] == "expenses"
    states = {row["state"] for row in body["expenses"]}
    assert states == {"approved", "reimbursed", "rejected"}
    ids = {row["id"] for row in body["expenses"]}
    assert ids == {approved_id, reimbursed_id, rejected_id}


def test_leaves_tab_returns_past_approved_only(db_session: Session) -> None:
    """Leaves tab surfaces ``status='approved' AND ends_at < today``."""
    caller_ctx, _peer_ctx, workspace_id, caller_id, _peer_id, _eng, _peer_eng = (
        _seed_workspace(db_session)
    )
    past_approved_id = _seed_leave(
        db_session,
        workspace_id=workspace_id,
        user_id=caller_id,
        status="approved",
        starts_at=_PAST - timedelta(days=10),
        ends_at=_PAST - timedelta(days=8),
        decided_by=caller_id,
    )
    # Future approved — must NOT show up (ends_at is past today).
    future_starts = datetime.now(tz=UTC) + timedelta(days=5)
    _seed_leave(
        db_session,
        workspace_id=workspace_id,
        user_id=caller_id,
        status="approved",
        starts_at=future_starts,
        ends_at=future_starts + timedelta(days=2),
        decided_by=caller_id,
    )
    # Pending — must NOT show up (status filter).
    _seed_leave(
        db_session,
        workspace_id=workspace_id,
        user_id=caller_id,
        status="pending",
        starts_at=_PAST - timedelta(days=20),
        ends_at=_PAST - timedelta(days=18),
    )
    db_session.commit()

    client = _client(db_session, caller_ctx)
    response = client.get("/history?tab=leaves")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tab"] == "leaves"
    assert [row["id"] for row in body["leaves"]] == [past_approved_id]
    # ``approved_at`` mirrors ``decided_at`` for approved rows.
    assert body["leaves"][0]["approved_at"] is not None


def test_chats_tab_returns_empty(db_session: Session) -> None:
    """Chats tab is a documented placeholder until the archive surface ships."""
    caller_ctx, _peer_ctx, *_ = _seed_workspace(db_session)
    db_session.commit()

    client = _client(db_session, caller_ctx)
    response = client.get("/history?tab=chats")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tab"] == "chats"
    assert body["chats"] == []


def test_cross_user_rows_are_excluded(db_session: Session) -> None:
    """The aggregator keys on ``ctx.actor_id``; peer rows never leak."""
    caller_ctx, _peer_ctx, workspace_id, caller_id, peer_id, caller_eng, peer_eng = (
        _seed_workspace(db_session)
    )
    property_id = _seed_property(db_session, workspace_id)

    # Caller's own history rows.
    caller_task_id = _seed_occurrence(
        db_session,
        workspace_id=workspace_id,
        assignee_user_id=caller_id,
        state="completed",
        title="Caller task",
        property_id=property_id,
        starts_at=_PAST,
        created_by_user_id=caller_id,
    )
    caller_claim_id = _seed_claim(
        db_session,
        workspace_id=workspace_id,
        work_engagement_id=caller_eng,
        state="approved",
        vendor="Caller vendor",
        decided_by=caller_id,
    )
    caller_leave_id = _seed_leave(
        db_session,
        workspace_id=workspace_id,
        user_id=caller_id,
        status="approved",
        starts_at=_PAST - timedelta(days=10),
        ends_at=_PAST - timedelta(days=8),
        decided_by=caller_id,
    )

    # Peer rows — must NOT bleed into the caller's history.
    _seed_occurrence(
        db_session,
        workspace_id=workspace_id,
        assignee_user_id=peer_id,
        state="completed",
        title="Peer task",
        property_id=property_id,
        starts_at=_PAST + timedelta(minutes=1),
        created_by_user_id=peer_id,
    )
    _seed_claim(
        db_session,
        workspace_id=workspace_id,
        work_engagement_id=peer_eng,
        state="approved",
        vendor="Peer vendor",
        decided_by=peer_id,
    )
    _seed_leave(
        db_session,
        workspace_id=workspace_id,
        user_id=peer_id,
        status="approved",
        starts_at=_PAST - timedelta(days=12),
        ends_at=_PAST - timedelta(days=11),
        decided_by=peer_id,
    )
    db_session.commit()

    client = _client(db_session, caller_ctx)
    response = client.get("/history?tab=tasks")

    assert response.status_code == 200, response.text
    body = response.json()
    # Tasks: only the caller's row.
    assert [row["id"] for row in body["tasks"]] == [caller_task_id]
    assert all(row["assigned_user_id"] == caller_id for row in body["tasks"])
    # Expenses: only the caller's claim.
    assert [row["id"] for row in body["expenses"]] == [caller_claim_id]
    # Leaves: only the caller's leave.
    assert [row["id"] for row in body["leaves"]] == [caller_leave_id]


def test_tasks_tab_caps_to_fifty_rows_newest_first(
    db_session: Session,
) -> None:
    """Above :data:`_TAB_CAP`, the tasks tab keeps the newest rows only."""
    caller_ctx, _peer_ctx, workspace_id, caller_id, _peer_id, _eng, _peer_eng = (
        _seed_workspace(db_session)
    )
    property_id = _seed_property(db_session, workspace_id)
    seeded_ids: list[str] = []
    # Seed 60 completed tasks. Spaced one second apart so the ULID
    # ordering matches creation order (ULID prefix carries the
    # ms-resolution timestamp).
    for index in range(60):
        seeded_ids.append(
            _seed_occurrence(
                db_session,
                workspace_id=workspace_id,
                assignee_user_id=caller_id,
                state="completed",
                title=f"Task {index:02d}",
                property_id=property_id,
                starts_at=_PAST + timedelta(seconds=index),
                created_by_user_id=caller_id,
            )
        )
    db_session.commit()

    client = _client(db_session, caller_ctx)
    response = client.get("/history?tab=tasks")

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["tasks"]) == _TAB_CAP
    # Newest-first: the returned set is the last 50 ULIDs by sort order.
    expected_newest = sorted(seeded_ids, reverse=True)[:_TAB_CAP]
    assert [row["id"] for row in body["tasks"]] == expected_newest
