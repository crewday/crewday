"""Domain exception to HTTP exception mapping for task routes."""

from __future__ import annotations

from fastapi import HTTPException, status

from app.domain.llm.budget import BudgetExceeded
from app.domain.llm.capabilities.tasks_intake import (
    NlPreviewExpired,
    NlPreviewNotFound,
    NlPreviewUnresolved,
    TaskIntakeParseError,
)
from app.domain.llm.router import CapabilityUnassignedError
from app.domain.tasks.assignment import TaskAlreadyAssigned
from app.domain.tasks.assignment import TaskNotFound as AssignTaskNotFound
from app.domain.tasks.comments import (
    CommentAttachmentInvalid,
    CommentEditWindowExpired,
    CommentKindForbidden,
    CommentMentionAmbiguous,
    CommentMentionInvalid,
    CommentNotEditable,
    CommentNotFound,
)
from app.domain.tasks.completion import (
    EvidenceRequired,
    InvalidStateTransition,
    PhotoForbidden,
    RequiredChecklistIncomplete,
    SkipNotPermitted,
)
from app.domain.tasks.completion import PermissionDenied as CompletionPermissionDenied
from app.domain.tasks.completion import TaskNotFound as CompletionTaskNotFound
from app.domain.tasks.oneoff import PersonalAssignmentError, TaskFieldInvalid
from app.domain.tasks.oneoff import TaskNotFound as OneOffTaskNotFound
from app.domain.tasks.schedules import (
    InvalidBackupWorkRole,
    InvalidRRule,
    ScheduleNotFound,
)
from app.domain.tasks.templates import (
    ScopeInconsistent,
    TaskTemplateNotFound,
    TemplateInUseError,
)


def _http(status_code: int, error: str, **extra: object) -> HTTPException:
    """Construct the ``{"error": "<code>", ...}`` detail envelope."""
    detail: dict[str, object] = {"error": error}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


def _template_not_found() -> HTTPException:
    return _http(status.HTTP_404_NOT_FOUND, "task_template_not_found")


def _schedule_not_found() -> HTTPException:
    return _http(status.HTTP_404_NOT_FOUND, "schedule_not_found")


def _task_not_found() -> HTTPException:
    return _http(status.HTTP_404_NOT_FOUND, "task_not_found")


def _comment_not_found() -> HTTPException:
    return _http(status.HTTP_404_NOT_FOUND, "comment_not_found")


def _http_for_nl_task_intake(exc: Exception) -> HTTPException:
    """Map NL task-intake exceptions to their HTTP shape."""
    if isinstance(exc, NlPreviewNotFound):
        return _http(status.HTTP_404_NOT_FOUND, "preview_not_found")
    if isinstance(exc, NlPreviewExpired):
        return _http(status.HTTP_410_GONE, "preview_expired")
    if isinstance(exc, NlPreviewUnresolved):
        return _http(
            422,
            "preview_unresolved",
            ambiguities=[
                ambiguity.model_dump(mode="json") for ambiguity in exc.ambiguities
            ],
        )
    if isinstance(exc, CapabilityUnassignedError):
        return _http(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "capability_unassigned",
            capability=exc.capability,
        )
    if isinstance(exc, BudgetExceeded):
        return HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=exc.to_dict(),
        )
    if isinstance(exc, TaskIntakeParseError):
        return _http(422, "task_intake_parse_error", message=str(exc))
    return _http(500, "internal")


def _http_for_template_mutation(exc: Exception) -> HTTPException:
    """Map a template-domain exception to its HTTP shape."""
    if isinstance(exc, TaskTemplateNotFound):
        return _template_not_found()
    if isinstance(exc, TemplateInUseError):
        return _http(
            status.HTTP_409_CONFLICT,
            "template_in_use",
            schedule_ids=list(exc.schedule_ids),
            stay_lifecycle_rule_ids=list(exc.stay_lifecycle_rule_ids),
        )
    if isinstance(exc, ScopeInconsistent):
        return _http(422, "scope_inconsistent", message=str(exc))
    return _http(500, "internal")


