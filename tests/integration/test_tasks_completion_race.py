"""Integration test for the concurrent-completion branch of
:func:`app.domain.tasks.completion.complete`.

§06 "Concurrent completion": "If two actors complete the same task
in overlapping transactions, the write that commits second wins —
it overwrites ``completed_at``, ``completed_by_user_id``, and
``completion_note_md``. The loser receives a 200 with the final
state; the audit log records both completions via
``task.complete_superseded`` against the earlier row."

This test spins up two real :class:`Session` instances on an
:class:`Engine` that commits to disk (the default ``db_session``
fixture uses the SAVEPOINT-per-test pattern and never commits
outward), calls :func:`complete` in sequence from each session,
commits, and asserts:

* both ``task.complete`` audit rows land (one per session);
* a ``task.complete_superseded`` row lands carrying the displaced
  ``completed_by_user_id`` + ``completed_at``;
* the final row holds the **second** writer's ``completed_at`` and
  ``completed_by_user_id`` — second writer wins on the fields.

Strictly overlapping transactions would require threading + a
barrier (see ``tests/integration/identity/test_last_owner_race.py``
for the template), but §06's semantics reduce the test to a
sequential scenario: the pre-state observable to writer B is the
DB's post-commit view of writer A. No locking, no read-modify-write —
second writer commits its fields unconditionally. Threading would
only exercise a flakier variant of the same assertion.

See ``docs/specs/06-tasks-and-scheduling.md`` §"Concurrent
completion".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.identity.models import User
from app.adapters.db.places.models import Property
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import Workspace
from app.domain.tasks.completion import complete
from app.events.bus import EventBus
from app.tenancy import tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture
def isolated_engine(db_url: str) -> Iterator[Engine]:
    """Engine dedicated to this race test.

    Built fresh (not the session-scoped ``engine`` fixture) so
    ``session.commit()`` calls really hit disk. The default
    ``db_session`` fixture wraps every test in a SAVEPOINT that
    rolls back on teardown — that pattern is incompatible with the
    "two sessions see each other's commits" contract this test
    exercises.
    """
    eng = make_engine(db_url)
    try:
        yield eng
    finally:
        eng.dispose()


def _ctx(
    workspace_id: str, slug: str, actor_id: str, *, role: str = "manager"
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=role,  # type: ignore[arg-type]
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )


def _bootstrap(engine: Engine) -> tuple[str, str, str, str, str]:
    """Seed a workspace + property + two users + one task.

    Commits before returning so the two race sessions both read the
    same committed rows. Wrapped in :func:`tenant_agnostic` so the
    SELECT paths the ORM uses during INSERT don't trip the tenant
    filter before we have a context.

    Each ``session.add`` is paired with an explicit ``flush()`` so a
    later insert (e.g. ``Occurrence``) sees the parent rows it FKs
    against — :func:`Session.commit` would batch the flush at the
    end and fail the FK check on the same INSERT pass.

    Returns ``(workspace_id, slug, task_id, user_a_id, user_b_id)``.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    slug = f"race-{new_ulid()[-8:].lower()}"
    with factory() as session, tenant_agnostic():
        ws_id = new_ulid()
        session.add(
            Workspace(
                id=ws_id,
                slug=slug,
                name="Race workspace",
                plan="free",
                quota_json={},
                created_at=_PINNED,
            )
        )
        session.flush()

        prop_id = new_ulid()
        session.add(
            Property(
                id=prop_id,
                address="1 Race Way",
                timezone="Europe/Paris",
                tags_json=[],
                created_at=_PINNED,
            )
        )
        session.flush()

        user_a = new_ulid()
        user_b = new_ulid()
        for uid in (user_a, user_b):
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

        task_id = new_ulid()
        session.add(
            Occurrence(
                id=task_id,
                workspace_id=ws_id,
                schedule_id=None,
                template_id=None,
                property_id=prop_id,
                assignee_user_id=user_a,
                starts_at=_PINNED,
                ends_at=_PINNED + timedelta(minutes=30),
                scheduled_for_local="2026-04-19T14:00",
                originally_scheduled_for="2026-04-19T14:00",
                state="pending",
                cancellation_reason=None,
                title="Pool clean",
                description_md="",
                priority="normal",
                photo_evidence="disabled",
                duration_minutes=30,
                area_id=None,
                unit_id=None,
                expected_role_id=None,
                linked_instruction_ids=[],
                inventory_consumption_json={},
                is_personal=False,
                created_by_user_id=user_a,
                created_at=_PINNED,
            )
        )
        session.flush()
        session.commit()
    return ws_id, slug, task_id, user_a, user_b


