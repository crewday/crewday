"""Retry queued / failed ``email_delivery`` rows.

The task is deployment-scope but deliberately walks workspaces one at
a time so the repository SELECT carries ``workspace_id`` and can ride
``ix_email_delivery_workspace_state_sent``. Each row remains in the
existing §10 state machine: retries flip to ``sent`` on success, stay
``failed`` on transport / render failure, and get a dead-letter audit
once the retry budget is exhausted.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.messaging.repositories import SqlAlchemyEmailDeliveryRepository
from app.adapters.db.workspace.models import Workspace
from app.adapters.mail.ports import Mailer
from app.audit import write_audit
from app.domain.messaging.notifications import Jinja2TemplateLoader, TemplateLoader
from app.domain.messaging.ports import EmailDeliveryRow
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.worker.jobs.common import _system_actor_context

__all__ = [
    "BACKOFF_SCHEDULE_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_BATCH_SIZE",
    "EmailDeliveryRetryReport",
    "EmailDeliveryRetryTask",
    "retry_due_email_deliveries",
]

_log = logging.getLogger(__name__)

BACKOFF_SCHEDULE_SECONDS: Final[tuple[int, ...]] = (
    30,
    120,
    600,
    3600,
)
MAX_ATTEMPTS: Final[int] = 5
MAX_BATCH_SIZE: Final[int] = 200


@dataclass(frozen=True, slots=True)
class EmailDeliveryRetryReport:
    workspaces_considered: int
    attempted: int
    sent: int
    failed: int
    dead_lettered: int


@dataclass(frozen=True, slots=True)
class EmailDeliveryRetryTask:
    """Worker task that retries due ``email_delivery`` rows."""

    session: Session
    mailer: Mailer
    clock: Clock = field(default_factory=SystemClock)
    templates: TemplateLoader | None = None
    batch_size: int = MAX_BATCH_SIZE
    max_attempts: int = MAX_ATTEMPTS
    backoff_schedule_seconds: Sequence[int] = BACKOFF_SCHEDULE_SECONDS

    def run(self) -> EmailDeliveryRetryReport:
        now = self.clock.now()
        templates = (
            self.templates
            if self.templates is not None
            else Jinja2TemplateLoader.default()
        )
        repo = SqlAlchemyEmailDeliveryRepository(self.session)

        workspaces_considered = 0
        attempted = 0
        sent = 0
        failed = 0
        dead_lettered = 0

        for workspace_id, workspace_slug in _workspace_rows(self.session):
            if attempted >= self.batch_size:
                break
            workspaces_considered += 1
            ctx = _system_actor_context(
                workspace_id=workspace_id,
                workspace_slug=workspace_slug,
            )
            remaining = self.batch_size - attempted
            due_rows = repo.select_due_for_retry(
                workspace_id=workspace_id,
                now=now,
                backoff_schedule_seconds=self.backoff_schedule_seconds,
                max_attempts=self.max_attempts,
                limit=remaining,
            )
            for row in due_rows:
                attempted += 1
                try:
                    with self.session.begin_nested():
                        outcome = self._retry_one(
                            repo=repo,
                            ctx=ctx,
                            row=row,
                            templates=templates,
                        )
                except Exception:
                    failed += 1
                    _log.exception(
                        "email delivery retry row failed",
                        extra={
                            "event": "messaging.email_delivery.retry.row_error",
                            "delivery_id": row.id,
                            "workspace_id": row.workspace_id,
                        },
                    )
                    continue
                if outcome == "sent":
                    sent += 1
                elif outcome == "dead_lettered":
                    dead_lettered += 1
                elif outcome == "failed":
                    failed += 1

        report = EmailDeliveryRetryReport(
            workspaces_considered=workspaces_considered,
            attempted=attempted,
            sent=sent,
            failed=failed,
            dead_lettered=dead_lettered,
        )
        _log.info(
            "email delivery retry tick completed",
            extra={
                "event": "messaging.email_delivery.retry.tick",
                "workspaces_considered": report.workspaces_considered,
                "attempted": report.attempted,
                "sent": report.sent,
                "failed": report.failed,
                "dead_lettered": report.dead_lettered,
            },
        )
        return report

    def _retry_one(
        self,
        *,
        repo: SqlAlchemyEmailDeliveryRepository,
        ctx: WorkspaceContext,
        row: EmailDeliveryRow,
        templates: TemplateLoader,
    ) -> str:
        try:
            context = dict(row.context_snapshot_json)
            locale = _locale_from_context(context)
            subject = templates.render(
                kind=row.template_key,
                locale=locale,
                channel="subject",
                context=context,
            ).strip()
            body_md = templates.render(
                kind=row.template_key,
                locale=locale,
                channel="body_md",
                context=context,
            )
            provider_message_id = self.mailer.send(
                to=(row.to_email_at_send,),
                subject=subject,
                body_text=body_md,
                reply_to=None,
                headers={
                    "X-CrewDay-Email-Delivery-Id": row.id,
                    "X-CrewDay-Notification-Kind": row.template_key,
                },
            )
        except Exception as exc:
            failed = repo.mark_retry_failed(
                delivery_id=row.id,
                expected_retry_count=row.retry_count,
                error_text=str(exc),
                now=self.clock.now(),
                max_attempts=self.max_attempts,
            )
            if failed is None:
                return "no_op"
            _write_retry_audit(
                self.session,
                ctx,
                row=failed,
                outcome="failed",
                clock=self.clock,
                error=str(exc),
            )
            if failed.retry_count >= self.max_attempts:
                _write_dead_letter_audit(
                    self.session,
                    ctx,
                    row=failed,
                    max_attempts=self.max_attempts,
                    clock=self.clock,
                )
                return "dead_lettered"
            return "failed"

        sent = repo.mark_retry_sent(
            delivery_id=row.id,
            expected_retry_count=row.retry_count,
            provider_message_id=provider_message_id,
            sent_at=self.clock.now(),
        )
        if sent is None:
            return "no_op"
        _write_retry_audit(
            self.session,
            ctx,
            row=sent,
            outcome="sent",
            clock=self.clock,
            provider_message_id=provider_message_id,
        )
        return "sent"


def retry_due_email_deliveries(
    *,
    session: Session,
    mailer: Mailer,
    clock: Clock | None = None,
    templates: TemplateLoader | None = None,
    batch_size: int = MAX_BATCH_SIZE,
) -> EmailDeliveryRetryReport:
    task = EmailDeliveryRetryTask(
        session=session,
        mailer=mailer,
        clock=clock if clock is not None else SystemClock(),
        templates=templates,
        batch_size=batch_size,
    )
    return task.run()


def _workspace_rows(session: Session) -> tuple[tuple[str, str], ...]:
    # justification: retry worker enumerates workspaces deployment-wide.
    with tenant_agnostic():
        rows = session.execute(
            select(Workspace.id, Workspace.slug).order_by(Workspace.id.asc())
        ).all()
    return tuple((row.id, row.slug) for row in rows)


def _locale_from_context(context: dict[str, object]) -> str | None:
    raw = context.get("locale")
    if isinstance(raw, str) and raw:
        return raw
    return None


def _write_retry_audit(
    session: Session,
    ctx: WorkspaceContext,
    *,
    row: EmailDeliveryRow,
    outcome: str,
    clock: Clock,
    provider_message_id: str | None = None,
    error: str | None = None,
) -> None:
    diff: dict[str, object] = {
        "template_key": row.template_key,
        "retry_count": row.retry_count,
        "outcome": outcome,
    }
    if provider_message_id is not None:
        diff["provider_message_id"] = provider_message_id
    if error is not None:
        diff["error"] = error
    write_audit(
        session,
        ctx,
        entity_kind="email_delivery",
        entity_id=row.id,
        action="messaging.email_delivery.retry",
        diff=diff,
        via="worker",
        clock=clock,
    )


def _write_dead_letter_audit(
    session: Session,
    ctx: WorkspaceContext,
    *,
    row: EmailDeliveryRow,
    max_attempts: int,
    clock: Clock,
) -> None:
    # justification: retry worker audits a row from a deployment-wide sweep.
    with tenant_agnostic():
        existing = session.scalars(
            select(AuditLog.id)
            .where(AuditLog.workspace_id == ctx.workspace_id)
            .where(AuditLog.entity_kind == "email_delivery")
            .where(AuditLog.entity_id == row.id)
            .where(AuditLog.action == "messaging.email_delivery.dead_lettered")
            .limit(1)
        ).first()
    if existing is not None:
        return
    write_audit(
        session,
        ctx,
        entity_kind="email_delivery",
        entity_id=row.id,
        action="messaging.email_delivery.dead_lettered",
        diff={
            "template_key": row.template_key,
            "retry_count": row.retry_count,
            "max_attempts": max_attempts,
        },
        via="worker",
        clock=clock,
    )
