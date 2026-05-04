"""Demo-mode worker job bodies."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.base import Base
from app.adapters.db.demo.models import DemoWorkspace
from app.adapters.db.session import make_uow
from app.adapters.db.workspace.models import Workspace
from app.config import Settings
from app.domain.llm.budget import refresh_aggregate
from app.tenancy.current import reset_current, set_current, tenant_agnostic
from app.util.clock import Clock
from app.worker.jobs.common import _system_actor_context

__all__ = [
    "DemoGcReport",
    "_make_demo_gc_body",
    "_make_demo_usage_rollup_body",
    "count_workspace_id_orphans",
    "purge_expired_demo_workspaces",
]

_log = logging.getLogger("app.worker.scheduler")


@dataclass(frozen=True, slots=True)
class DemoGcReport:
    """Summary of one demo GC sweep."""

    purged: int
    workspace_ids: tuple[str, ...]


def _make_demo_gc_body(settings: Settings, clock: Clock) -> Callable[[], None]:
    """Build the 15 min demo-GC job body."""

    def _body() -> None:
        report = purge_expired_demo_workspaces(settings=settings, clock=clock)
        _log.info(
            "demo garbage collection tick summary",
            extra={
                "event": "demo.gc.tick",
                "purged": report.purged,
                "workspace_ids": report.workspace_ids,
            },
        )

    return _body


def _make_demo_usage_rollup_body(clock: Clock) -> Callable[[], None]:
    """Build the demo-only 60 s usage rollup body."""

    def _body() -> None:
        attempted = 0
        failures = 0
        total_cents = 0
        with make_uow() as session:
            assert isinstance(session, Session)
            with tenant_agnostic():
                rows = list(
                    session.execute(
                        select(Workspace.id, Workspace.slug).join(
                            DemoWorkspace,
                            DemoWorkspace.id == Workspace.id,
                        )
                    ).all()
                )
            for row in rows:
                attempted += 1
                ctx = _system_actor_context(
                    workspace_id=row.id,
                    workspace_slug=row.slug,
                )
                token = set_current(ctx)
                try:
                    try:
                        with session.begin_nested():
                            total_cents += refresh_aggregate(session, ctx, clock=clock)
                    except Exception as exc:
                        failures += 1
                        _log.warning(
                            "demo usage rollup failed for workspace",
                            extra={
                                "event": "demo.usage_rollup.workspace_failed",
                                "workspace_id": row.id,
                                "error": type(exc).__name__,
                            },
                        )
                finally:
                    reset_current(token)
        _log.info(
            "demo usage rollup tick summary",
            extra={
                "event": "demo.usage_rollup.tick",
                "workspaces": attempted,
                "failures": failures,
                "total_cents": total_cents,
            },
        )

    return _body


def purge_expired_demo_workspaces(
    *,
    settings: Settings,
    clock: Clock,
) -> DemoGcReport:
    """Delete expired demo workspaces and their per-workspace upload dir."""
    now = clock.now()
    with make_uow() as session:
        assert isinstance(session, Session)
        with tenant_agnostic():
            ids = tuple(
                session.scalars(
                    select(DemoWorkspace.id)
                    .where(DemoWorkspace.expires_at < now)
                    .order_by(DemoWorkspace.expires_at.asc())
                )
            )
            for workspace_id in ids:
                workspace = session.get(Workspace, workspace_id)
                if workspace is not None:
                    session.delete(workspace)
    for workspace_id in ids:
        _remove_demo_upload_dir(settings.data_dir, workspace_id)
    return DemoGcReport(purged=len(ids), workspace_ids=ids)


def count_workspace_id_orphans(session: Session) -> int:
    """Return the number of workspace-scoped rows with no workspace parent."""
    total = 0
    workspace_table = Workspace.__table__
    with tenant_agnostic():
        for table in Base.metadata.sorted_tables:
            if table is workspace_table or "workspace_id" not in table.c:
                continue
            workspace_id = table.c.workspace_id
            stmt = (
                select(func.count())
                .select_from(
                    table.outerjoin(
                        workspace_table,
                        workspace_id == workspace_table.c.id,
                    )
                )
                .where(workspace_id.is_not(None), workspace_table.c.id.is_(None))
            )
            total += int(session.execute(stmt).scalar_one())
    return total


def _remove_demo_upload_dir(data_dir: Path, workspace_id: str) -> None:
    target = data_dir / "demo" / workspace_id
    try:
        target.relative_to(data_dir)
    except ValueError:
        raise RuntimeError(
            f"refusing to delete path outside data_dir: {target}"
        ) from None
    shutil.rmtree(target, ignore_errors=True)
