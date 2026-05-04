"""Messaging scheduler job bodies."""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import select

from app.adapters.db.session import make_uow
from app.config import get_settings
from app.util.clock import Clock
from app.worker.jobs.common import _demo_expired_workspace_ids, _system_actor_context

_log = logging.getLogger("app.worker.scheduler")


def _make_daily_digest_fanout_body(clock: Clock) -> Callable[[], None]:
    """Build the hourly daily-digest fan-out body (cd-f0ue)."""

    from app.adapters.llm.openrouter import (
        DeploymentOpenRouterConfigSource,
        OpenRouterClient,
    )
    from app.adapters.mail.smtp import SMTPMailer
    from app.adapters.mail.smtp_config import (
        DeploymentSmtpConfigSource,
        SmtpConfig,
        SmtpConfigError,
    )

    settings = get_settings()
    root_key = getattr(settings, "root_key", None)
    smtp_source = DeploymentSmtpConfigSource(
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
        root_key=root_key,
    )
    mailer = SMTPMailer(config_source=smtp_source)
    llm = (
        OpenRouterClient(
            DeploymentOpenRouterConfigSource(
                env_api_key=settings.openrouter_api_key,
                root_key=root_key,
            )
        )
        if settings.openrouter_api_key is not None or root_key is not None
        else None
    )

    def _body() -> None:
        from sqlalchemy.orm import Session as _Session

        from app.adapters.db.workspace.models import Workspace
        from app.tenancy import tenant_agnostic
        from app.worker.tasks.daily_digest import send_daily_digest

        try:
            smtp_config = smtp_source.config()
        except SmtpConfigError as exc:
            _log.warning(
                "daily digest skipped: SMTP config unavailable",
                extra={
                    "event": "worker.daily_digest.skipped_no_smtp",
                    "reason": str(exc),
                },
            )
            return
        if smtp_config.host is None or smtp_config.from_addr is None:
            _log.warning(
                "daily digest skipped: SMTP not configured",
                extra={"event": "worker.daily_digest.skipped_no_smtp"},
            )
            return

        now = clock.now()

        total_workspaces = 0
        total_workspaces_skipped = 0
        total_workspaces_failed = 0
        total_recipients_considered = 0
        total_sent = 0
        total_skipped_not_due = 0
        total_skipped_empty = 0
        total_skipped_existing = 0
        total_llm_rendered = 0
        total_template_rendered = 0

        with make_uow() as session:
            assert isinstance(session, _Session)

            with tenant_agnostic():
                rows = list(session.execute(select(Workspace.id, Workspace.slug)).all())
                workspace_ids = [row.id for row in rows]
                expired_ids = _demo_expired_workspace_ids(
                    session, workspace_ids, now=now
                )

            for row in rows:
                workspace_id = row.id
                workspace_slug = row.slug
                total_workspaces += 1

                if workspace_id in expired_ids:
                    total_workspaces_skipped += 1
                    continue

                ctx = _system_actor_context(
                    workspace_id=workspace_id,
                    workspace_slug=workspace_slug,
                )
                try:
                    with session.begin_nested():
                        report = send_daily_digest(
                            ctx,
                            session=session,
                            mailer=mailer,
                            llm=llm,
                            clock=clock,
                            due_local_hour=7,
                        )
                except Exception as exc:
                    total_workspaces_failed += 1
                    _log.warning(
                        "daily digest failed for workspace",
                        extra={
                            "event": "worker.daily_digest.workspace.failed",
                            "workspace_id": workspace_id,
                            "workspace_slug": workspace_slug,
                            "error": type(exc).__name__,
                        },
                    )
                    continue

                total_recipients_considered += report.recipients_considered
                total_sent += report.sent
                total_skipped_not_due += report.skipped_not_due
                total_skipped_empty += report.skipped_empty
                total_skipped_existing += report.skipped_existing
                total_llm_rendered += report.llm_rendered
                total_template_rendered += report.template_rendered

                _log.info(
                    "daily digest ran for workspace",
                    extra={
                        "event": "worker.daily_digest.workspace.tick",
                        "workspace_id": workspace_id,
                        "workspace_slug": workspace_slug,
                        "recipients_considered": report.recipients_considered,
                        "sent": report.sent,
                        "skipped_not_due": report.skipped_not_due,
                        "skipped_empty": report.skipped_empty,
                        "skipped_existing": report.skipped_existing,
                        "llm_rendered": report.llm_rendered,
                        "template_rendered": report.template_rendered,
                    },
                )

        _log.info(
            "daily digest tick summary",
            extra={
                "event": "worker.daily_digest.tick.summary",
                "total_workspaces": total_workspaces,
                "total_workspaces_skipped": total_workspaces_skipped,
                "total_workspaces_failed": total_workspaces_failed,
                "total_recipients_considered": total_recipients_considered,
                "total_sent": total_sent,
                "total_skipped_not_due": total_skipped_not_due,
                "total_skipped_empty": total_skipped_empty,
                "total_skipped_existing": total_skipped_existing,
                "total_llm_rendered": total_llm_rendered,
                "total_template_rendered": total_template_rendered,
            },
        )

    return _body
