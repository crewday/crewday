"""SA-backed concretion of :mod:`app.ports.tasks_create_occurrence`.

The cd-ncbdb adapter persists a turnover :class:`Occurrence` row
when the stays-side bundle service / turnover generator decides one
should exist. Until cd-ncbdb landed the
:class:`~app.ports.tasks_create_occurrence.NoopTasksCreateOccurrencePort`
stub kept the call surface honest while the actual write surface was
absent; this module flips that wiring to a live concretion that
implements the full create-or-patch state machine the port
docstring promises.

**Idempotency.** The natural key is
``(workspace_id, reservation_id, lifecycle_rule_id, occurrence_key)``
matching the partial unique index added in migration cd-ncbdb. The
adapter falls back to an empty-string ``occurrence_key`` when the
caller leaves it ``None`` so single-shot rules (the default
``after_checkout`` rule and ``before_checkin``) dedup on
``(reservation_id, lifecycle_rule_id)`` alone.

**State machine.** Mirrors the port docstring:

1. Look up an existing row by the natural key.
2. Miss → INSERT a fresh ``occurrence`` row tagged with the triple,
   return ``"created"``.
3. Hit + identical ``starts_at`` / ``ends_at`` → no-op, return the
   stored id.
4. Hit + state is terminal / in-progress (``completed``,
   ``approved``, ``skipped``, ``overdue``, ``in_progress``) → no-op,
   return the stored id. Historical rows are immutable; the new
   bundle keeps the existing occurrence as its tasks_json entry.
5. Hit + |Δstarts_at| < ``patch_in_place_threshold`` and the row is
   in ``scheduled | pending`` → patch ``starts_at`` / ``ends_at`` /
   ``due_by_utc`` / ``scheduled_for_local`` in place, return
   ``"patched"``.
6. Hit + |Δstarts_at| ≥ threshold and the row is in
   ``scheduled | pending`` → cancel the existing row
   (``state='cancelled'``,
   ``cancellation_reason=request.regenerate_cancellation_reason``),
   INSERT a fresh row, return ``"regenerated"``.

Reaches across the workspace boundary (the active
:class:`~sqlalchemy.orm.Session`) but the caller's UoW owns the
commit boundary — the adapter only flushes so a peer read in the
same UoW sees the row. Mirrors §01 "Key runtime invariants" #3.

See ``docs/specs/04-properties-and-stays.md`` §"Stay task bundles"
§"Edit semantics" + ``docs/specs/06-tasks-and-scheduling.md``
§"Task row".
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import Property
from app.adapters.db.tasks.models import Occurrence
from app.ports.tasks_create_occurrence import (
    TasksCreateOccurrenceOutcome,
    TurnoverOccurrenceRequest,
    TurnoverOccurrenceResult,
)
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid

__all__ = ["SqlAlchemyTasksCreateOccurrencePort"]


# Occurrence states the spec gates in-place patches on. cd-04
# "Edit semantics" pins ``scheduled | pending`` as the writable
# window — anything past that (``in_progress``, ``completed``,
# ``approved``, ``skipped``, ``overdue``, ``cancelled``) means the
# patch path can't safely mutate the row, so a window shift larger
# than the threshold OR an unwritable state both fall through to
# the regenerate branch.
_PATCHABLE_STATES: frozenset[str] = frozenset({"scheduled", "pending"})


class SqlAlchemyTasksCreateOccurrencePort:
    """Live SA concretion of :class:`TasksCreateOccurrencePort`.

    Stateless — every call resolves the triple against the open
    session. The adapter is intentionally trivial: it does not own
    the bundle service's tasks_json bookkeeping, the audit row, or
    any cross-context fan-out. Those belong to the caller. This
    class is the single seam between the stays generator's request
    shape and the ``occurrence`` table's rows.
    """

    def create_or_patch_turnover_occurrence(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        request: TurnoverOccurrenceRequest,
        now: datetime,
    ) -> TurnoverOccurrenceResult:
        # Defence-in-depth: every timestamp that reaches the row is
        # tz-aware UTC. The caller (turnover generator + bundle
        # service) already enforces this; restamp here so a future
        # caller that forgets cannot smuggle a naive datetime into
        # ``starts_at`` / ``ends_at`` / ``due_by_utc``.
        starts_at = _to_utc(request.starts_at)
        ends_at = _to_utc(request.ends_at)
        due_by = _to_utc(request.due_by_utc) if request.due_by_utc is not None else None
        occurrence_key = request.occurrence_key or ""

        existing = session.scalars(
            select(Occurrence)
            .where(Occurrence.workspace_id == ctx.workspace_id)
            .where(Occurrence.reservation_id == request.reservation_id)
            .where(Occurrence.lifecycle_rule_id == request.rule_id)
            .where(Occurrence.occurrence_key == occurrence_key)
            .where(Occurrence.state != "cancelled")
            .limit(1)
        ).one_or_none()

        if existing is None:
            return _insert_occurrence(
                session,
                ctx,
                request=request,
                starts_at=starts_at,
                ends_at=ends_at,
                due_by=due_by,
                occurrence_key=occurrence_key,
                now=now,
                outcome="created",
            )

        existing_starts = _to_utc(existing.starts_at)
        existing_ends = _to_utc(existing.ends_at)
        if existing_starts == starts_at and existing_ends == ends_at:
            return TurnoverOccurrenceResult(occurrence_id=existing.id, outcome="noop")

        # Both the patch and regenerate branches require the existing
        # row to be in ``scheduled | pending`` per §04 "Edit semantics"
        # ("cancel the existing bundle's `scheduled | pending` tasks").
        # A terminal row (``completed``, ``approved``, ``skipped``,
        # ``overdue``) carries history we MUST NOT overwrite —
        # silently flipping ``state`` to ``cancelled`` would clobber
        # the completion record that ``completed_at`` /
        # ``completion_note_md`` already pin. ``in_progress`` is
        # mid-flight and shouldn't be yanked out from under the
        # worker either. Return ``noop`` with the existing id so the
        # caller's audit logs the no-action and the historical row
        # stands. The new bundle keeps the old occurrence as its
        # tasks_json entry; a fresh INSERT would collide with the
        # partial unique index anyway (the predicate is ``state !=
        # 'cancelled'``, so terminal / in-progress rows are still in
        # the index).
        if existing.state not in _PATCHABLE_STATES:
            return TurnoverOccurrenceResult(occurrence_id=existing.id, outcome="noop")

        delta = abs(starts_at - existing_starts)
        if delta < request.patch_in_place_threshold:
            existing.starts_at = starts_at
            existing.ends_at = ends_at
            if due_by is not None:
                existing.due_by_utc = due_by
            existing.scheduled_for_local = _scheduled_for_local(
                session, request.property_id, starts_at
            )
            session.flush()
            return TurnoverOccurrenceResult(
                occurrence_id=existing.id, outcome="patched"
            )

        # Regenerate: cancel the existing scheduled / pending row and
        # insert a fresh one. The partial unique index excludes
        # ``state='cancelled'`` so the cancelled tombstone keeps its
        # triple visible for audit while the regenerated row inherits
        # the live triple.
        existing.state = "cancelled"
        existing.cancellation_reason = request.regenerate_cancellation_reason
        session.flush()

        return _insert_occurrence(
            session,
            ctx,
            request=request,
            starts_at=starts_at,
            ends_at=ends_at,
            due_by=due_by,
            occurrence_key=occurrence_key,
            now=now,
            outcome="regenerated",
        )


def _insert_occurrence(
    session: Session,
    ctx: WorkspaceContext,
    *,
    request: TurnoverOccurrenceRequest,
    starts_at: datetime,
    ends_at: datetime,
    due_by: datetime | None,
    occurrence_key: str,
    now: datetime,
    outcome: TasksCreateOccurrenceOutcome,
) -> TurnoverOccurrenceResult:
    """Insert a fresh ``occurrence`` row and return the port outcome."""
    scheduled_for_local = _scheduled_for_local(session, request.property_id, starts_at)
    row = Occurrence(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        schedule_id=None,
        template_id=None,
        property_id=request.property_id,
        unit_id=request.unit_id,
        starts_at=starts_at,
        ends_at=ends_at,
        scheduled_for_local=scheduled_for_local,
        originally_scheduled_for=scheduled_for_local,
        state="scheduled",
        due_by_utc=due_by,
        reservation_id=request.reservation_id,
        lifecycle_rule_id=request.rule_id,
        occurrence_key=occurrence_key,
        created_at=now,
    )
    session.add(row)
    session.flush()
    return TurnoverOccurrenceResult(occurrence_id=row.id, outcome=outcome)


def _scheduled_for_local(
    session: Session,
    property_id: str,
    starts_at: datetime,
) -> str:
    """Return the ISO-8601 property-local timestamp for ``starts_at``.

    Mirrors :func:`app.worker.tasks.generator._iso_local`'s shape so
    a stay-driven occurrence reads the same column the schedule
    generator's rows do. Falls back to the UTC ISO string if the
    property row is missing — defence against a race where the
    reservation row outlives its property; never crash the port.
    """
    prop = session.get(Property, property_id)
    if prop is None:
        return starts_at.astimezone(UTC).isoformat(timespec="minutes")
    tz = ZoneInfo(prop.timezone)
    local = starts_at.astimezone(tz).replace(tzinfo=None)
    return local.isoformat(timespec="minutes")


def _to_utc(value: datetime) -> datetime:
    """Restamp UTC tz on a possibly-naive datetime.

    SQLite drops tzinfo off ``DateTime(timezone=True)`` columns on
    read; Postgres preserves it. The column is always written as
    aware UTC, so a naive read is a UTC value that has lost its
    zone. Mirrors the helper in
    :mod:`app.domain.stays.turnover_generator`.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
