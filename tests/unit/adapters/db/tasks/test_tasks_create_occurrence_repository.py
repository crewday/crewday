"""Unit tests for :class:`SqlAlchemyTasksCreateOccurrencePort` (cd-ncbdb).

Covers the SA-backed concretion of
:class:`~app.ports.tasks_create_occurrence.TasksCreateOccurrencePort`:

* happy-path INSERT — first call for a
  ``(reservation_id, lifecycle_rule_id, occurrence_key)`` triple
  lands an :class:`~app.adapters.db.tasks.models.Occurrence` row
  tagged with the triple, returns ``"created"``.
* idempotency — re-firing the same request is a port-side
  ``"noop"`` and does not duplicate the row (the partial unique
  index would otherwise surface as :class:`IntegrityError`).
* patch-in-place — a small ``starts_at`` shift inside the
  threshold patches the existing row, returns ``"patched"``.
* regenerate — a shift past the threshold cancels the existing row
  (``state='cancelled'`` + ``cancellation_reason``) and inserts a
  fresh row, returns ``"regenerated"``.
* terminal-state guard — a hit on a row that has moved out of
  ``scheduled | pending`` (``completed``, ``approved``, …) returns
  ``"noop"`` and leaves the historical row untouched (no
  state-flip, no cancellation reason write).
* per-rule scoping — a different ``rule_id`` for the same
  reservation lands its own row.

Schema-shape coverage of the natural-key columns + the partial
unique index lives in the cd-ncbdb migration smoke under
:mod:`tests.integration` once the migration runs end-to-end.

See ``docs/specs/04-properties-and-stays.md`` §"Stay task bundles"
§"Edit semantics" + the "Idempotency contract" docstring on
:mod:`app.ports.tasks_create_occurrence`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.base import Base
from app.adapters.db.places.models import Property
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.tasks.repositories import SqlAlchemyTasksCreateOccurrencePort
from app.adapters.db.workspace.models import Workspace
from app.ports.tasks_create_occurrence import (
    DEFAULT_PATCH_IN_PLACE_THRESHOLD,
    TurnoverOccurrenceRequest,
)
from app.tenancy.context import WorkspaceContext

_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_WORKSPACE_ID = "01HWA00000000000000000WS01"
_PROPERTY_ID = "01HWA00000000000000000PRP1"
_RESERVATION_ID = "01HWA00000000000000000RES1"
_RULE_ID = "rule_default_after_checkout"
_ACTOR_ID = "01HWA00000000000000000USR1"
_CORRELATION_ID = "01HWA00000000000000000CRL1"


# ---------------------------------------------------------------------------
# Engine + session fixtures
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every adapter's ``models`` so cross-package FKs resolve.

    Mirrors the sibling helper in
    :mod:`tests.unit.adapters.db.test_property_work_role_assignment` —
    a bare ``Base.metadata.create_all`` would otherwise miss any
    cross-package target the tasks adapter references (workspace,
    property, user).
    """
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
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    s = factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def ctx() -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=_WORKSPACE_ID,
        workspace_slug="ncbdb-unit",
        actor_id=_ACTOR_ID,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=_CORRELATION_ID,
    )


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _seed_workspace(session: Session) -> None:
    session.add(
        Workspace(
            id=_WORKSPACE_ID,
            slug="ncbdb-unit",
            name="NCBDB Unit",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    session.flush()


def _seed_property(session: Session) -> None:
    session.add(
        Property(
            id=_PROPERTY_ID,
            name="Villa Sud",
            kind="str",
            address="12 Chemin des Oliviers",
            address_json={"country": "FR"},
            country="FR",
            timezone="Europe/Paris",
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
    )
    session.flush()


def _make_request(
    *,
    starts_at: datetime,
    duration: timedelta = timedelta(hours=2),
    rule_id: str = _RULE_ID,
    occurrence_key: str | None = None,
    threshold: timedelta = DEFAULT_PATCH_IN_PLACE_THRESHOLD,
) -> TurnoverOccurrenceRequest:
    return TurnoverOccurrenceRequest(
        reservation_id=_RESERVATION_ID,
        rule_id=rule_id,
        property_id=_PROPERTY_ID,
        unit_id=None,
        starts_at=starts_at,
        ends_at=starts_at + duration,
        patch_in_place_threshold=threshold,
        occurrence_key=occurrence_key,
        due_by_utc=starts_at + duration,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    """First call lands a fresh ``occurrence`` row tagged with the triple."""

    def test_first_call_inserts_occurrence(
        self,
        session: Session,
        ctx: WorkspaceContext,
    ) -> None:
        _seed_workspace(session)
        _seed_property(session)

        port = SqlAlchemyTasksCreateOccurrencePort()
        starts_at = _PINNED + timedelta(days=4)
        result = port.create_or_patch_turnover_occurrence(
            session,
            ctx,
            request=_make_request(starts_at=starts_at),
            now=_PINNED,
        )

        assert result.outcome == "created"
        assert result.occurrence_id is not None

        rows = session.scalars(
            select(Occurrence).where(Occurrence.reservation_id == _RESERVATION_ID)
        ).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.id == result.occurrence_id
        assert row.workspace_id == _WORKSPACE_ID
        assert row.property_id == _PROPERTY_ID
        assert row.lifecycle_rule_id == _RULE_ID
        # The port normalises a ``None`` occurrence_key to ``""`` so
        # the partial unique index keys the same triple every time.
        assert row.occurrence_key == ""
        assert row.state == "scheduled"
        assert row.scheduled_for_local is not None
        assert row.scheduled_for_local == row.originally_scheduled_for


class TestIdempotency:
    """Re-firing identical / shifted requests honours the port contract."""

    def test_identical_re_call_is_noop(
        self,
        session: Session,
        ctx: WorkspaceContext,
    ) -> None:
        _seed_workspace(session)
        _seed_property(session)

        port = SqlAlchemyTasksCreateOccurrencePort()
        starts_at = _PINNED + timedelta(days=4)
        request = _make_request(starts_at=starts_at)

        first = port.create_or_patch_turnover_occurrence(
            session, ctx, request=request, now=_PINNED
        )
        second = port.create_or_patch_turnover_occurrence(
            session, ctx, request=request, now=_PINNED
        )

        assert first.outcome == "created"
        assert second.outcome == "noop"
        assert second.occurrence_id == first.occurrence_id

        rows = session.scalars(
            select(Occurrence).where(Occurrence.reservation_id == _RESERVATION_ID)
        ).all()
        assert len(rows) == 1, "noop must not duplicate"

    def test_small_shift_patches_in_place(
        self,
        session: Session,
        ctx: WorkspaceContext,
    ) -> None:
        _seed_workspace(session)
        _seed_property(session)

        port = SqlAlchemyTasksCreateOccurrencePort()
        starts_at = _PINNED + timedelta(days=4)
        first = port.create_or_patch_turnover_occurrence(
            session,
            ctx,
            request=_make_request(starts_at=starts_at),
            now=_PINNED,
        )
        # Shift inside the 4 h threshold — the port must patch the
        # existing row in place.
        shifted = starts_at + timedelta(hours=1)
        result = port.create_or_patch_turnover_occurrence(
            session,
            ctx,
            request=_make_request(starts_at=shifted),
            now=_PINNED,
        )

        assert result.outcome == "patched"
        assert result.occurrence_id == first.occurrence_id

        rows = session.scalars(
            select(Occurrence).where(Occurrence.reservation_id == _RESERVATION_ID)
        ).all()
        assert len(rows) == 1
        # The patch must reflect the new window.
        assert rows[0].starts_at.replace(tzinfo=UTC) == shifted
        assert rows[0].state == "scheduled"

    def test_large_shift_regenerates(
        self,
        session: Session,
        ctx: WorkspaceContext,
    ) -> None:
        _seed_workspace(session)
        _seed_property(session)

        port = SqlAlchemyTasksCreateOccurrencePort()
        starts_at = _PINNED + timedelta(days=4)
        first = port.create_or_patch_turnover_occurrence(
            session,
            ctx,
            request=_make_request(starts_at=starts_at),
            now=_PINNED,
        )
        # Shift past the 4 h threshold — the port must cancel the
        # existing row and INSERT a fresh one.
        shifted = starts_at + timedelta(hours=8)
        result = port.create_or_patch_turnover_occurrence(
            session,
            ctx,
            request=_make_request(starts_at=shifted),
            now=_PINNED,
        )

        assert result.outcome == "regenerated"
        assert result.occurrence_id is not None
        assert result.occurrence_id != first.occurrence_id

        rows = session.scalars(
            select(Occurrence)
            .where(Occurrence.reservation_id == _RESERVATION_ID)
            .order_by(Occurrence.created_at.asc(), Occurrence.id.asc())
        ).all()
        assert len(rows) == 2
        cancelled = next(row for row in rows if row.id == first.occurrence_id)
        assert cancelled.state == "cancelled"
        assert cancelled.cancellation_reason == "stay rescheduled"
        fresh = next(row for row in rows if row.id == result.occurrence_id)
        assert fresh.state == "scheduled"

    def test_per_rule_scoping(
        self,
        session: Session,
        ctx: WorkspaceContext,
    ) -> None:
        # Two distinct ``rule_id`` values for the same reservation
        # produce two rows; the partial unique index must NOT collide
        # across rules.
        _seed_workspace(session)
        _seed_property(session)

        port = SqlAlchemyTasksCreateOccurrencePort()
        starts_at = _PINNED + timedelta(days=4)
        first = port.create_or_patch_turnover_occurrence(
            session,
            ctx,
            request=_make_request(starts_at=starts_at, rule_id="rule_after"),
            now=_PINNED,
        )
        second = port.create_or_patch_turnover_occurrence(
            session,
            ctx,
            request=_make_request(starts_at=starts_at, rule_id="rule_before"),
            now=_PINNED,
        )
        assert first.outcome == "created"
        assert second.outcome == "created"
        assert first.occurrence_id != second.occurrence_id

        rows = session.scalars(
            select(Occurrence).where(Occurrence.reservation_id == _RESERVATION_ID)
        ).all()
        assert len(rows) == 2
        assert {row.lifecycle_rule_id for row in rows} == {
            "rule_after",
            "rule_before",
        }

    def test_terminal_state_re_call_is_noop(
        self,
        session: Session,
        ctx: WorkspaceContext,
    ) -> None:
        # An already-completed (or otherwise terminal) row must NOT
        # be flipped to ``cancelled`` when the stay window shifts —
        # that would clobber the completion record. The port returns
        # ``noop`` with the existing id and leaves the historical
        # row untouched. Covers ``completed``, ``approved``,
        # ``skipped``, ``overdue``, and ``in_progress`` — only
        # ``scheduled | pending`` are writable per §04 "Edit
        # semantics".
        _seed_workspace(session)
        _seed_property(session)

        port = SqlAlchemyTasksCreateOccurrencePort()
        starts_at = _PINNED + timedelta(days=4)
        first = port.create_or_patch_turnover_occurrence(
            session,
            ctx,
            request=_make_request(starts_at=starts_at),
            now=_PINNED,
        )
        assert first.outcome == "created"

        # Worker completes the task; the row's state moves out of
        # the patchable window.
        completed = session.scalars(
            select(Occurrence).where(Occurrence.id == first.occurrence_id)
        ).one()
        completed.state = "completed"
        completed.completed_at = _PINNED + timedelta(hours=2)
        session.flush()

        # Manager pushes the stay forward by 24 h — well past the
        # 4 h threshold, which on a writable row would regenerate.
        shifted = starts_at + timedelta(hours=24)
        result = port.create_or_patch_turnover_occurrence(
            session,
            ctx,
            request=_make_request(starts_at=shifted),
            now=_PINNED,
        )

        assert result.outcome == "noop"
        assert result.occurrence_id == first.occurrence_id

        rows = session.scalars(
            select(Occurrence).where(Occurrence.reservation_id == _RESERVATION_ID)
        ).all()
        assert len(rows) == 1, "terminal row must not be cloned"
        assert rows[0].state == "completed", "completion history must survive"
        assert rows[0].cancellation_reason is None

    def test_occurrence_key_separates_recurring_rule_instances(
        self,
        session: Session,
        ctx: WorkspaceContext,
    ) -> None:
        # Recurring rules thread an explicit ``occurrence_key`` per
        # instance (``during_stay:0``, ``during_stay:1``); the port
        # must dedup per-key, not per-rule.
        _seed_workspace(session)
        _seed_property(session)

        port = SqlAlchemyTasksCreateOccurrencePort()
        starts_at = _PINNED + timedelta(days=4)
        first = port.create_or_patch_turnover_occurrence(
            session,
            ctx,
            request=_make_request(starts_at=starts_at, occurrence_key="during_stay:0"),
            now=_PINNED,
        )
        second = port.create_or_patch_turnover_occurrence(
            session,
            ctx,
            request=_make_request(
                starts_at=starts_at + timedelta(days=1),
                occurrence_key="during_stay:1",
            ),
            now=_PINNED,
        )
        third = port.create_or_patch_turnover_occurrence(
            session,
            ctx,
            request=_make_request(starts_at=starts_at, occurrence_key="during_stay:0"),
            now=_PINNED,
        )

        assert first.outcome == "created"
        assert second.outcome == "created"
        assert third.outcome == "noop"
        assert third.occurrence_id == first.occurrence_id

        rows = session.scalars(
            select(Occurrence).where(Occurrence.reservation_id == _RESERVATION_ID)
        ).all()
        assert {row.occurrence_key for row in rows} == {
            "during_stay:0",
            "during_stay:1",
        }
