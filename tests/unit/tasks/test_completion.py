"""Unit tests for :mod:`app.domain.tasks.completion`.

Mirrors the in-memory SQLite bootstrap in
``tests/unit/tasks/test_assignment.py`` — fresh engine per test,
load every sibling ``models`` module onto the shared metadata, run
``create_all``, drive the service with :class:`FrozenClock` and a
private :class:`EventBus` so subscriptions don't leak between tests.

Covers cd-7am7:

* :func:`start` drives ``pending → in_progress``; audit only, no
  event.
* :func:`complete` drives ``pending | in_progress → done`` with
  the photo + checklist gates, writes the note, runs the inventory
  hook, fires :class:`TaskCompleted`, writes ``task.complete``.
* Photo-policy ``forbid`` rejects a supplied photo; ``require``
  rejects an empty completion; ``optional`` accepts either.
* Required checklist items block completion;
  :class:`RequiredChecklistIncomplete` carries the unchecked ids.
* Inventory hook writes :class:`Movement` rows per SKU with
  negative deltas; no-op override suppresses.
* Concurrent completion branch emits both
  ``task.complete`` and ``task.complete_superseded`` audit rows,
  and the later writer's fields land on the row.
* :func:`skip` routes through :data:`SkipAllowedResolver` for
  workers; owners / managers bypass; workers not assigned are
  rejected.
* :func:`cancel` is owners / managers only.
* :func:`revert_overdue` accepts the ``overdue`` source state and
  writes the target state.
* ``_assert_transition`` validator rejects every illegal edge.
* ``TaskCancelled`` + ``TaskSkipped`` reject free-text reasons at
  publish time via the ``_REASON_CODE_RE`` validator.

See ``docs/specs/06-tasks-and-scheduling.md`` §"State machine",
§"Completing a task", §"Skipping and cancellation",
§"Concurrent completion".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.inventory.models import Item, Movement
from app.adapters.db.places.models import Property
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import (
    ChecklistItem,
    Evidence,
    Occurrence,
)
from app.adapters.db.workspace.models import Workspace
from app.domain.tasks.completion import (
    EvidenceRequired,
    InvalidStateTransition,
    PermissionDenied,
    PhotoForbidden,
    RequiredChecklistIncomplete,
    SkipNotPermitted,
    TaskNotFound,
    _assert_transition,
    cancel,
    complete,
    revert_overdue,
    skip,
    start,
)
from app.events.bus import EventBus
from app.events.types import TaskCancelled, TaskCompleted, TaskSkipped
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures + bootstrap
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
    import importlib
    import pkgutil

    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def clock() -> FrozenClock:
    # Fresh instance per test — the public fixture in
    # ``conftest.py`` is shared across the whole tasks suite, which
    # would couple our cases unnecessarily.
    return FrozenClock(_PINNED)


def _ctx(
    workspace_id: str,
    *,
    slug: str = "ws",
    role: str = "manager",
    owner: bool = True,
    actor_id: str | None = None,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id if actor_id is not None else new_ulid(),
        actor_kind="user",
        actor_grant_role=role,  # type: ignore[arg-type]
        actor_was_owner_member=owner,
        audit_correlation_id=new_ulid(),
    )


def _bootstrap_workspace(session: Session, *, slug: str = "ws") -> str:
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


def _bootstrap_property(session: Session) -> str:
    pid = new_ulid()
    session.add(
        Property(
            id=pid,
            address="1 Villa Sud Way",
            timezone="Europe/Paris",
            tags_json=[],
            created_at=_PINNED,
        )
    )
    session.flush()
    return pid


def _bootstrap_user(session: Session) -> str:
    """Insert a minimal user row with a fresh id.

    Uses :func:`new_ulid` so the PII redactor (see
    :mod:`app.util.redact`) does not mistake a long run of zeros in
    a pinned test id for a PAN and rewrite it as ``<redacted:pan>``
    inside audit diffs.
    """
    uid = new_ulid()
    from app.adapters.db.identity.models import User

    session.add(
        User(
            id=uid,
            email=f"{uid}@example.com",
            email_lower=f"{uid}@example.com".lower(),
            display_name=uid,
            locale=None,
            timezone=None,
            avatar_blob_hash=None,
            created_at=_PINNED,
            last_login_at=None,
        )
    )
    session.flush()
    return uid


def _bootstrap_occurrence(
    session: Session,
    *,
    workspace_id: str,
    property_id: str | None,
    assignee_user_id: str | None = None,
    state: str = "pending",
    photo_evidence: str = "disabled",
    inventory: dict[str, Any] | None = None,
) -> str:
    oid = new_ulid()
    session.add(
        Occurrence(
            id=oid,
            workspace_id=workspace_id,
            schedule_id=None,
            template_id=None,
            property_id=property_id,
            assignee_user_id=assignee_user_id,
            starts_at=_PINNED,
            ends_at=_PINNED + timedelta(minutes=30),
            scheduled_for_local="2026-04-19T14:00",
            originally_scheduled_for="2026-04-19T14:00",
            state=state,
            cancellation_reason=None,
            title="Pool clean",
            description_md="",
            priority="normal",
            photo_evidence=photo_evidence,
            duration_minutes=30,
            area_id=None,
            unit_id=None,
            expected_role_id=None,
            linked_instruction_ids=[],
            inventory_consumption_json=inventory or {},
            is_personal=False,
            created_by_user_id=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return oid


def _bootstrap_required_checklist(
    session: Session, *, workspace_id: str, occurrence_id: str, label: str = "wipe"
) -> str:
    cid = new_ulid()
    session.add(
        ChecklistItem(
            id=cid,
            workspace_id=workspace_id,
            occurrence_id=occurrence_id,
            label=label,
            position=0,
            requires_photo=True,  # v1: ``requires_photo`` doubles as "required"
            checked=False,
            checked_at=None,
            evidence_blob_hash=None,
        )
    )
    session.flush()
    return cid


def _bootstrap_inventory_item(
    session: Session, *, workspace_id: str, sku: str = "BLEACH-1L"
) -> str:
    iid = new_ulid()
    session.add(
        Item(
            id=iid,
            workspace_id=workspace_id,
            sku=sku,
            name=sku,
            unit="l",
            category=None,
            barcode=None,
            current_qty=Decimal("10"),
            min_qty=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return iid


def _record(
    bus: EventBus,
) -> tuple[list[TaskCompleted], list[TaskSkipped], list[TaskCancelled]]:
    completed: list[TaskCompleted] = []
    skipped: list[TaskSkipped] = []
    cancelled: list[TaskCancelled] = []
    bus.subscribe(TaskCompleted)(completed.append)
    bus.subscribe(TaskSkipped)(skipped.append)
    bus.subscribe(TaskCancelled)(cancelled.append)
    return completed, skipped, cancelled


# ---------------------------------------------------------------------------
# Transition validator
# ---------------------------------------------------------------------------


class TestAssertTransition:
    """``_assert_transition`` encodes the §06 edge set."""

    def test_pending_to_in_progress_ok(self) -> None:
        _assert_transition("pending", "in_progress")

    def test_pending_to_done_ok(self) -> None:
        _assert_transition("pending", "done")

    def test_in_progress_to_done_ok(self) -> None:
        _assert_transition("in_progress", "done")

    def test_overdue_to_pending_ok(self) -> None:
        _assert_transition("overdue", "pending")

    def test_overdue_to_in_progress_ok(self) -> None:
        _assert_transition("overdue", "in_progress")

    def test_done_to_pending_rejected(self) -> None:
        with pytest.raises(InvalidStateTransition) as excinfo:
            _assert_transition("done", "pending")
        assert excinfo.value.current == "done"
        assert excinfo.value.target == "pending"

    def test_skipped_to_done_rejected(self) -> None:
        with pytest.raises(InvalidStateTransition):
            _assert_transition("skipped", "done")

    def test_cancelled_to_done_rejected(self) -> None:
        with pytest.raises(InvalidStateTransition):
            _assert_transition("cancelled", "done")

    def test_unknown_source_state_rejected(self) -> None:
        with pytest.raises(InvalidStateTransition):
            _assert_transition("made_up_state", "pending")


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------


class TestStart:
    """:func:`start` drives ``pending → in_progress`` with audit only."""

    def test_happy_path_flips_state(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=worker
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=worker)

        result = start(session, ctx, occ, clock=clock, event_bus=bus)

        assert result.state == "in_progress"
        row = session.get(Occurrence, occ)
        assert row is not None and row.state == "in_progress"

        audit = session.scalars(select(AuditLog).where(AuditLog.entity_id == occ)).one()
        assert audit.action == "task.start"
        assert audit.diff["before"]["state"] == "pending"
        assert audit.diff["after"]["state"] == "in_progress"

    def test_worker_not_assigned_rejected(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        owner_of_task = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            assignee_user_id=owner_of_task,
        )
        stranger = _bootstrap_user(session)
        ctx = _ctx(ws, role="worker", owner=False, actor_id=stranger)

        with pytest.raises(PermissionDenied):
            start(session, ctx, occ, clock=clock, event_bus=bus)

    def test_manager_can_start_any_task(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=worker
        )
        ctx = _ctx(ws, role="manager", owner=False)

        result = start(session, ctx, occ, clock=clock, event_bus=bus)
        assert result.state == "in_progress"

    def test_task_not_found(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        with pytest.raises(TaskNotFound):
            start(session, _ctx(ws), "missing", clock=clock, event_bus=bus)

    def test_illegal_transition_from_done(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, state="done"
        )
        with pytest.raises(InvalidStateTransition):
            start(session, _ctx(ws), occ, clock=clock, event_bus=bus)


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------


class TestComplete:
    """:func:`complete` drives ``pending | in_progress → done``."""

    def test_happy_path_emits_event_and_audit(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=worker
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=worker)
        completed, _, _ = _record(bus)

        result = complete(session, ctx, occ, clock=clock, event_bus=bus)

        assert result.state == "done"
        row = session.get(Occurrence, occ)
        assert row is not None
        assert row.state == "done"
        assert row.completed_by_user_id == worker
        # SQLite strips tzinfo on round-trip; the service wrote an
        # aware UTC value, the DB reads back naive.
        assert row.completed_at is not None
        assert row.completed_at.replace(tzinfo=UTC) == _PINNED

        audits = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == occ)
        ).all()
        actions = [a.action for a in audits]
        assert actions == ["task.complete"]
        assert len(completed) == 1
        assert completed[0].completed_by == worker

    def test_note_md_persisted_as_evidence_note(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=worker
        )

        complete(
            session,
            _ctx(ws, role="worker", owner=False, actor_id=worker),
            occ,
            note_md="all clean, filter replaced",
            clock=clock,
            event_bus=bus,
        )

        ev = session.scalars(
            select(Evidence).where(Evidence.occurrence_id == occ)
        ).one()
        assert ev.kind == "note"
        assert ev.note_md == "all clean, filter replaced"

    def test_empty_note_md_is_not_persisted(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=worker
        )

        complete(
            session,
            _ctx(ws, role="worker", owner=False, actor_id=worker),
            occ,
            note_md="   ",
            clock=clock,
            event_bus=bus,
        )

        evs = session.scalars(
            select(Evidence).where(Evidence.occurrence_id == occ)
        ).all()
        assert evs == []

    def test_photo_forbid_rejects_supplied_photo(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        # ``photo_evidence='disabled'`` → resolver returns ``forbid``.
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            assignee_user_id=worker,
            photo_evidence="disabled",
        )

        with pytest.raises(PhotoForbidden):
            complete(
                session,
                _ctx(ws, role="worker", owner=False, actor_id=worker),
                occ,
                photo_evidence_ids=["blob-1"],
                clock=clock,
                event_bus=bus,
            )

    def test_photo_require_rejects_empty_completion(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            assignee_user_id=worker,
            photo_evidence="required",
        )

        with pytest.raises(EvidenceRequired):
            complete(
                session,
                _ctx(ws, role="worker", owner=False, actor_id=worker),
                occ,
                clock=clock,
                event_bus=bus,
            )

    def test_photo_require_accepts_prelinked_photo_evidence(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        """The API layer may upload + link the :class:`Evidence` row
        before calling ``complete``. The gate reads both the payload
        AND any already-linked ``kind='photo'`` rows."""
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            assignee_user_id=worker,
            photo_evidence="required",
        )
        session.add(
            Evidence(
                id=new_ulid(),
                workspace_id=ws,
                occurrence_id=occ,
                kind="photo",
                blob_hash="sha256-abc",
                note_md=None,
                created_at=_PINNED,
                created_by_user_id=worker,
            )
        )
        session.flush()

        result = complete(
            session,
            _ctx(ws, role="worker", owner=False, actor_id=worker),
            occ,
            clock=clock,
            event_bus=bus,
        )
        assert result.state == "done"

    def test_photo_optional_is_permissive(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            assignee_user_id=worker,
            photo_evidence="optional",
        )

        result = complete(
            session,
            _ctx(ws, role="worker", owner=False, actor_id=worker),
            occ,
            photo_evidence_ids=["blob-1"],
            clock=clock,
            event_bus=bus,
        )
        assert result.state == "done"

    def test_required_checklist_blocks_completion(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=worker
        )
        item_id = _bootstrap_required_checklist(
            session, workspace_id=ws, occurrence_id=occ
        )

        with pytest.raises(RequiredChecklistIncomplete) as excinfo:
            complete(
                session,
                _ctx(ws, role="worker", owner=False, actor_id=worker),
                occ,
                clock=clock,
                event_bus=bus,
            )
        assert item_id in excinfo.value.unchecked_ids

    def test_checklist_resolver_off_bypasses_gate(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=worker
        )
        _bootstrap_required_checklist(session, workspace_id=ws, occurrence_id=occ)

        def off(session: Session, ctx: WorkspaceContext, task: Occurrence) -> bool:
            _ = session, ctx, task
            return False

        result = complete(
            session,
            _ctx(ws, role="worker", owner=False, actor_id=worker),
            occ,
            clock=clock,
            event_bus=bus,
            checklist_required=off,
        )
        assert result.state == "done"

    def test_inventory_default_writes_movement_rows(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        item_id = _bootstrap_inventory_item(session, workspace_id=ws, sku="BLEACH-1L")
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            assignee_user_id=worker,
            inventory={"BLEACH-1L": 2},
        )

        complete(
            session,
            _ctx(ws, role="worker", owner=False, actor_id=worker),
            occ,
            clock=clock,
            event_bus=bus,
        )

        mov = session.scalars(
            select(Movement).where(Movement.occurrence_id == occ)
        ).one()
        assert mov.item_id == item_id
        assert mov.delta == Decimal("-2")
        assert mov.reason == "consume"

    def test_inventory_noop_hook_suppresses_movements(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        _bootstrap_inventory_item(session, workspace_id=ws, sku="BLEACH-1L")
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            assignee_user_id=worker,
            inventory={"BLEACH-1L": 2},
        )

        def noop(session: Session, ctx: WorkspaceContext, task: Occurrence) -> None:
            _ = session, ctx, task

        complete(
            session,
            _ctx(ws, role="worker", owner=False, actor_id=worker),
            occ,
            clock=clock,
            event_bus=bus,
            inventory_apply=noop,
        )

        mov = session.scalars(
            select(Movement).where(Movement.occurrence_id == occ)
        ).all()
        assert mov == []

    def test_inventory_unknown_sku_skipped(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            assignee_user_id=worker,
            inventory={"GHOST-SKU": 1},
        )

        complete(
            session,
            _ctx(ws, role="worker", owner=False, actor_id=worker),
            occ,
            clock=clock,
            event_bus=bus,
        )
        mov = session.scalars(
            select(Movement).where(Movement.occurrence_id == occ)
        ).all()
        assert mov == []

    def test_concurrent_completion_emits_superseded_audit(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        """Two writers racing through ``complete()``: both land on the
        row (second wins on fields) and the audit log carries BOTH
        ``task.complete`` and ``task.complete_superseded`` rows.

        This unit test simulates the race with a single session by
        calling :func:`complete` twice in sequence; the integration
        counterpart at ``tests/integration/test_tasks_completion_race``
        exercises the real two-UoW case.
        """
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        first = _bootstrap_user(session)
        second = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=first
        )
        ctx_first = _ctx(ws, role="worker", owner=False, actor_id=first)
        complete(session, ctx_first, occ, clock=clock, event_bus=bus)

        # Second writer lands on the same (already-done) row.
        clock.advance(timedelta(minutes=1))
        ctx_second = _ctx(ws, role="manager", owner=False, actor_id=second)
        complete(session, ctx_second, occ, clock=clock, event_bus=bus)

        audits = session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == occ)
            .order_by(AuditLog.created_at)
        ).all()
        actions = [a.action for a in audits]
        assert actions == [
            "task.complete",
            "task.complete",
            "task.complete_superseded",
        ]
        # The superseding diff carries the displaced fields.
        super_row = audits[-1]
        assert super_row.diff["displaced"]["completed_by_user_id"] == first
        assert super_row.diff["superseded_by"]["completed_by_user_id"] == second

        # Second writer's fields landed on the row.
        row = session.get(Occurrence, occ)
        assert row is not None
        assert row.completed_by_user_id == second

    def test_permission_denied_for_non_assignee_worker(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        owner_u = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=owner_u
        )
        stranger = _bootstrap_user(session)

        with pytest.raises(PermissionDenied):
            complete(
                session,
                _ctx(ws, role="worker", owner=False, actor_id=stranger),
                occ,
                clock=clock,
                event_bus=bus,
            )


# ---------------------------------------------------------------------------
# skip()
# ---------------------------------------------------------------------------


class TestSkip:
    """:func:`skip` drops a task to ``skipped`` with a reason."""

    def test_manager_can_skip_unassigned_task(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        occ = _bootstrap_occurrence(session, workspace_id=ws, property_id=prop)
        _, skipped, _ = _record(bus)

        result = skip(
            session,
            _ctx(ws, role="manager", owner=False),
            occ,
            reason="guest_left_early",
            clock=clock,
            event_bus=bus,
        )

        assert result.state == "skipped"
        row = session.get(Occurrence, occ)
        assert row is not None
        assert row.state == "skipped"
        assert row.cancellation_reason == "guest_left_early"
        assert len(skipped) == 1
        assert skipped[0].reason == "guest_left_early"

    def test_worker_can_skip_own_task_when_resolver_true(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=worker
        )

        result = skip(
            session,
            _ctx(ws, role="worker", owner=False, actor_id=worker),
            occ,
            reason="weather_blocked",
            clock=clock,
            event_bus=bus,
        )
        assert result.state == "skipped"

    def test_worker_blocked_when_resolver_false(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=worker
        )

        def off(session: Session, ctx: WorkspaceContext, task: Occurrence) -> bool:
            _ = session, ctx, task
            return False

        with pytest.raises(SkipNotPermitted):
            skip(
                session,
                _ctx(ws, role="worker", owner=False, actor_id=worker),
                occ,
                reason="weather_blocked",
                clock=clock,
                event_bus=bus,
                skip_allowed=off,
            )

    def test_manager_bypasses_resolver(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        occ = _bootstrap_occurrence(session, workspace_id=ws, property_id=prop)

        def off(session: Session, ctx: WorkspaceContext, task: Occurrence) -> bool:
            _ = session, ctx, task
            return False

        result = skip(
            session,
            _ctx(ws, role="manager", owner=False),
            occ,
            reason="closure_confirmed",
            clock=clock,
            event_bus=bus,
            skip_allowed=off,
        )
        assert result.state == "skipped"

    def test_worker_cannot_skip_someone_elses_task(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        owner_u = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=owner_u
        )
        stranger = _bootstrap_user(session)

        with pytest.raises(PermissionDenied):
            skip(
                session,
                _ctx(ws, role="worker", owner=False, actor_id=stranger),
                occ,
                reason="not_needed",
                clock=clock,
                event_bus=bus,
            )

    def test_client_guest_rejected(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        occ = _bootstrap_occurrence(session, workspace_id=ws, property_id=prop)

        with pytest.raises(PermissionDenied):
            skip(
                session,
                _ctx(ws, role="client", owner=False),
                occ,
                reason="not_needed",
                clock=clock,
                event_bus=bus,
            )

    def test_free_text_reason_rejected_at_publish(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        occ = _bootstrap_occurrence(session, workspace_id=ws, property_id=prop)

        with pytest.raises(ValidationError):
            skip(
                session,
                _ctx(ws, role="manager", owner=False),
                occ,
                reason="Manager decided it was not needed",
                clock=clock,
                event_bus=bus,
            )


# ---------------------------------------------------------------------------
# cancel()
# ---------------------------------------------------------------------------


class TestCancel:
    """:func:`cancel` is owners / managers only."""

    def test_owner_can_cancel(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        occ = _bootstrap_occurrence(session, workspace_id=ws, property_id=prop)
        _, _, cancelled = _record(bus)

        result = cancel(
            session,
            _ctx(ws, role="worker", owner=True),  # owner flag wins
            occ,
            reason="schedule_deleted",
            clock=clock,
            event_bus=bus,
        )
        assert result.state == "cancelled"
        assert len(cancelled) == 1
        assert cancelled[0].reason == "schedule_deleted"
        row = session.get(Occurrence, occ)
        assert row is not None
        assert row.cancellation_reason == "schedule_deleted"

    def test_manager_can_cancel(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        occ = _bootstrap_occurrence(session, workspace_id=ws, property_id=prop)

        result = cancel(
            session,
            _ctx(ws, role="manager", owner=False),
            occ,
            reason="owner_request",
            clock=clock,
            event_bus=bus,
        )
        assert result.state == "cancelled"

    def test_worker_rejected(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=worker
        )

        with pytest.raises(PermissionDenied):
            cancel(
                session,
                _ctx(ws, role="worker", owner=False, actor_id=worker),
                occ,
                reason="owner_request",
                clock=clock,
                event_bus=bus,
            )

    def test_illegal_from_terminal(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, state="done"
        )

        with pytest.raises(InvalidStateTransition):
            cancel(
                session,
                _ctx(ws, role="manager", owner=False),
                occ,
                reason="owner_request",
                clock=clock,
                event_bus=bus,
            )

    def test_free_text_reason_rejected_at_publish(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        occ = _bootstrap_occurrence(session, workspace_id=ws, property_id=prop)

        with pytest.raises(ValidationError):
            cancel(
                session,
                _ctx(ws, role="manager", owner=False),
                occ,
                reason="Manager thinks this is unnecessary work",
                clock=clock,
                event_bus=bus,
            )


# ---------------------------------------------------------------------------
# revert_overdue()
# ---------------------------------------------------------------------------


class TestRevertOverdue:
    """:func:`revert_overdue` drops the soft ``overdue`` state."""

    def test_overdue_back_to_pending(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        _ = bus  # no event
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=worker
        )
        # Set the in-memory state to ``overdue`` without flushing so
        # the DB CHECK doesn't reject it (the enum widening lands
        # with the §06 spec-drift follow-up). ``no_autoflush`` keeps
        # the SELECT inside :func:`revert_overdue` from triggering an
        # autoflush — the service then mutates ``state`` again and
        # the eventual flush writes the legal ``pending`` value.
        row = session.get(Occurrence, occ)
        assert row is not None
        with session.no_autoflush:
            row.state = "overdue"
            result = revert_overdue(
                session,
                _ctx(ws, role="worker", owner=False, actor_id=worker),
                occ,
                target_state="pending",
                clock=clock,
            )
        assert result.state == "pending"
        # Re-fetch to confirm it landed.
        session.expire(row)
        fresh = session.get(Occurrence, occ)
        assert fresh is not None
        assert fresh.state == "pending"

        audit = session.scalars(select(AuditLog).where(AuditLog.entity_id == occ)).one()
        assert audit.action == "task.revert_overdue"
        assert audit.diff["after"]["state"] == "pending"

    def test_overdue_back_to_in_progress(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        worker = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=worker
        )
        row = session.get(Occurrence, occ)
        assert row is not None
        with session.no_autoflush:
            row.state = "overdue"
            result = revert_overdue(
                session,
                _ctx(ws, role="manager", owner=False),
                occ,
                target_state="in_progress",
                clock=clock,
            )
        assert result.state == "in_progress"

    def test_permission_denied_for_stranger_worker(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        assignee = _bootstrap_user(session)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=assignee
        )
        stranger = _bootstrap_user(session)
        row = session.get(Occurrence, occ)
        assert row is not None
        with session.no_autoflush:
            row.state = "overdue"
            with pytest.raises(PermissionDenied):
                revert_overdue(
                    session,
                    _ctx(ws, role="worker", owner=False, actor_id=stranger),
                    occ,
                    target_state="pending",
                    clock=clock,
                )
