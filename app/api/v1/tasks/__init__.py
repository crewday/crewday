"""Tasks context router package.

The public import path stays ``app.api.v1.tasks`` while the route
handlers live in subresource modules. Keep re-exports here for the
payload/helper names imported by tests and downstream code.
"""

from __future__ import annotations

from .cursor import _decode_comment_cursor, _encode_comment_cursor
from .derived import _compute_overdue, _compute_time_window_local, _humanize_rrule
from .payloads import (
    AssignmentPayload,
    ChecklistPatchRequest,
    CommentEditRequest,
    CommentListResponse,
    CommentPayload,
    CompleteRequest,
    EvidenceListResponse,
    EvidencePayload,
    InventoryEffectPayload,
    NlTaskCommitPayload,
    NlTaskCommitRequest,
    NlTaskPreviewPayload,
    NlTaskPreviewRequest,
    OccurrencePreviewItem,
    ReasonRequest,
    ResolvedInventoryEffectPayload,
    ScheduleListResponse,
    SchedulePayload,
    SchedulePreviewResponse,
    TaskChecklistItemPayload,
    TaskDetailInstructionPayload,
    TaskDetailPayload,
    TaskDetailPropertyPayload,
    TaskListResponse,
    TaskPayload,
    TaskStatePayload,
    TaskTemplateListResponse,
    TaskTemplatePayload,
)
from .router import router

__all__ = [
    "AssignmentPayload",
    "ChecklistPatchRequest",
    "CommentEditRequest",
    "CommentListResponse",
    "CommentPayload",
    "CompleteRequest",
    "EvidenceListResponse",
    "EvidencePayload",
    "InventoryEffectPayload",
    "NlTaskCommitPayload",
    "NlTaskCommitRequest",
    "NlTaskPreviewPayload",
    "NlTaskPreviewRequest",
    "OccurrencePreviewItem",
    "ReasonRequest",
    "ResolvedInventoryEffectPayload",
    "ScheduleListResponse",
    "SchedulePayload",
    "SchedulePreviewResponse",
    "TaskChecklistItemPayload",
    "TaskDetailInstructionPayload",
    "TaskDetailPayload",
    "TaskDetailPropertyPayload",
    "TaskListResponse",
    "TaskPayload",
    "TaskStatePayload",
    "TaskTemplateListResponse",
    "TaskTemplatePayload",
    "_compute_overdue",
    "_compute_time_window_local",
    "_decode_comment_cursor",
    "_encode_comment_cursor",
    "_humanize_rrule",
    "router",
]
