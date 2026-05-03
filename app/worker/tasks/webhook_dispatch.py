"""``dispatch_due_webhooks`` — outbound delivery worker tick (cd-q885).

Walks every ``webhook_delivery`` row whose ``status='pending'`` and
``next_attempt_at <= now``, opens a fresh UoW per row, and fires one
HTTP POST attempt through :func:`app.domain.integrations.webhooks.deliver`.

Cross-tenant by design — like the approval-TTL sweep, the dispatcher
is deployment-scope (NOT per-workspace):

1. Every ``webhook_delivery`` row carries its own ``workspace_id``;
   the dispatcher reads under :func:`tenant_agnostic` and the
   per-row deliver call writes its audit (on dead-letter) keyed off
   that ``workspace_id``.
2. The retry schedule is a global policy. Per-workspace fan-out
   would scale linearly with workspace count and dominate a fleet's
   worker budget; one global sweep is O(n) in the count of due rows.

Per-row UoW so a single misbehaving subscription cannot roll back
the entire sweep: every attempt commits independently, and the audit
row on dead-letter rides the same UoW as the row's status flip.

A failed dispatch raises out of the wrapper; the scheduler's
:func:`~app.worker.scheduler.wrap_job` catches + logs the
exception, the heartbeat row stops advancing, and ``/readyz`` goes
red via the staleness window. The next tick (:data:`WEBHOOK_DISPATCH_INTERVAL_SECONDS`
later) retries.

See ``docs/specs/10-messaging-notifications.md`` §"Webhooks
(outbound)" and ``docs/specs/16-deployment-operations.md`` §"Worker
process".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import User
from app.adapters.db.integrations.models import WebhookDelivery
from app.adapters.db.integrations.repositories import (
    SqlAlchemyWebhookRepository,
)
from app.adapters.db.messaging.repositories import SqlAlchemyEmailDeliveryRepository
from app.adapters.db.secrets.repositories import (
    SqlAlchemySecretEnvelopeRepository,
)
from app.adapters.db.session import make_uow
from app.adapters.db.workspace.models import Workspace
from app.adapters.mail.null import NullMailer
from app.adapters.mail.ports import MailDeliveryError, Mailer
from app.adapters.mail.smtp import SMTPMailer
from app.adapters.mail.smtp_config import DeploymentSmtpConfigSource, SmtpConfig
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.config import Settings, get_settings
from app.domain.integrations.ports import WebhookSubscriptionRow
from app.domain.integrations.webhooks import (
    DELIVERY_PENDING,
    DeliveryReport,
    WebhookHealthThresholds,
    deliver,
    pause_unhealthy_subscriptions,
)
from app.domain.messaging.notifications import NotificationKind, NotificationService
from app.events.bus import bus as default_event_bus
from app.tenancy import reset_current, set_current, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.util.clock import Clock, SystemClock

__all__ = [
    "DispatchReport",
    "dispatch_due_webhooks",
    "pause_unhealthy_webhook_subscriptions",
]


_log = logging.getLogger(__name__)

_LOG_EVENT: Final[str] = "webhook.dispatch.tick"


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """Summary of one :func:`dispatch_due_webhooks` call.

    ``successes`` / ``retries`` / ``dead_lettered`` are counts of
    per-row outcomes the tick produced. ``processed_ids`` is the
    full set of delivery ids touched — useful for tests pinning
    determinism on a fixture set.
    """

    processed_count: int
    successes: int
    retries: int
    dead_lettered: int
    auto_paused: int
    processed_ids: tuple[str, ...]


def _select_due(session: Session, *, now: datetime) -> tuple[str, ...]:
    """Return delivery ids whose retry window has opened.

    Filters for ``status='pending'`` and ``next_attempt_at <= now``.
    Ordered by ``next_attempt_at`` ascending so older overdue rows
    fire first; a fleet returning from a long pause clears the
    backlog in time order.
    """
    with tenant_agnostic():
        stmt = (
            select(WebhookDelivery.id)
            .where(WebhookDelivery.status == DELIVERY_PENDING)
            .where(WebhookDelivery.next_attempt_at <= now)
            .order_by(WebhookDelivery.next_attempt_at.asc())
        )
        return tuple(session.scalars(stmt))


def dispatch_due_webhooks(*, clock: Clock | None = None) -> DispatchReport:
    """Run one dispatch sweep across the deployment.

    For every due delivery row, opens a fresh UoW (the worker has no
    ambient session) and calls :func:`deliver`. Each attempt commits
    independently — a misbehaving subscription cannot roll back the
    sweep's progress on its peers.

    The clock is injectable for tests; production passes ``None``
    and falls back to :class:`SystemClock`.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    settings = get_settings()
    if settings.root_key is None:
        # No root key → cannot decrypt subscription secrets. Bail
        # noisily so /readyz catches the misconfig; the heartbeat
        # stops advancing and the operator dashboard surfaces it.
        _log.warning(
            "webhook dispatcher: root key is unset; skipping tick",
            extra={"event": "webhook.dispatch.skipped_no_root_key"},
        )
        return DispatchReport(
            processed_count=0,
            successes=0,
            retries=0,
            dead_lettered=0,
            auto_paused=0,
            processed_ids=(),
        )

    auto_paused = pause_unhealthy_webhook_subscriptions(clock=resolved_clock)

    # First read — gather the due ids in one short UoW; we then
    # process each in its own UoW so a long sweep doesn't pin a
    # transaction across every dispatch.
    with make_uow() as session:
        assert isinstance(session, Session)
        due_ids = _select_due(session, now=now)

    successes = 0
    retries = 0
    dead_lettered = 0
    processed: list[str] = []
    for delivery_id in due_ids:
        try:
            report = _dispatch_one(delivery_id, clock=resolved_clock)
        except Exception:
            # Per-row failure is logged + counted as a retry-side
            # event. We swallow here so the rest of the sweep proceeds.
            _log.exception(
                "webhook dispatch row failed",
                extra={
                    "event": "webhook.dispatch.row_error",
                    "delivery_id": delivery_id,
                },
            )
            retries += 1
            processed.append(delivery_id)
            continue

        processed.append(delivery_id)
        if report.dead_lettered:
            dead_lettered += 1
        elif report.status == "succeeded":
            successes += 1
        else:
            retries += 1

    _log.info(
        "webhook dispatch tick completed",
        extra={
            "event": _LOG_EVENT,
            "processed_count": len(processed),
            "successes": successes,
            "retries": retries,
            "dead_lettered": dead_lettered,
            "auto_paused": auto_paused,
        },
    )
    return DispatchReport(
        processed_count=len(processed),
        successes=successes,
        retries=retries,
        dead_lettered=dead_lettered,
        auto_paused=auto_paused,
        processed_ids=tuple(processed),
    )


