"""Daily digest worker.

Builds one recipient-local daily digest for each active workspace
member, sends it through :class:`app.domain.messaging.notifications.
NotificationService`, and writes a ``digest_record`` idempotency row.

The LLM path is enrichment only. The prompt contains aggregate counts
and state labels, never recipient names, emails, property names, or
task titles; the rendered email can include operational details from
the local database without sending those details upstream.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import User
from app.adapters.db.llm.models import ApprovalRequest
from app.adapters.db.messaging.models import DigestRecord
from app.adapters.db.payroll.models import PayPeriod
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.tasks.models import Occurrence, TaskApproval
from app.adapters.db.workspace.models import Workspace
from app.adapters.llm.ports import LLMClient
from app.adapters.mail.ports import Mailer
from app.domain.llm.budget import (
    PricingTable,
    default_pricing_table,
)
from app.domain.llm.capabilities.digest_gen import (
    DIGEST_COMPOSE_CAPABILITY,
    DigestComposeContext,
)
from app.domain.llm.capabilities.digest_gen import (
    compose as compose_digest_prose,
)
from app.domain.llm.router import ModelPick, resolve_model
from app.domain.messaging.notifications import NotificationKind, NotificationService
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.tenancy import WorkspaceContext, reset_current, set_current, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "DAILY_DIGEST_CAPABILITY",
    "DEFAULT_ALWAYS_SEND_EMPTY_DIGEST",
    "DailyDigestReport",
    "DigestRecipient",
    "DigestSnapshot",
    "send_daily_digest",
]


DAILY_DIGEST_CAPABILITY = DIGEST_COMPOSE_CAPABILITY
DEFAULT_ALWAYS_SEND_EMPTY_DIGEST = False

DigestModelResolver = Callable[[Session, WorkspaceContext, Clock], Sequence[ModelPick]]


@dataclass(frozen=True, slots=True)
class DigestRecipient:
    user_id: str
    email: str
    display_name: str
    locale: str | None
    timezone: str
    role: str


@dataclass(frozen=True, slots=True)
class DigestSnapshot:
    local_day: date
    timezone: str
    role: str
    scheduled_count: int
    overdue_count: int
    pending_task_approval_count: int
    pending_agent_approval_count: int
    pay_period_state: str | None
    task_titles: tuple[str, ...]

    @property
    def has_actionable_items(self) -> bool:
        return (
            self.scheduled_count
            + self.overdue_count
            + self.pending_task_approval_count
            + self.pending_agent_approval_count
        ) > 0


@dataclass(frozen=True, slots=True)
class DailyDigestReport:
    recipients_considered: int
    sent: int
    skipped_not_due: int
    skipped_empty: int
    skipped_existing: int
    llm_rendered: int
    template_rendered: int


def send_daily_digest(
    ctx: WorkspaceContext,
    *,
    session: Session,
    mailer: Mailer,
    llm: LLMClient | None = None,
    clock: Clock | None = None,
    pricing: PricingTable | None = None,
    always_send_empty: bool = DEFAULT_ALWAYS_SEND_EMPTY_DIGEST,
    bus: EventBus | None = None,
    resolve_models: DigestModelResolver | None = None,
    due_local_hour: int | None = None,
) -> DailyDigestReport:
    """Send daily digests for every eligible recipient in ``ctx``'s workspace.

    The preference surface for per-user "always send an empty digest"
    does not exist yet, so the default is explicit and conservative:
    empty-day recipients are skipped unless the caller opts the whole
    invocation into ``always_send_empty=True``.
    """

    resolved_clock = clock if clock is not None else SystemClock()
    resolved_pricing = pricing if pricing is not None else default_pricing_table()
    model_resolver = resolve_models if resolve_models is not None else _resolve_models
    now = resolved_clock.now()

    recipients_considered = 0
    sent = 0
    skipped_not_due = 0
    skipped_empty = 0
    skipped_existing = 0
    llm_rendered = 0
    template_rendered = 0

    token = set_current(ctx)
    try:
        recipients = _eligible_recipients(session, ctx=ctx)
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=mailer,
            clock=resolved_clock,
            bus=bus if bus is not None else default_event_bus,
        )
        for recipient in recipients:
            recipients_considered += 1
            if due_local_hour is not None and not _recipient_is_due(
                now,
                recipient.timezone,
                due_local_hour=due_local_hour,
            ):
                skipped_not_due += 1
                continue

            period_start, period_end = _local_day_window(now, recipient.timezone)
            if _digest_exists(
                session,
                workspace_id=ctx.workspace_id,
                recipient_user_id=recipient.user_id,
                period_start=period_start,
            ):
                skipped_existing += 1
                continue

            snapshot = _snapshot_for(
                session,
                recipient=recipient,
                period_start=period_start,
                period_end=period_end,
            )
            if not snapshot.has_actionable_items and not always_send_empty:
                skipped_empty += 1
                continue

            digest_record = _reserve_digest_record(
                session,
                ctx=ctx,
                recipient_user_id=recipient.user_id,
                period_start=period_start,
                period_end=period_end,
                clock=resolved_clock,
            )
            if digest_record is None:
                skipped_existing += 1
                continue

            subject = _fallback_subject(snapshot)
            llm_prose = _try_llm_prose(
                session,
                ctx=ctx,
                snapshot=snapshot,
                recipient_user_id=recipient.user_id,
                llm=llm,
                clock=resolved_clock,
                pricing=resolved_pricing,
                resolve_models=model_resolver,
            )
            if llm_prose is None:
                body_md = _fallback_body(snapshot, prose=None)
                template_rendered += 1
            else:
                body_md = _fallback_body(snapshot, prose=llm_prose)
                llm_rendered += 1

            service.notify(
                recipient_user_id=recipient.user_id,
                kind=NotificationKind.DAILY_DIGEST,
                payload={
                    "subject": subject,
                    "body_md": body_md,
                    "local_day": snapshot.local_day.isoformat(),
                    "timezone": snapshot.timezone,
                    "scheduled_count": snapshot.scheduled_count,
                    "overdue_count": snapshot.overdue_count,
                    "pending_task_approval_count": (
                        snapshot.pending_task_approval_count
                    ),
                    "pending_agent_approval_count": (
                        snapshot.pending_agent_approval_count
                    ),
                    "pay_period_state": snapshot.pay_period_state,
                },
            )
            digest_record.body_md = body_md
            digest_record.sent_at = resolved_clock.now()
            session.flush()
            sent += 1
    finally:
        reset_current(token)

    return DailyDigestReport(
        recipients_considered=recipients_considered,
        sent=sent,
        skipped_not_due=skipped_not_due,
        skipped_empty=skipped_empty,
        skipped_existing=skipped_existing,
        llm_rendered=llm_rendered,
        template_rendered=template_rendered,
    )


def _resolve_models(
    session: Session,
    ctx: WorkspaceContext,
    clock: Clock,
) -> Sequence[ModelPick]:
    return resolve_model(session, ctx, DAILY_DIGEST_CAPABILITY, clock=clock)


def _eligible_recipients(
    session: Session,
    *,
    ctx: WorkspaceContext,
) -> tuple[DigestRecipient, ...]:
    rows: dict[str, DigestRecipient] = {}
    default_timezone = _workspace_timezone(session, workspace_id=ctx.workspace_id)
    manager_timezone = _manager_timezone(session, workspace_id=ctx.workspace_id)

    role_rows = session.execute(
        select(
            User.id,
            User.email,
            User.display_name,
            User.locale,
            User.timezone,
            RoleGrant.grant_role,
        )
        .join(RoleGrant, RoleGrant.user_id == User.id)
        .where(RoleGrant.workspace_id == ctx.workspace_id)
        .where(RoleGrant.scope_kind == "workspace")
        .where(RoleGrant.grant_role.in_(("manager", "worker")))
        # cd-x1xh: do not mail a soft-retired user their daily digest.
        .where(RoleGrant.revoked_at.is_(None))
    ).all()
    for row in role_rows:
        role = str(row.grant_role)
        timezone = (
            manager_timezone if role == "manager" else row.timezone or default_timezone
        )
        rows[str(row.id)] = DigestRecipient(
            user_id=str(row.id),
            email=str(row.email),
            display_name=str(row.display_name),
            locale=row.locale,
            timezone=timezone,
            role=role,
        )

    owner_rows = session.execute(
        select(User.id, User.email, User.display_name, User.locale, User.timezone)
        .join(PermissionGroupMember, PermissionGroupMember.user_id == User.id)
        .join(PermissionGroup, PermissionGroup.id == PermissionGroupMember.group_id)
        .where(PermissionGroup.workspace_id == ctx.workspace_id)
        .where(PermissionGroup.slug == "owners")
    ).all()
    for owner_row in owner_rows:
        rows[str(owner_row.id)] = DigestRecipient(
            user_id=str(owner_row.id),
            email=str(owner_row.email),
            display_name=str(owner_row.display_name),
            locale=owner_row.locale,
            timezone=manager_timezone,
            role="manager",
        )

    return tuple(rows.values())


def _workspace_timezone(session: Session, *, workspace_id: str) -> str:
    with tenant_agnostic():
        timezone = session.scalar(
            select(Workspace.default_timezone).where(Workspace.id == workspace_id)
        )
    return timezone or "UTC"


def _manager_timezone(session: Session, *, workspace_id: str) -> str:
    row = session.execute(
        select(Property.timezone)
        .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
        .where(PropertyWorkspace.workspace_id == workspace_id)
        .order_by(PropertyWorkspace.created_at)
        .limit(1)
    ).first()
    if row is None:
        return _workspace_timezone(session, workspace_id=workspace_id)
    return str(row.timezone or _workspace_timezone(session, workspace_id=workspace_id))


def _local_day_window(now: datetime, timezone_name: str) -> tuple[datetime, datetime]:
    zone = _zoneinfo_or_utc(timezone_name)
    local_day = now.astimezone(zone).date()
    start_local = datetime.combine(local_day, time.min, tzinfo=zone)
    end_local = datetime.combine(local_day, time.max, tzinfo=zone)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _zoneinfo_or_utc(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _recipient_is_due(
    now: datetime,
    timezone_name: str,
    *,
    due_local_hour: int,
) -> bool:
    local_now = now.astimezone(_zoneinfo_or_utc(timezone_name))
    return local_now.hour >= due_local_hour


def _digest_exists(
    session: Session,
    *,
    workspace_id: str,
    recipient_user_id: str,
    period_start: datetime,
) -> bool:
    existing = session.scalar(
        select(DigestRecord.id)
        .where(DigestRecord.workspace_id == workspace_id)
        .where(DigestRecord.recipient_user_id == recipient_user_id)
        .where(DigestRecord.period_start == period_start)
        .where(DigestRecord.kind == "daily")
        .limit(1)
    )
    return existing is not None


def _reserve_digest_record(
    session: Session,
    *,
    ctx: WorkspaceContext,
    recipient_user_id: str,
    period_start: datetime,
    period_end: datetime,
    clock: Clock,
) -> DigestRecord | None:
    record = DigestRecord(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        recipient_user_id=recipient_user_id,
        period_start=period_start,
        period_end=period_end,
        kind="daily",
        body_md="",
        sent_at=None,
    )
    try:
        with session.begin_nested():
            session.add(record)
            session.flush()
    except IntegrityError:
        return None
    return record


def _snapshot_for(
    session: Session,
    *,
    recipient: DigestRecipient,
    period_start: datetime,
    period_end: datetime,
) -> DigestSnapshot:
    occurrence_stmt = _occurrence_base(period_start=period_start, period_end=period_end)
    if recipient.role == "worker":
        occurrence_stmt = occurrence_stmt.where(
            Occurrence.assignee_user_id == recipient.user_id
        )

    occurrences = session.scalars(occurrence_stmt.order_by(Occurrence.starts_at)).all()
    scheduled = [row for row in occurrences if row.state != "overdue"]
    overdue_count = _overdue_count(
        session,
        recipient=recipient,
        period_end=period_end,
    )
    pending_task_approval_count = (
        0 if recipient.role == "worker" else _pending_task_approval_count(session)
    )
    pending_agent_approval_count = (
        0 if recipient.role == "worker" else _pending_agent_approval_count(session)
    )
    pay_period_state = _pay_period_state(
        session,
        period_start=period_start,
        period_end=period_end,
    )
    return DigestSnapshot(
        local_day=period_start.astimezone(_zoneinfo_or_utc(recipient.timezone)).date(),
        timezone=recipient.timezone,
        role=recipient.role,
        scheduled_count=len(scheduled),
        overdue_count=overdue_count,
        pending_task_approval_count=pending_task_approval_count,
        pending_agent_approval_count=pending_agent_approval_count,
        pay_period_state=pay_period_state,
        task_titles=tuple(_title_for(row) for row in scheduled[:8]),
    )


def _occurrence_base(
    *,
    period_start: datetime,
    period_end: datetime,
) -> Select[tuple[Occurrence]]:
    return (
        select(Occurrence)
        .where(Occurrence.starts_at >= period_start)
        .where(Occurrence.starts_at <= period_end)
        .where(Occurrence.state.in_(("scheduled", "pending", "in_progress", "overdue")))
    )


def _overdue_count(
    session: Session,
    *,
    recipient: DigestRecipient,
    period_end: datetime,
) -> int:
    stmt = (
        select(func.count())
        .select_from(Occurrence)
        .where(Occurrence.state == "overdue")
        .where(
            or_(
                Occurrence.overdue_since.is_(None),
                Occurrence.overdue_since <= period_end,
            )
        )
        .where(Occurrence.ends_at <= period_end)
    )
    if recipient.role == "worker":
        stmt = stmt.where(Occurrence.assignee_user_id == recipient.user_id)
    count = session.scalar(stmt)
    return int(count or 0)


def _pending_task_approval_count(session: Session) -> int:
    count = session.scalar(
        select(func.count())
        .select_from(TaskApproval)
        .where(TaskApproval.state == "pending")
    )
    return int(count or 0)


def _pending_agent_approval_count(session: Session) -> int:
    count = session.scalar(
        select(func.count())
        .select_from(ApprovalRequest)
        .where(ApprovalRequest.status == "pending")
    )
    return int(count or 0)


def _pay_period_state(
    session: Session,
    *,
    period_start: datetime,
    period_end: datetime,
) -> str | None:
    row = session.execute(
        select(PayPeriod.state)
        .where(PayPeriod.starts_at <= period_end)
        .where(PayPeriod.ends_at >= period_start)
        .order_by(PayPeriod.starts_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return str(row.state)


def _title_for(row: Occurrence) -> str:
    title = (row.title or "").strip()
    return title or "Untitled task"


def _try_llm_prose(
    session: Session,
    *,
    ctx: WorkspaceContext,
    snapshot: DigestSnapshot,
    recipient_user_id: str,
    llm: LLMClient | None,
    clock: Clock,
    pricing: PricingTable,
    resolve_models: DigestModelResolver,
) -> str | None:
    if llm is None:
        return None
    try:
        model_chain = resolve_models(session, ctx, clock)
    except Exception:
        return None
    if not model_chain:
        return None

    try:
        prose = compose_digest_prose(
            DigestComposeContext(
                session=session,
                workspace_ctx=ctx,
                llm=llm,
                model_chain=tuple(model_chain),
                pricing=pricing,
                clock=clock,
            ),
            recipient_user_id,
            _structured_payload(snapshot),
        )
    except Exception:
        return None
    if prose.used_fallback:
        return None
    return prose.body_md.strip() or None


def _structured_payload(snapshot: DigestSnapshot) -> dict[str, object]:
    return {
        "role": snapshot.role,
        "local_day": snapshot.local_day.isoformat(),
        "scheduled_tasks": snapshot.scheduled_count,
        "overdue_tasks": snapshot.overdue_count,
        "pending_task_approvals": snapshot.pending_task_approval_count,
        "pending_agent_approvals": snapshot.pending_agent_approval_count,
        "pay_period_state": snapshot.pay_period_state or "none",
    }


def _fallback_subject(snapshot: DigestSnapshot) -> str:
    return f"Daily digest for {snapshot.local_day.isoformat()}"


def _fallback_body(snapshot: DigestSnapshot, *, prose: str | None) -> str:
    lines: list[str] = [
        f"## Daily digest for {snapshot.local_day.isoformat()}",
        "",
    ]
    if prose is not None:
        lines.extend([prose, ""])
    lines.extend(
        [
            f"- Scheduled tasks: {snapshot.scheduled_count}",
            f"- Overdue tasks: {snapshot.overdue_count}",
            f"- Pending task approvals: {snapshot.pending_task_approval_count}",
            f"- Pending agent approvals: {snapshot.pending_agent_approval_count}",
            f"- Pay period: {snapshot.pay_period_state or 'none'}",
        ]
    )
    if snapshot.task_titles:
        lines.extend(
            ("", "### Today", *[f"- {title}" for title in snapshot.task_titles])
        )
    return "\n".join(lines)
