"""Natural-language task intake routes."""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.domain.llm.budget import BudgetExceeded
from app.domain.llm.capabilities.tasks_intake import (
    NlCommitEdits,
    NlIntakeContext,
    NlPreviewExpired,
    NlPreviewNotFound,
    NlPreviewUnresolved,
    TaskIntakeParseError,
)
from app.domain.llm.capabilities.tasks_intake import commit as commit_nl_task
from app.domain.llm.capabilities.tasks_intake import draft as draft_nl_task
from app.domain.llm.router import CapabilityUnassignedError
from app.domain.tasks.schedules import InvalidBackupWorkRole, InvalidRRule

from .deps import _agent_attribution_from_request, _Ctx, _Db, _Llm
from .errors import _http, _http_for_nl_task_intake, _http_for_schedule_mutation
from .payloads import (
    NlTaskCommitPayload,
    NlTaskCommitRequest,
    NlTaskPreviewPayload,
    NlTaskPreviewRequest,
)

router = APIRouter()


@router.post(
    "/from_nl",
    response_model=NlTaskPreviewPayload,
    operation_id="draft_task_from_nl",
    summary="Draft a task schedule from natural language",
)
def draft_task_from_nl_route(
    body: NlTaskPreviewRequest,
    request: Request,
    ctx: _Ctx,
    session: _Db,
    llm: _Llm,
) -> NlTaskPreviewPayload:
    """Dry-run natural-language task intake; no task rows are created."""
    if not body.dry_run:
        raise _http(422, "dry_run_required")
    try:
        preview = draft_nl_task(
            NlIntakeContext(
                session=session,
                workspace_ctx=ctx,
                llm=llm,
                attribution=_agent_attribution_from_request(session, ctx, request),
            ),
            body.text,
        )
    except (BudgetExceeded, CapabilityUnassignedError, TaskIntakeParseError) as exc:
        raise _http_for_nl_task_intake(exc) from exc
    return NlTaskPreviewPayload.from_preview(preview)


@router.post(
    "/from_nl/commit",
    status_code=status.HTTP_201_CREATED,
    response_model=NlTaskCommitPayload,
    operation_id="commit_task_from_nl",
    summary="Commit a natural-language task preview",
)
def commit_task_from_nl_route(
    body: NlTaskCommitRequest,
    request: Request,
    ctx: _Ctx,
    session: _Db,
) -> NlTaskCommitPayload:
    """Confirm an NL preview and create the template + schedule.

    ``Idempotency-Key`` replay is handled by the process-wide middleware.
    """
    try:
        scheduled = commit_nl_task(
            NlIntakeContext(
                session=session,
                workspace_ctx=ctx,
                llm=None,
                attribution=_agent_attribution_from_request(session, ctx, request),
            ),
            body.preview_id,
            NlCommitEdits(
                resolved=body.resolved,
                assumptions=body.assumptions,
                ambiguities=body.ambiguities,
            ),
        )
    except (
        NlPreviewNotFound,
        NlPreviewExpired,
        NlPreviewUnresolved,
        TaskIntakeParseError,
    ) as exc:
        raise _http_for_nl_task_intake(exc) from exc
    except (InvalidRRule, InvalidBackupWorkRole, ValueError) as exc:
        raise _http_for_schedule_mutation(exc) from exc
    return NlTaskCommitPayload.from_scheduled(scheduled)