def pause_unhealthy_webhook_subscriptions(*, clock: Clock | None = None) -> int:
    """Pause active subscriptions whose recent attempts are all non-2xx."""
    resolved_clock = clock if clock is not None else SystemClock()
    settings = get_settings()
    thresholds = WebhookHealthThresholds(
        window_h=settings.webhook_health_window_h,
        min_deliveries=settings.webhook_health_min_deliveries,
    )
    mailer = _auto_pause_mailer(settings)

    with make_uow() as session:
        assert isinstance(session, Session)
        repo = SqlAlchemyWebhookRepository(session)

        def _notify(
            subscription: WebhookSubscriptionRow,
            delivery_count: int,
            used_thresholds: WebhookHealthThresholds,
        ) -> None:
            _notify_managers_auto_paused(
                session,
                subscription=subscription,
                delivery_count=delivery_count,
                thresholds=used_thresholds,
                mailer=mailer,
                clock=resolved_clock,
            )

        paused = pause_unhealthy_subscriptions(
            session,
            repo=repo,
            thresholds=thresholds,
            notify_managers=_notify,
            clock=resolved_clock,
        )
        return len(paused)


def _dispatch_one(delivery_id: str, *, clock: Clock) -> DeliveryReport:
    """Open a fresh UoW and dispatch one delivery.

    The cipher + repository are wired here so each row's commit is
    independent. Settings are resolved per call; the lookup is
    cached by :func:`get_settings`.
    """
    settings = get_settings()
    if settings.root_key is None:
        # The outer sweep already gates on root_key; defensive
        # re-check here would be redundant. Raise so a programming
        # bug (someone hot-reloaded settings between the gate and
        # this call) surfaces noisily.
        raise RuntimeError("root_key is unset; cannot dispatch")

    with make_uow() as session:
        assert isinstance(session, Session)
        secret_repo = SqlAlchemySecretEnvelopeRepository(session)
        envelope = Aes256GcmEnvelope(
            settings.root_key, repository=secret_repo, clock=clock
        )
        webhook_repo = SqlAlchemyWebhookRepository(session)
        return deliver(
            session,
            delivery_id=delivery_id,
            repo=webhook_repo,
            envelope=envelope,
            clock=clock,
        )


