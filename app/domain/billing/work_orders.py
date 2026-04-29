"""Billing work-order service."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from sqlalchemy.orm import Session

from app.audit import write_audit
from app.events import (
    EventBus,
    ShiftEnded,
    WorkOrderCompleted,
)
from app.events import (
    bus as default_bus,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "ShiftAccrualRow",
    "WorkOrderCreate",
    "WorkOrderInvalid",
    "WorkOrderNotFound",
    "WorkOrderOrganizationRow",
    "WorkOrderPatch",
    "WorkOrderPropertyRow",
    "WorkOrderRateCardRow",
    "WorkOrderRepository",
    "WorkOrderRow",
    "WorkOrderService",
    "WorkOrderShiftRow",
    "WorkOrderView",
    "handle_shift_ended",
    "register_shift_accrual_subscription",
]

_STATUS_VALUES = frozenset({"draft", "sent", "in_progress", "completed", "invoiced"})
_MUTABLE_FIELDS = frozenset({"title", "starts_at", "ends_at", "rate_card_id"})
_HOUR = Decimal("3600")
_CENT = Decimal("1")
_HUNDREDTH = Decimal("0.01")


class WorkOrderInvalid(ValueError):
    """The requested work-order mutation violates the billing contract."""


class WorkOrderNotFound(LookupError):
    """The work order or one of its scoped parents does not exist."""


@dataclass(frozen=True, slots=True)
class WorkOrderOrganizationRow:
    id: str
    workspace_id: str
    kind: str
    default_currency: str


@dataclass(frozen=True, slots=True)
class WorkOrderPropertyRow:
    id: str
    client_org_id: str | None
    default_currency: str | None


@dataclass(frozen=True, slots=True)
class WorkOrderRateCardRow:
    id: str
    workspace_id: str
    organization_id: str
    currency: str
    rates: Mapping[str, int]
    active_from: object
    active_to: object | None


@dataclass(frozen=True, slots=True)
class WorkOrderShiftRow:
    id: str
    workspace_id: str
    starts_at: datetime
    ends_at: datetime | None
    property_id: str | None


@dataclass(frozen=True, slots=True)
class WorkOrderRow:
    id: str
    workspace_id: str
    organization_id: str
    property_id: str
    title: str
    status: str
    starts_at: datetime
    ends_at: datetime | None
    rate_card_id: str | None
    total_hours_decimal: Decimal
    total_cents: int


@dataclass(frozen=True, slots=True)
class ShiftAccrualRow:
    id: str
    workspace_id: str
    work_order_id: str
    shift_id: str
    hours_decimal: Decimal
    hourly_rate_cents: int
    accrued_cents: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WorkOrderView:
    id: str
    workspace_id: str
    organization_id: str
    property_id: str
    title: str
    status: str
    starts_at: datetime
    ends_at: datetime | None
    rate_card_id: str | None
    total_hours_decimal: Decimal
    total_cents: int


@dataclass(frozen=True, slots=True)
class WorkOrderCreate:
    organization_id: str
    property_id: str
    title: str
    starts_at: datetime
    ends_at: datetime | None = None
    rate_card_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkOrderPatch:
    fields: Mapping[str, object | None]


class WorkOrderRepository(Protocol):
    @property
    def session(self) -> Session: ...

    def get_workspace_default_currency(self, *, workspace_id: str) -> str | None: ...

    def get_organization(
        self, *, workspace_id: str, organization_id: str, for_update: bool = False
    ) -> WorkOrderOrganizationRow | None: ...

    def get_property(
        self, *, workspace_id: str, property_id: str
    ) -> WorkOrderPropertyRow | None: ...

    def get_rate_card(
        self, *, workspace_id: str, organization_id: str, rate_card_id: str
    ) -> WorkOrderRateCardRow | None: ...

    def insert(
        self,
        *,
        work_order_id: str,
        workspace_id: str,
        organization_id: str,
        property_id: str,
        title: str,
        status: str,
        starts_at: datetime,
        ends_at: datetime | None,
        rate_card_id: str | None,
    ) -> WorkOrderRow: ...

    def get(
        self, *, workspace_id: str, work_order_id: str, for_update: bool = False
    ) -> WorkOrderRow | None: ...

    def list(
        self,
        *,
        workspace_id: str,
        organization_id: str | None,
        property_id: str | None,
        status: str | None,
    ) -> Sequence[WorkOrderRow]: ...

    def update_fields(
        self,
        *,
        workspace_id: str,
        work_order_id: str,
        fields: Mapping[str, object | None],
    ) -> WorkOrderRow: ...

    def get_shift(
        self, *, workspace_id: str, shift_id: str
    ) -> WorkOrderShiftRow | None: ...

    def open_for_property(
        self, *, workspace_id: str, property_id: str, for_update: bool = False
    ) -> Sequence[WorkOrderRow]: ...

    def append_shift_accrual(
        self,
        *,
        accrual_id: str,
        workspace_id: str,
        work_order_id: str,
        shift_id: str,
        hours_decimal: Decimal,
        hourly_rate_cents: int,
        accrued_cents: int,
        created_at: datetime,
    ) -> ShiftAccrualRow | None: ...


class WorkOrderService:
    """Workspace-scoped work-order use cases."""

    def __init__(
        self,
        ctx: WorkspaceContext,
        *,
        clock: Clock | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._ctx = ctx
        self._clock = clock if clock is not None else SystemClock()
        self._bus = event_bus if event_bus is not None else default_bus

    def create(self, repo: WorkOrderRepository, body: WorkOrderCreate) -> WorkOrderView:
        organization = self._get_client_organization(
            repo, body.organization_id, for_update=True
        )
        prop = self._get_property(repo, body.property_id)
        _validate_property_client(prop, organization.id)
        title = _clean_required(body.title, field="title")
        _validate_window(body.starts_at, body.ends_at)
        rate_card_id = _clean_optional(body.rate_card_id)
        if rate_card_id is not None:
            self._get_rate_card(repo, organization.id, rate_card_id)
        row = repo.insert(
            work_order_id=new_ulid(),
            workspace_id=self._ctx.workspace_id,
            organization_id=organization.id,
            property_id=prop.id,
            title=title,
            status="draft",
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            rate_card_id=rate_card_id,
        )
        view = _to_view(row)
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="work_order",
            entity_id=view.id,
            action="billing.work_order.created",
            diff={"after": _audit_shape(view)},
            clock=self._clock,
        )
        return view

    def list(
        self,
        repo: WorkOrderRepository,
        *,
        organization_id: str | None = None,
        property_id: str | None = None,
        status: str | None = None,
    ) -> list[WorkOrderView]:
        clean_status = _validate_status(status) if status is not None else None
        rows = repo.list(
            workspace_id=self._ctx.workspace_id,
            organization_id=_clean_optional(organization_id),
            property_id=_clean_optional(property_id),
            status=clean_status,
        )
        return [_to_view(row) for row in rows]

    def get(self, repo: WorkOrderRepository, work_order_id: str) -> WorkOrderView:
        return _to_view(self._get(repo, work_order_id))

    def update(
        self, repo: WorkOrderRepository, work_order_id: str, patch: WorkOrderPatch
    ) -> WorkOrderView:
        if not patch.fields:
            raise WorkOrderInvalid("PATCH body must include at least one field")
        unknown = sorted(set(patch.fields) - _MUTABLE_FIELDS)
        if unknown:
            raise WorkOrderInvalid(f"unknown work-order fields: {', '.join(unknown)}")
        current = self._get(repo, work_order_id, for_update=True)
        if current.status in {"completed", "invoiced"}:
            raise WorkOrderInvalid("completed work orders are locked")
        fields = self._normalize_patch(repo, current, patch)
        changed = {
            key: value
            for key, value in fields.items()
            if getattr(current, key) != value
        }
        if not changed:
            return _to_view(current)
        updated = repo.update_fields(
            workspace_id=self._ctx.workspace_id,
            work_order_id=current.id,
            fields=changed,
        )
        self._audit_update(repo, current, updated, changed)
        return _to_view(updated)

    def mark_in_progress(
        self, repo: WorkOrderRepository, work_order_id: str
    ) -> WorkOrderView:
        return self._transition(
            repo,
            work_order_id,
            target="in_progress",
            allowed={"draft", "sent"},
        )

    def complete(self, repo: WorkOrderRepository, work_order_id: str) -> WorkOrderView:
        current = self._get(repo, work_order_id, for_update=True)
        if current.status == "completed":
            return _to_view(current)
        updated = self._transition_row(
            repo,
            current,
            target="completed",
            allowed={"in_progress"},
        )
        self._bus.publish(
            WorkOrderCompleted(
                workspace_id=self._ctx.workspace_id,
                actor_id=self._ctx.actor_id,
                correlation_id=self._ctx.audit_correlation_id,
                occurred_at=self._clock.now(),
                work_order_id=updated.id,
                total_cents=updated.total_cents,
                total_hours_decimal=str(updated.total_hours_decimal),
            )
        )
        return _to_view(updated)

    def invoice(self, repo: WorkOrderRepository, work_order_id: str) -> WorkOrderView:
        return self._transition(
            repo,
            work_order_id,
            target="invoiced",
            allowed={"completed"},
        )

    def accrue_shift_ended(
        self, repo: WorkOrderRepository, event: ShiftEnded
    ) -> ShiftAccrualRow | None:
        if event.workspace_id != self._ctx.workspace_id:
            raise WorkOrderInvalid("shift event workspace does not match context")
        shift = repo.get_shift(workspace_id=event.workspace_id, shift_id=event.shift_id)
        if shift is None or shift.property_id is None or shift.ends_at is None:
            return None
        work_orders = list(
            repo.open_for_property(
                workspace_id=event.workspace_id,
                property_id=shift.property_id,
                for_update=True,
            )
        )
        if not work_orders:
            return None
        if len(work_orders) > 1:
            raise WorkOrderInvalid(
                f"multiple in-progress work orders for property {shift.property_id!r}"
            )
        work_order = work_orders[0]
        if work_order.rate_card_id is None:
            return None
        rate_card = self._get_rate_card(
            repo,
            work_order.organization_id,
            work_order.rate_card_id,
        )
        hourly_rate_cents = _single_hourly_rate(rate_card)
        hours_decimal = _hours_decimal(shift.starts_at, shift.ends_at)
        accrued_cents = _money_cents(hours_decimal, hourly_rate_cents)
        accrual = repo.append_shift_accrual(
            accrual_id=new_ulid(),
            workspace_id=event.workspace_id,
            work_order_id=work_order.id,
            shift_id=shift.id,
            hours_decimal=hours_decimal,
            hourly_rate_cents=hourly_rate_cents,
            accrued_cents=accrued_cents,
            created_at=self._clock.now(),
        )
        if accrual is not None:
            write_audit(
                repo.session,
                self._ctx,
                entity_kind="work_order",
                entity_id=work_order.id,
                action="billing.work_order.shift_accrued",
                diff={
                    "shift_id": shift.id,
                    "hours_decimal": str(hours_decimal),
                    "hourly_rate_cents": hourly_rate_cents,
                    "accrued_cents": accrued_cents,
                },
                clock=self._clock,
            )
        return accrual

    def _normalize_patch(
        self,
        repo: WorkOrderRepository,
        current: WorkOrderRow,
        patch: WorkOrderPatch,
    ) -> dict[str, object | None]:
        fields: dict[str, object | None] = {}
        for key, value in patch.fields.items():
            if key == "title":
                if not isinstance(value, str):
                    raise WorkOrderInvalid("title must be a string")
                fields[key] = _clean_required(value, field="title")
            elif key in {"starts_at", "ends_at"}:
                if value is not None and not isinstance(value, datetime):
                    raise WorkOrderInvalid(f"{key} must be a datetime or null")
                fields[key] = value
            elif key == "rate_card_id":
                if value is not None and not isinstance(value, str):
                    raise WorkOrderInvalid("rate_card_id must be a string or null")
                if current.total_hours_decimal > Decimal("0"):
                    raise WorkOrderInvalid("rate_card_id is locked after hours accrue")
                clean_id = _clean_optional(value)
                if clean_id is not None:
                    self._get_rate_card(repo, current.organization_id, clean_id)
                fields[key] = clean_id
        starts_at = fields.get("starts_at", current.starts_at)
        ends_at = fields.get("ends_at", current.ends_at)
        if not isinstance(starts_at, datetime):
            raise WorkOrderInvalid("starts_at must be a datetime")
        if ends_at is not None and not isinstance(ends_at, datetime):
            raise WorkOrderInvalid("ends_at must be a datetime or null")
        _validate_window(starts_at, ends_at)
        return fields

    def _transition(
        self,
        repo: WorkOrderRepository,
        work_order_id: str,
        *,
        target: str,
        allowed: set[str],
    ) -> WorkOrderView:
        current = self._get(repo, work_order_id, for_update=True)
        if current.status == target:
            return _to_view(current)
        return _to_view(
            self._transition_row(repo, current, target=target, allowed=allowed)
        )

    def _transition_row(
        self,
        repo: WorkOrderRepository,
        current: WorkOrderRow,
        *,
        target: str,
        allowed: set[str],
    ) -> WorkOrderRow:
        if current.status not in allowed:
            raise WorkOrderInvalid(
                f"cannot transition work order from {current.status!r} to {target!r}"
            )
        updated = repo.update_fields(
            workspace_id=self._ctx.workspace_id,
            work_order_id=current.id,
            fields={"status": target},
        )
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="work_order",
            entity_id=current.id,
            action="billing.work_order.state_changed",
            diff={
                "from": current.status,
                "to": target,
                "before": _audit_shape(_to_view(current)),
                "after": _audit_shape(_to_view(updated)),
            },
            clock=self._clock,
        )
        return updated

    def _get(
        self,
        repo: WorkOrderRepository,
        work_order_id: str,
        *,
        for_update: bool = False,
    ) -> WorkOrderRow:
        clean_id = _clean_required(work_order_id, field="work_order_id")
        row = repo.get(
            workspace_id=self._ctx.workspace_id,
            work_order_id=clean_id,
            for_update=for_update,
        )
        if row is None:
            raise WorkOrderNotFound("work order not found")
        return row

    def _get_client_organization(
        self,
        repo: WorkOrderRepository,
        organization_id: str,
        *,
        for_update: bool = False,
    ) -> WorkOrderOrganizationRow:
        clean_id = _clean_required(organization_id, field="organization_id")
        row = repo.get_organization(
            workspace_id=self._ctx.workspace_id,
            organization_id=clean_id,
            for_update=for_update,
        )
        if row is None:
            raise WorkOrderNotFound("organization not found")
        if row.kind == "vendor":
            raise WorkOrderInvalid("vendor-only organizations cannot own work orders")
        return row

    def _get_property(
        self, repo: WorkOrderRepository, property_id: str
    ) -> WorkOrderPropertyRow:
        clean_id = _clean_required(property_id, field="property_id")
        row = repo.get_property(
            workspace_id=self._ctx.workspace_id,
            property_id=clean_id,
        )
        if row is None:
            raise WorkOrderNotFound("property not found")
        return row

    def _get_rate_card(
        self,
        repo: WorkOrderRepository,
        organization_id: str,
        rate_card_id: str,
    ) -> WorkOrderRateCardRow:
        clean_id = _clean_required(rate_card_id, field="rate_card_id")
        row = repo.get_rate_card(
            workspace_id=self._ctx.workspace_id,
            organization_id=organization_id,
            rate_card_id=clean_id,
        )
        if row is None:
            raise WorkOrderNotFound("rate card not found")
        _single_hourly_rate(row)
        return row

    def _audit_update(
        self,
        repo: WorkOrderRepository,
        before: WorkOrderRow,
        after: WorkOrderRow,
        changed: Mapping[str, object | None],
    ) -> None:
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="work_order",
            entity_id=after.id,
            action="billing.work_order.updated",
            diff={
                "changed": sorted(changed),
                "before": _audit_shape(_to_view(before)),
                "after": _audit_shape(_to_view(after)),
            },
            clock=self._clock,
        )


SessionContextProvider = Callable[[ShiftEnded], tuple[Session, WorkspaceContext] | None]

_SUBSCRIBED_BUSES: set[int] = set()
_SUBSCRIBED_BUSES_LOCK = threading.Lock()


def register_shift_accrual_subscription(
    event_bus: EventBus,
    *,
    session_provider: SessionContextProvider,
    repository_factory: Callable[[Session], WorkOrderRepository],
    clock: Clock | None = None,
) -> None:
    """Subscribe shift-end accrual against a caller-provided UoW binding."""

    bus_id = id(event_bus)
    with _SUBSCRIBED_BUSES_LOCK:
        if bus_id in _SUBSCRIBED_BUSES:
            return
        _SUBSCRIBED_BUSES.add(bus_id)

    @event_bus.subscribe(ShiftEnded)
    def _on_shift_ended(event: ShiftEnded) -> None:
        bound = session_provider(event)
        if bound is None:
            return
        session, ctx = bound
        handle_shift_ended(
            event,
            repo=repository_factory(session),
            ctx=ctx,
            clock=clock,
            event_bus=event_bus,
        )


def handle_shift_ended(
    event: ShiftEnded,
    *,
    repo: WorkOrderRepository,
    ctx: WorkspaceContext,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> ShiftAccrualRow | None:
    return WorkOrderService(ctx, clock=clock, event_bus=event_bus).accrue_shift_ended(
        repo, event
    )


def _clean_required(value: str, *, field: str) -> str:
    clean = value.strip()
    if not clean:
        raise WorkOrderInvalid(f"{field} is required")
    return clean


def _clean_optional(value: object | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkOrderInvalid("id fields must be strings")
    clean = value.strip()
    return clean or None


def _validate_status(value: str) -> str:
    clean = value.strip()
    if clean not in _STATUS_VALUES:
        raise WorkOrderInvalid(f"unknown work-order status {value!r}")
    return clean


def _validate_window(starts_at: datetime, ends_at: datetime | None) -> None:
    if ends_at is not None and ends_at <= starts_at:
        raise WorkOrderInvalid("ends_at must be after starts_at")


def _validate_property_client(prop: WorkOrderPropertyRow, organization_id: str) -> None:
    if prop.client_org_id is not None and prop.client_org_id != organization_id:
        raise WorkOrderInvalid(
            "property client_org_id does not match work-order organization"
        )


def _single_hourly_rate(rate_card: WorkOrderRateCardRow) -> int:
    if "hourly" in rate_card.rates:
        return rate_card.rates["hourly"]
    if len(rate_card.rates) == 1:
        return next(iter(rate_card.rates.values()))
    raise WorkOrderInvalid(
        "rate card must include an 'hourly' rate or exactly one service rate"
    )


def _hours_decimal(starts_at: datetime, ends_at: datetime) -> Decimal:
    seconds = Decimal(str((ends_at - starts_at).total_seconds()))
    return (seconds / _HOUR).quantize(_HUNDREDTH, rounding=ROUND_HALF_UP)


def _money_cents(hours_decimal: Decimal, hourly_rate_cents: int) -> int:
    cents = (hours_decimal * Decimal(hourly_rate_cents)).quantize(
        _CENT, rounding=ROUND_HALF_UP
    )
    return int(cents)


def _to_view(row: WorkOrderRow) -> WorkOrderView:
    return WorkOrderView(
        id=row.id,
        workspace_id=row.workspace_id,
        organization_id=row.organization_id,
        property_id=row.property_id,
        title=row.title,
        status=row.status,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        rate_card_id=row.rate_card_id,
        total_hours_decimal=row.total_hours_decimal,
        total_cents=row.total_cents,
    )


def _audit_shape(view: WorkOrderView) -> dict[str, object]:
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "organization_id": view.organization_id,
        "property_id": view.property_id,
        "title": view.title,
        "status": view.status,
        "starts_at": view.starts_at.isoformat(),
        "ends_at": view.ends_at.isoformat() if view.ends_at is not None else None,
        "rate_card_id": view.rate_card_id,
        "total_hours_decimal": str(view.total_hours_decimal),
        "total_cents": view.total_cents,
    }
