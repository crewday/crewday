"""Agent scheduler job bodies."""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import select

from app.adapters.db.session import make_uow
from app.config import get_settings
from app.util.clock import Clock
from app.worker.jobs.common import _system_actor_context

_log = logging.getLogger("app.worker.scheduler")


def _make_agent_compaction_body(clock: Clock) -> Callable[[], None]:
    """Build the conversation-compaction worker tick (cd-cn7v)."""

    from app.adapters.llm.openrouter import (
        DeploymentOpenRouterConfigSource,
        OpenRouterClient,
    )

    settings = get_settings()
    root_key = getattr(settings, "root_key", None)
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
        from app.domain.agent.compaction import compact_due_threads
        from app.tenancy import tenant_agnostic
        from app.tenancy.current import reset_current, set_current

        if llm is None:
            _log.warning(
                "agent compaction skipped: LLM unavailable",
                extra={"event": "worker.agent.compaction.skipped_no_llm"},
            )
            return

        workspaces_attempted = 0
        workspaces_failed = 0
        summaries_written = 0

        with make_uow() as session:
            assert isinstance(session, _Session)
            with tenant_agnostic():
                rows = list(session.execute(select(Workspace.id, Workspace.slug)).all())

            for row in rows:
                workspaces_attempted += 1
                ctx = _system_actor_context(
                    workspace_id=row.id,
                    workspace_slug=row.slug,
                )
                token = set_current(ctx)
                try:
                    try:
                        with session.begin_nested():
                            results = compact_due_threads(
                                session,
                                ctx,
                                llm_client=llm,
                                clock=clock,
                            )
                    except Exception as exc:
                        workspaces_failed += 1
                        _log.warning(
                            "agent compaction failed for workspace",
                            extra={
                                "event": "worker.agent.compaction.workspace_failed",
                                "workspace_id": row.id,
                                "workspace_slug": row.slug,
                                "error": type(exc).__name__,
                            },
                        )
                        continue
                finally:
                    reset_current(token)
                summaries_written += len(results)
                if results:
                    _log.info(
                        "agent compaction ran for workspace",
                        extra={
                            "event": "worker.agent.compaction.workspace_tick",
                            "workspace_id": row.id,
                            "workspace_slug": row.slug,
                            "summaries_written": len(results),
                        },
                    )

        _log.info(
            "agent compaction tick summary",
            extra={
                "event": "worker.agent.compaction.tick.summary",
                "workspaces_attempted": workspaces_attempted,
                "workspaces_failed": workspaces_failed,
                "summaries_written": summaries_written,
            },
        )

    return _body