def _scrub(engine: Engine, workspace_id: str) -> None:
    """Tear down the committed rows so successive tests start clean."""
    from sqlalchemy import delete

    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session, tenant_agnostic():
        session.execute(delete(AuditLog).where(AuditLog.workspace_id == workspace_id))
        session.execute(
            delete(Occurrence).where(Occurrence.workspace_id == workspace_id)
        )
        session.execute(delete(Property).where(Property.address == "1 Race Way"))
        # Users and workspace: drop by id/slug is harder without the
        # ids in scope, so leave them for test isolation via unique
        # slugs (see ``_bootstrap`` — each test run mints a fresh
        # slug). A workspace hard-delete would cascade to the other
        # rows but would also require a dialect-specific FK pass —
        # overkill for a two-iteration test.
        session.execute(delete(Workspace).where(Workspace.id == workspace_id))
        session.commit()


class TestConcurrentCompletion:
    def test_second_writer_wins_and_audit_carries_both_rows(
        self, isolated_engine: Engine
    ) -> None:
        ws_id, slug, task_id, user_a, user_b = _bootstrap(isolated_engine)
        try:
            factory = sessionmaker(
                bind=isolated_engine, expire_on_commit=False, class_=Session
            )
            clock = FrozenClock(_PINNED)
            bus = EventBus()

            # --- Writer A (first) completes the task. -------------------
            with factory() as session_a:
                complete(
                    session_a,
                    _ctx(ws_id, slug, user_a, role="worker"),
                    task_id,
                    clock=clock,
                    event_bus=bus,
                )
                session_a.commit()

            # Advance the clock so the superseding completion lands
            # with a distinguishable timestamp.
            clock.advance(timedelta(minutes=2))

            # --- Writer B (second) lands on the same (already-done)
            # row. The two UoWs commit in sequence; writer B reads the
            # committed state left by writer A. -------------------------
            with factory() as session_b:
                complete(
                    session_b,
                    _ctx(ws_id, slug, user_b, role="manager"),
                    task_id,
                    clock=clock,
                    event_bus=bus,
                )
                session_b.commit()

            # --- Assertions. --------------------------------------------
            with factory() as session_read, tenant_agnostic():
                # Row state: writer B's fields win.
                row = session_read.get(Occurrence, task_id)
                assert row is not None
                assert row.state == "done"
                assert row.completed_by_user_id == user_b

                # Audit: one ``task.complete`` per writer + one
                # ``task.complete_superseded`` for writer B's pass.
                audits = session_read.scalars(
                    select(AuditLog)
                    .where(AuditLog.entity_id == task_id)
                    .order_by(AuditLog.created_at)
                ).all()
                actions = [a.action for a in audits]
                assert "task.complete" in actions
                assert actions.count("task.complete") == 2
                assert "task.complete_superseded" in actions

                # The supersession diff must point at writer A's
                # displaced completion and writer B's superseding one.
                super_row = next(
                    a for a in audits if a.action == "task.complete_superseded"
                )
                assert super_row.diff["displaced"]["completed_by_user_id"] == user_a
                assert super_row.diff["superseded_by"]["completed_by_user_id"] == user_b
        finally:
            _scrub(isolated_engine, ws_id)