def _http_for_schedule_mutation(exc: Exception) -> HTTPException:
    """Map a schedule-domain exception to its HTTP shape."""
    if isinstance(exc, ScheduleNotFound):
        return _schedule_not_found()
    if isinstance(exc, InvalidRRule):
        return _http(422, "invalid_rrule", message=str(exc))
    if isinstance(exc, InvalidBackupWorkRole):
        return _http(
            422,
            "backup_invalid_work_role",
            invalid_user_ids=list(exc.invalid_user_ids),
            role_id=exc.role_id,
        )
    if isinstance(exc, ValueError):
        # Fallthrough ValueError covers the "template_id unknown"
        # case the service raises during create/update (see
        # :func:`app.domain.tasks.schedules._load_template`). Surface
        # as 422 with a dedicated code so the SPA can branch.
        return _http(422, "invalid_schedule_payload", message=str(exc))
    return _http(500, "internal")


def _http_for_task_mutation(exc: Exception) -> HTTPException:
    """Map a task-domain exception (state machine + evidence) to HTTP."""
    if isinstance(
        exc, OneOffTaskNotFound | CompletionTaskNotFound | AssignTaskNotFound
    ):
        return _task_not_found()
    if isinstance(exc, TaskTemplateNotFound):
        return _template_not_found()
    if isinstance(exc, PersonalAssignmentError):
        return _http(422, "personal_assignment_invalid", message=str(exc))
    if isinstance(exc, TaskFieldInvalid):
        return _http(
            422,
            "invalid_task_field",
            field=exc.field,
            value=exc.value,
            message=str(exc),
        )
    if isinstance(exc, InvalidStateTransition):
        return _http(
            status.HTTP_409_CONFLICT,
            "invalid_state_transition",
            current=exc.current,
            target=exc.target,
        )
    if isinstance(exc, RequiredChecklistIncomplete):
        return _http(
            422,
            "required_checklist_incomplete",
            unchecked_ids=list(exc.unchecked_ids),
        )
    if isinstance(exc, PhotoForbidden):
        return _http(422, "photo_forbidden", message=str(exc))
    if isinstance(exc, EvidenceRequired):
        return _http(422, "evidence_required", message=str(exc))
    if isinstance(exc, SkipNotPermitted):
        return _http(status.HTTP_403_FORBIDDEN, "skip_not_permitted")
    if isinstance(exc, CompletionPermissionDenied):
        return _http(status.HTTP_403_FORBIDDEN, "permission_denied")
    if isinstance(exc, TaskAlreadyAssigned):
        return _http(422, "task_already_assigned", message=str(exc))
    return _http(500, "internal")


def _http_for_comment_mutation(exc: Exception) -> HTTPException:
    """Map a comment-domain exception to its HTTP shape."""
    if isinstance(exc, CommentNotFound):
        return _comment_not_found()
    if isinstance(exc, CommentKindForbidden):
        return _http(status.HTTP_403_FORBIDDEN, "comment_kind_forbidden")
    if isinstance(exc, CommentEditWindowExpired):
        return _http(status.HTTP_409_CONFLICT, "comment_edit_window_expired")
    if isinstance(exc, CommentNotEditable):
        return _http(status.HTTP_409_CONFLICT, "comment_not_editable")
    if isinstance(exc, CommentMentionInvalid):
        return _http(
            422,
            "comment_mention_invalid",
            unknown_slugs=list(exc.unknown_slugs),
        )
    if isinstance(exc, CommentMentionAmbiguous):
        return _http(
            422,
            "comment_mention_ambiguous",
            ambiguous_slugs=list(exc.ambiguous_slugs),
        )
    if isinstance(exc, CommentAttachmentInvalid):
        return _http(
            422,
            "comment_attachment_invalid",
            unknown_ids=list(exc.unknown_ids),
        )
    return _http(500, "internal")