def _notify_managers_auto_paused(
    session: Session,
    *,
    subscription: WebhookSubscriptionRow,
    delivery_count: int,
    thresholds: WebhookHealthThresholds,
    mailer: Mailer,
    clock: Clock,
) -> None:
    ctx = _system_ctx(session, workspace_id=subscription.workspace_id)
    manager_ids = _manager_user_ids(session, workspace_id=subscription.workspace_id)
    if not manager_ids:
        _log.warning(
            "webhook subscription auto-paused with no manager recipient",
            extra={
                "event": "webhook.subscription.auto_paused.no_manager",
                "workspace_id": subscription.workspace_id,
                "subscription_id": subscription.id,
            },
        )
        return

    token = set_current(ctx)
    try:
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=mailer,
            clock=clock,
            bus=default_event_bus,
            email_deliveries=SqlAlchemyEmailDeliveryRepository(session),
        )
        payload = {
            "subscription_id": subscription.id,
            "subscription_name": subscription.name,
            "subscription_url": subscription.url,
            "paused_reason": subscription.paused_reason,
            "paused_at": subscription.paused_at.isoformat()
            if subscription.paused_at is not None
            else None,
            "webhook_health_window_h": thresholds.window_h,
            "webhook_health_min_deliveries": thresholds.min_deliveries,
            "observed_deliveries": delivery_count,
        }
        for manager_id in manager_ids:
            try:
                service.notify(
                    recipient_user_id=manager_id,
                    kind=NotificationKind.WEBHOOK_AUTO_PAUSED,
                    payload=payload,
                )
            except MailDeliveryError:
                _log.exception(
                    "webhook auto-pause email notification failed",
                    extra={
                        "event": "webhook.subscription.auto_paused.email_failed",
                        "workspace_id": subscription.workspace_id,
                        "subscription_id": subscription.id,
                        "recipient_user_id": manager_id,
                    },
                )
    finally:
        reset_current(token)


def _manager_user_ids(session: Session, *, workspace_id: str) -> tuple[str, ...]:
    with tenant_agnostic():
        manager_rows = session.scalars(
            select(User.id)
            .join(RoleGrant, RoleGrant.user_id == User.id)
            .where(RoleGrant.workspace_id == workspace_id)
            .where(RoleGrant.scope_kind == "workspace")
            .where(RoleGrant.grant_role == "manager")
            .where(RoleGrant.revoked_at.is_(None))
        )
        owner_rows = session.scalars(
            select(User.id)
            .join(PermissionGroupMember, PermissionGroupMember.user_id == User.id)
            .join(PermissionGroup, PermissionGroup.id == PermissionGroupMember.group_id)
            .where(PermissionGroup.workspace_id == workspace_id)
            .where(PermissionGroup.slug == "owners")
        )
        return tuple(dict.fromkeys([*manager_rows, *owner_rows]))


def _system_ctx(session: Session, *, workspace_id: str) -> WorkspaceContext:
    with tenant_agnostic():
        slug = session.scalar(
            select(Workspace.slug).where(Workspace.id == workspace_id)
        )
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=str(slug or ""),
        actor_id="00000000000000000000000000",
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id="00000000000000000000000000",
        principal_kind="system",
    )


def _auto_pause_mailer(settings: Settings) -> Mailer:
    if settings.smtp_host is None or settings.smtp_from is None:
        return NullMailer()
    return SMTPMailer(
        config_source=DeploymentSmtpConfigSource(
            env=SmtpConfig(
                host=settings.smtp_host,
                port=settings.smtp_port,
                from_addr=settings.smtp_from,
                user=settings.smtp_user,
                password=settings.smtp_password,
                use_tls=settings.smtp_use_tls,
                timeout=settings.smtp_timeout,
                bounce_domain=settings.smtp_bounce_domain,
            ),
            root_key=settings.root_key,
        )
    )
