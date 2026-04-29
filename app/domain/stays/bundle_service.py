"""Stay task bundle generation driven by lifecycle rules.

This module broadens the turnover generator's in-memory rule path to
cover the full stay-task bundle surface while the database is still
between slices: ``stay_bundle`` exists, but ``stay_lifecycle_rule`` and
``occurrence.stay_task_bundle_id`` do not. Rule identity is therefore
stored in ``StayBundle.tasks_json`` and threaded through the tasks port
request so the future tasks adapter can persist the linkage without a
stays-side migration.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from dateutil.rrule import rrulestr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.stays.models import Reservation, StayBundle
from app.domain.stays.turnover_generator import (
    DEFAULT_RULES,
    GuestKind,
    ReservationContext,
    ReservationContextResolver,
    StaticReservationContextResolver,
    _ensure_utc,
    _gap_intersects_closure,
    _load_reservation,
    _next_stay_check_in,
)
from app.events.bus import EventBus
from app.events.types import ReservationUpserted
from app.ports.tasks_create_occurrence import (
    DEFAULT_PATCH_IN_PLACE_THRESHOLD,
    TasksCreateOccurrencePort,
    TurnoverOccurrenceRequest,
)
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid

__all__ = [
    "BundleGenerationResult",
    "BundleOccurrenceOutcome",
    "BundleRuleOutcome",
    "StayLifecycleRule",
    "cancel_bundles_for_stay",
    "generate_bundles_for_stay",
    "list_bundles",
    "reapply_bundles_for_stay",
    "register_subscriptions",
]


_log = logging.getLogger(__name__)

LifecycleTrigger = Literal["after_checkout", "before_checkin", "during_stay"]
BundleKind = Literal["turnover", "welcome", "deep_clean"]
BundleDecision = Literal[
    "materialised",
    "skipped_guest_kind",
    "skipped_no_next_stay",
    "skipped_zero_gap",
    "skipped_closure",
    "skipped_no_rrule",
]


@dataclass(frozen=True, slots=True)
class StayLifecycleRule:
    id: str
    trigger: LifecycleTrigger
    duration: timedelta
    kind: BundleKind
    template_id: str | None = None
    offset_hours: int | None = None
    rrule: str | None = None
    unit_id: str | None = None
    guest_kind_filter: tuple[GuestKind, ...] | None = None
    ordinal: int = 0
    active: bool = True


DEFAULT_LIFECYCLE_RULES: tuple[StayLifecycleRule, ...] = tuple(
    StayLifecycleRule(
        id=rule.id,
        trigger=rule.trigger,
        duration=rule.duration,
        kind="turnover",
        guest_kind_filter=rule.guest_kind_filter,
    )
    for rule in DEFAULT_RULES
)


@dataclass(frozen=True, slots=True)
class BundleOccurrenceOutcome:
    occurrence_key: str
    occurrence_id: str | None
    port_outcome: str
    starts_at: datetime
    ends_at: datetime
    due_by_utc: datetime


@dataclass(frozen=True, slots=True)
class BundleRuleOutcome:
    rule_id: str
    bundle_id: str | None
    decision: BundleDecision
    occurrences: tuple[BundleOccurrenceOutcome, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class BundleGenerationResult:
    reservation_id: str
    skipped_reason: str | None
    per_rule: tuple[BundleRuleOutcome, ...] = field(default_factory=tuple)


def generate_bundles_for_stay(
    session: Session,
    ctx: WorkspaceContext,
    *,
    reservation_id: str,
    port: TasksCreateOccurrencePort,
    resolver: ReservationContextResolver | None = None,
    rules: tuple[StayLifecycleRule, ...] = DEFAULT_LIFECYCLE_RULES,
    now: datetime | None = None,
) -> BundleGenerationResult:
    resolved_now = _resolve_now(now)
    reservation = _active_reservation(session, ctx, reservation_id=reservation_id)
    if reservation is None:
        return BundleGenerationResult(
            reservation_id=reservation_id,
            skipped_reason="reservation_missing",
        )

    enriched = _resolve_context(
        session, ctx, resolver=resolver, reservation=reservation
    )
    existing = _bundles_by_rule(session, ctx, reservation_id=reservation.id)
    outcomes = [
        _apply_rule(
            session,
            ctx,
            reservation=reservation,
            enriched=enriched,
            rule=rule,
            bundle=existing.get(rule.id),
            port=port,
            now=resolved_now,
            regenerate=False,
        )
        for rule in _active_matching_unit_rules(rules, unit_id=enriched.unit_id)
    ]
    return BundleGenerationResult(
        reservation_id=reservation.id,
        skipped_reason=None,
        per_rule=tuple(outcomes),
    )


def reapply_bundles_for_stay(
    session: Session,
    ctx: WorkspaceContext,
    *,
    reservation_id: str,
    previous_check_in: datetime,
    previous_check_out: datetime,
    port: TasksCreateOccurrencePort,
    resolver: ReservationContextResolver | None = None,
    rules: tuple[StayLifecycleRule, ...] = DEFAULT_LIFECYCLE_RULES,
    now: datetime | None = None,
) -> BundleGenerationResult:
    resolved_now = _resolve_now(now)
    reservation = _active_reservation(session, ctx, reservation_id=reservation_id)
    if reservation is None:
        return BundleGenerationResult(
            reservation_id=reservation_id,
            skipped_reason="reservation_missing",
        )

    enriched = _resolve_context(
        session, ctx, resolver=resolver, reservation=reservation
    )
    existing = _bundles_by_rule(session, ctx, reservation_id=reservation.id)
    outcomes: list[BundleRuleOutcome] = []
    for rule in _active_matching_unit_rules(rules, unit_id=enriched.unit_id):
        bundle = existing.get(rule.id)
        regenerate = _requires_regeneration(
            reservation=reservation,
            rule=rule,
            previous_check_in=previous_check_in,
            previous_check_out=previous_check_out,
        )
        if regenerate and bundle is not None:
            _record_bundle_cancellation(bundle, reason="stay rescheduled")
        outcomes.append(
            _apply_rule(
                session,
                ctx,
                reservation=reservation,
                enriched=enriched,
                rule=rule,
                bundle=bundle,
                port=port,
                now=resolved_now,
                regenerate=regenerate,
            )
        )
    return BundleGenerationResult(
        reservation_id=reservation.id,
        skipped_reason=None,
        per_rule=tuple(outcomes),
    )


def cancel_bundles_for_stay(
    session: Session,
    ctx: WorkspaceContext,
    *,
    reservation_id: str,
    reason: str,
) -> int:
    bundles = list_bundles(session, ctx, reservation_id=reservation_id)
    for bundle in bundles:
        _record_bundle_cancellation(bundle, reason=reason)
    return len(bundles)


def list_bundles(
    session: Session,
    ctx: WorkspaceContext,
    *,
    reservation_id: str,
) -> tuple[StayBundle, ...]:
    stmt = (
        select(StayBundle)
        .where(StayBundle.workspace_id == ctx.workspace_id)
        .where(StayBundle.reservation_id == reservation_id)
        .order_by(StayBundle.created_at.asc(), StayBundle.id.asc())
    )
    return tuple(session.scalars(stmt).all())


SessionContextProvider = Callable[
    [ReservationUpserted], tuple[Session, WorkspaceContext] | None
]

_SUBSCRIBED_BUSES: set[int] = set()
_SUBSCRIBED_BUSES_LOCK = threading.Lock()


def register_subscriptions(
    event_bus: EventBus,
    *,
    port: TasksCreateOccurrencePort,
    session_provider: SessionContextProvider,
    resolver: ReservationContextResolver | None = None,
    rules: tuple[StayLifecycleRule, ...] = DEFAULT_LIFECYCLE_RULES,
) -> None:
    bus_id = id(event_bus)
    with _SUBSCRIBED_BUSES_LOCK:
        if bus_id in _SUBSCRIBED_BUSES:
            return
        _SUBSCRIBED_BUSES.add(bus_id)

    @event_bus.subscribe(ReservationUpserted)
    def _on_reservation_upserted(event: ReservationUpserted) -> None:
        bound = session_provider(event)
        if bound is None:
            _log.info(
                "stays.bundle_service.no_session_for_event",
                extra={
                    "event": "stays.bundle_service.no_session_for_event",
                    "reservation_id": event.reservation_id,
                    "workspace_id": event.workspace_id,
                },
            )
            return
        session, ctx = bound
        if event.change_kind == "cancelled":
            cancel_bundles_for_stay(
                session,
                ctx,
                reservation_id=event.reservation_id,
                reason="stay cancelled",
            )
            return
        generate_bundles_for_stay(
            session,
            ctx,
            reservation_id=event.reservation_id,
            port=port,
            resolver=resolver,
            rules=rules,
        )


def _reset_subscriptions_for_tests() -> None:
    with _SUBSCRIBED_BUSES_LOCK:
        _SUBSCRIBED_BUSES.clear()


def _apply_rule(
    session: Session,
    ctx: WorkspaceContext,
    *,
    reservation: Reservation,
    enriched: ReservationContext,
    rule: StayLifecycleRule,
    bundle: StayBundle | None,
    port: TasksCreateOccurrencePort,
    now: datetime,
    regenerate: bool,
) -> BundleRuleOutcome:
    if (
        rule.guest_kind_filter is not None
        and enriched.guest_kind not in rule.guest_kind_filter
    ):
        return BundleRuleOutcome(
            rule_id=rule.id,
            bundle_id=bundle.id if bundle is not None else None,
            decision="skipped_guest_kind",
        )

    windows = _windows_for_rule(
        session,
        ctx,
        reservation=reservation,
        rule=rule,
        unit_id=enriched.unit_id,
    )
    if isinstance(windows, str):
        return BundleRuleOutcome(
            rule_id=rule.id,
            bundle_id=bundle.id if bundle is not None else None,
            decision=windows,
        )

    resolved_bundle = (
        bundle
        if bundle is not None
        else _create_bundle(session, ctx, reservation=reservation, rule=rule, now=now)
    )
    occurrence_outcomes: list[BundleOccurrenceOutcome] = []
    task_entries: list[dict[str, object]] = []
    for window in windows:
        request = TurnoverOccurrenceRequest(
            reservation_id=reservation.id,
            rule_id=rule.id,
            property_id=reservation.property_id,
            unit_id=enriched.unit_id,
            starts_at=window.starts_at,
            ends_at=window.ends_at,
            patch_in_place_threshold=DEFAULT_PATCH_IN_PLACE_THRESHOLD,
            stay_task_bundle_id=resolved_bundle.id,
            occurrence_key=window.occurrence_key,
            due_by_utc=window.due_by_utc,
            regenerate_cancellation_reason="stay rescheduled",
        )
        result = port.create_or_patch_turnover_occurrence(
            session,
            ctx,
            request=request,
            now=now,
        )
        entry: dict[str, object] = {
            "rule_id": rule.id,
            "trigger": rule.trigger,
            "kind": rule.kind,
            "template_id": rule.template_id,
            "occurrence_key": window.occurrence_key,
            "occurrence_id": result.occurrence_id,
            "port_outcome": result.outcome,
            "starts_at": window.starts_at.isoformat(),
            "ends_at": window.ends_at.isoformat(),
            "due_by_utc": window.due_by_utc.isoformat(),
            "stay_task_bundle_id": resolved_bundle.id,
        }
        if regenerate and result.outcome == "regenerated":
            entry["cancellation_reason"] = "stay rescheduled"
        task_entries.append(entry)
        occurrence_outcomes.append(
            BundleOccurrenceOutcome(
                occurrence_key=window.occurrence_key,
                occurrence_id=result.occurrence_id,
                port_outcome=result.outcome,
                starts_at=window.starts_at,
                ends_at=window.ends_at,
                due_by_utc=window.due_by_utc,
            )
        )

    resolved_bundle.tasks_json = task_entries
    session.flush()
    return BundleRuleOutcome(
        rule_id=rule.id,
        bundle_id=resolved_bundle.id,
        decision="materialised",
        occurrences=tuple(occurrence_outcomes),
    )


@dataclass(frozen=True, slots=True)
class _Window:
    occurrence_key: str
    starts_at: datetime
    ends_at: datetime
    due_by_utc: datetime


def _windows_for_rule(
    session: Session,
    ctx: WorkspaceContext,
    *,
    reservation: Reservation,
    rule: StayLifecycleRule,
    unit_id: str | None,
) -> tuple[_Window, ...] | BundleDecision:
    check_in = _ensure_utc(reservation.check_in)
    check_out = _ensure_utc(reservation.check_out)
    duration = rule.duration

    if rule.trigger == "after_checkout":
        starts_at = check_out + timedelta(hours=rule.offset_hours or 0)
        next_check_in = _next_stay_check_in(
            session,
            ctx,
            reservation=reservation,
            unit_id=unit_id,
        )
        if next_check_in is None:
            return "skipped_no_next_stay"
        next_check_in_utc = _ensure_utc(next_check_in)
        if next_check_in_utc <= starts_at:
            return "skipped_zero_gap"
        due_by = next_check_in_utc
        ends_at = min(starts_at + duration, due_by)
        if _gap_intersects_closure(
            session,
            property_id=reservation.property_id,
            unit_id=unit_id,
            starts_at=starts_at,
            ends_at=ends_at,
        ):
            return "skipped_closure"
        return (_Window("after_checkout", starts_at, ends_at, due_by),)

    if rule.trigger == "before_checkin":
        starts_at = check_in - timedelta(hours=rule.offset_hours or 0)
        ends_at = starts_at + duration
        return (_Window("before_checkin", starts_at, ends_at, check_in),)

    if rule.rrule is None:
        return "skipped_no_rrule"
    rule_set = rrulestr(rule.rrule, dtstart=check_in)
    starts = [
        _ensure_utc(value)
        for value in rule_set.between(check_in, check_out, inc=True)
        if check_in <= _ensure_utc(value) < check_out
    ]
    return tuple(
        _Window(
            f"during_stay:{index}",
            start,
            start + duration,
            start + duration,
        )
        for index, start in enumerate(starts)
    )


def _create_bundle(
    session: Session,
    ctx: WorkspaceContext,
    *,
    reservation: Reservation,
    rule: StayLifecycleRule,
    now: datetime,
) -> StayBundle:
    bundle = StayBundle(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        reservation_id=reservation.id,
        kind=rule.kind,
        tasks_json=[
            {
                "rule_id": rule.id,
                "trigger": rule.trigger,
                "kind": rule.kind,
                "template_id": rule.template_id,
                "status": "creating",
            }
        ],
        created_at=now,
    )
    session.add(bundle)
    session.flush()
    return bundle


def _bundles_by_rule(
    session: Session,
    ctx: WorkspaceContext,
    *,
    reservation_id: str,
) -> dict[str, StayBundle]:
    by_rule: dict[str, StayBundle] = {}
    for bundle in list_bundles(session, ctx, reservation_id=reservation_id):
        rule_id = _bundle_rule_id(bundle)
        if rule_id is not None and rule_id not in by_rule:
            by_rule[rule_id] = bundle
    return by_rule


def _bundle_rule_id(bundle: StayBundle) -> str | None:
    for entry in bundle.tasks_json:
        value = entry.get("rule_id")
        if isinstance(value, str):
            return value
    return None


def _record_bundle_cancellation(bundle: StayBundle, *, reason: str) -> None:
    entries: list[dict[str, object]] = []
    for entry in bundle.tasks_json:
        next_entry = dict(entry)
        next_entry["cancellation_reason"] = reason
        entries.append(next_entry)
    bundle.tasks_json = entries


def _requires_regeneration(
    *,
    reservation: Reservation,
    rule: StayLifecycleRule,
    previous_check_in: datetime,
    previous_check_out: datetime,
) -> bool:
    if rule.trigger == "after_checkout":
        if reservation.status == "completed":
            return True
        return abs(
            _ensure_utc(reservation.check_out) - _ensure_utc(previous_check_out)
        ) >= (DEFAULT_PATCH_IN_PLACE_THRESHOLD)
    if rule.trigger == "before_checkin":
        return abs(
            _ensure_utc(reservation.check_in) - _ensure_utc(previous_check_in)
        ) >= (DEFAULT_PATCH_IN_PLACE_THRESHOLD)
    return True


def _active_matching_unit_rules(
    rules: tuple[StayLifecycleRule, ...],
    *,
    unit_id: str | None,
) -> tuple[StayLifecycleRule, ...]:
    return tuple(
        rule
        for rule in sorted(rules, key=lambda item: (item.ordinal, item.id))
        if rule.active and (rule.unit_id is None or rule.unit_id == unit_id)
    )


def _active_reservation(
    session: Session,
    ctx: WorkspaceContext,
    *,
    reservation_id: str,
) -> Reservation | None:
    reservation = _load_reservation(session, ctx, reservation_id=reservation_id)
    if reservation is None or reservation.status in {"cancelled", "completed"}:
        return None
    return reservation


def _resolve_context(
    session: Session,
    ctx: WorkspaceContext,
    *,
    resolver: ReservationContextResolver | None,
    reservation: Reservation,
) -> ReservationContext:
    resolved = resolver if resolver is not None else StaticReservationContextResolver()
    return resolved.resolve(session, ctx, reservation=reservation)


def _resolve_now(now: datetime | None) -> datetime:
    resolved = now if now is not None else datetime.now(UTC)
    if resolved.tzinfo is None or resolved.utcoffset() != timedelta(0):
        raise ValueError("now must be a timezone-aware datetime in UTC")
    return resolved
