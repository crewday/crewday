"""Inventory reorder worker and item-change subscriber."""

from __future__ import annotations

import logging
import weakref
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.session import make_uow
from app.adapters.db.workspace.models import Workspace
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import InventoryItemChanged
from app.services.inventory.reorder_service import check_reorder_points
from app.tenancy import reset_current, set_current, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.util.clock import Clock, SystemClock

__all__ = [
    "InventoryReorderTickReport",
    "check_reorder_points_for_all_workspaces",
    "register_inventory_reorder_subscriber",
]

_log = logging.getLogger(__name__)
_SYSTEM_ACTOR_ZERO_ULID = "00000000000000000000000000"
_REGISTERED_BUSES: weakref.WeakSet[EventBus] = weakref.WeakSet()


@dataclass(frozen=True, slots=True)
class InventoryReorderTickReport:
    total_workspaces: int = 0
    total_workspaces_failed: int = 0
    checked_items: int = 0
    tasks_created: int = 0
    events_emitted: int = 0


def check_reorder_points_for_all_workspaces(
    *,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> InventoryReorderTickReport:
    """Run the hourly reorder check once for every workspace."""
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    total_workspaces = 0
    failed = 0
    checked = 0
    created = 0
    emitted = 0

    with make_uow() as session:
        assert isinstance(session, Session)
        with tenant_agnostic():
            rows = list(session.execute(select(Workspace.id, Workspace.slug)).all())

        for row in rows:
            total_workspaces += 1
            ctx = _system_actor_context(
                workspace_id=row.id,
                workspace_slug=row.slug,
            )
            token = set_current(ctx)
            try:
                try:
                    with session.begin_nested():
                        report = check_reorder_points(
                            session,
                            ctx,
                            clock=resolved_clock,
                            event_bus=resolved_bus,
                        )
                except Exception as exc:
                    failed += 1
                    _log.warning(
                        "inventory reorder failed for workspace",
                        extra={
                            "event": "worker.inventory_reorder.workspace.failed",
                            "workspace_id": row.id,
                            "workspace_slug": row.slug,
                            "error": type(exc).__name__,
                        },
                    )
                    continue
            finally:
                reset_current(token)

            checked += report.checked_items
            created += report.tasks_created
            emitted += report.events_emitted
            _log.info(
                "inventory reorder ran for workspace",
                extra={
                    "event": "worker.inventory_reorder.workspace.tick",
                    "workspace_id": row.id,
                    "workspace_slug": row.slug,
                    "checked_items": report.checked_items,
                    "tasks_created": report.tasks_created,
                    "events_emitted": report.events_emitted,
                },
            )

    return InventoryReorderTickReport(
        total_workspaces=total_workspaces,
        total_workspaces_failed=failed,
        checked_items=checked,
        tasks_created=created,
        events_emitted=emitted,
    )


def register_inventory_reorder_subscriber(
    *,
    event_bus: EventBus | None = None,
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    clock: Clock | None = None,
) -> None:
    """Subscribe incremental reorder checks to inventory item changes."""
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    if resolved_bus in _REGISTERED_BUSES:
        return
    _REGISTERED_BUSES.add(resolved_bus)

    @resolved_bus.subscribe(InventoryItemChanged)
    def _on_inventory_item_changed(event: InventoryItemChanged) -> None:
        resolved_clock = clock if clock is not None else SystemClock()
        if session_factory is not None:
            with session_factory() as session:
                _check_changed_item(session, event, resolved_clock, resolved_bus)
            return
        with make_uow() as raw_session:
            if not isinstance(raw_session, Session):
                raise TypeError("inventory reorder subscriber requires a Session")
            _check_changed_item(
                raw_session,
                event,
                resolved_clock,
                resolved_bus,
            )


def _check_changed_item(
    session: Session,
    event: InventoryItemChanged,
    clock: Clock,
    event_bus: EventBus,
) -> None:
    with tenant_agnostic():
        slug = session.scalar(
            select(Workspace.slug).where(Workspace.id == event.workspace_id)
        )
    if slug is None:
        return
    ctx = _system_actor_context(
        workspace_id=event.workspace_id,
        workspace_slug=slug,
    )
    token = set_current(ctx)
    try:
        check_reorder_points(
            session,
            ctx,
            item_ids=(event.item_id,),
            clock=clock,
            event_bus=event_bus,
        )
    finally:
        reset_current(token)


def _system_actor_context(
    *,
    workspace_id: str,
    workspace_slug: str,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=_SYSTEM_ACTOR_ZERO_ULID,
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=_SYSTEM_ACTOR_ZERO_ULID,
        principal_kind="system",
    )
