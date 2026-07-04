"""Upcoming-stay notification sweep."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.messaging.audiences import list_owner_user_ids
from app.adapters.db.messaging.models import Notification
from app.adapters.db.messaging.repositories import SqlAlchemyEmailDeliveryRepository
from app.adapters.db.stays.models import Reservation
from app.adapters.db.tasks.models import Occurrence
from app.adapters.mail.null import NullMailer
from app.domain.messaging.notifications import NotificationKind, NotificationService
from app.domain.stays.notifications import StayUpcomingView, notify_stay_upcoming
from app.tenancy import WorkspaceContext, tenant_agnostic

STAY_UPCOMING_LOOKAHEAD: timedelta = timedelta(hours=24)
_ASSIGNEE_NOTIFICATION_STATES: tuple[str, ...] = (
    "scheduled",
    "pending",
    "in_progress",
    "overdue",
)


@dataclass(frozen=True, slots=True)
class StayUpcomingReport:
    stays_walked: int
    notifications_sent: int


def emit_upcoming_stay_notifications(
    ctx: WorkspaceContext,
    *,
    session: Session,
    now: datetime,
    lookahead: timedelta = STAY_UPCOMING_LOOKAHEAD,
) -> StayUpcomingReport:
    rows = _upcoming_stays(session, ctx=ctx, now=now, window_end=now + lookahead)
    notifications_sent = 0
    sink = NotificationService(
        session=session,
        ctx=ctx,
        mailer=NullMailer(),
        email_deliveries=SqlAlchemyEmailDeliveryRepository(session),
    )
    owner_ids = list_owner_user_ids(session, workspace_id=ctx.workspace_id)
    for stay in rows:
        recipient_ids = tuple(
            sorted(
                set(owner_ids).union(
                    _assigned_worker_user_ids(session, ctx=ctx, stay_id=stay.id)
                )
            )
        )
        pending = tuple(
            user_id
            for user_id in recipient_ids
            if not _already_notified(session, stay=stay, recipient_user_id=user_id)
        )
        notify_stay_upcoming(stay=stay, recipient_user_ids=pending, sink=sink)
        notifications_sent += len(pending)
    return StayUpcomingReport(
        stays_walked=len(rows),
        notifications_sent=notifications_sent,
    )


def _upcoming_stays(
    session: Session,
    *,
    ctx: WorkspaceContext,
    now: datetime,
    window_end: datetime,
) -> tuple[StayUpcomingView, ...]:
    # justification: worker read keyed by an explicit
    # Reservation.workspace_id predicate, not the ambient tenant filter.
    with tenant_agnostic():
        rows = session.scalars(
            select(Reservation)
            .where(
                Reservation.workspace_id == ctx.workspace_id,
                Reservation.status == "scheduled",
                Reservation.check_in > now,
                Reservation.check_in <= window_end,
            )
            .order_by(Reservation.check_in, Reservation.id)
        ).all()
    return tuple(
        StayUpcomingView(
            id=row.id,
            workspace_id=row.workspace_id,
            property_id=row.property_id,
            check_in=row.check_in,
            check_out=row.check_out,
            guest_name=row.guest_name,
            guest_count=row.guest_count,
            status=row.status,
            source=row.source,
        )
        for row in rows
    )


def _assigned_worker_user_ids(
    session: Session, *, ctx: WorkspaceContext, stay_id: str
) -> tuple[str, ...]:
    # justification: worker read keyed by an explicit
    # Occurrence.workspace_id predicate, not the ambient tenant filter.
    with tenant_agnostic():
        user_ids = session.scalars(
            select(Occurrence.assignee_user_id)
            .where(
                Occurrence.workspace_id == ctx.workspace_id,
                Occurrence.reservation_id == stay_id,
                Occurrence.assignee_user_id.is_not(None),
                Occurrence.state.in_(_ASSIGNEE_NOTIFICATION_STATES),
            )
            .distinct()
        ).all()
    return tuple(sorted(user_id for user_id in user_ids if user_id is not None))


def _already_notified(
    session: Session, *, stay: StayUpcomingView, recipient_user_id: str
) -> bool:
    # justification: worker read keyed by an explicit
    # Notification.workspace_id predicate, not the ambient tenant filter.
    with tenant_agnostic():
        rows = session.scalars(
            select(Notification).where(
                Notification.workspace_id == stay.workspace_id,
                Notification.recipient_user_id == recipient_user_id,
                Notification.kind == NotificationKind.STAY_UPCOMING.value,
            )
        ).all()
    check_in = stay.check_in.isoformat()
    return any(
        row.payload_json.get("stay_id") == stay.id
        and row.payload_json.get("check_in") == check_in
        for row in rows
    )
