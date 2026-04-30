"""Pydantic request and response models for task routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.db.tasks.models import ChecklistItem
from app.domain.llm.capabilities.tasks_intake import (
    Ambiguity,
    NlPreview,
    ResolvedTask,
    ScheduledTask,
)
from app.domain.tasks.comments import CommentView
from app.domain.tasks.completion import EvidenceView, TaskState
from app.domain.tasks.oneoff import TaskView
from app.domain.tasks.schedules import ScheduleView
from app.domain.tasks.templates import TaskTemplateView

from .derived import (
    _aware_utc,
    _compute_overdue,
    _compute_time_window_local,
    _humanize_rrule,
)


class InventoryEffectPayload(BaseModel):
    """One entry of a task template's :attr:`TaskTemplatePayload.inventory_effects`.

    Mirrors §08 "Inventory effects on task completion" — a list of
    ``{item_ref, kind, qty}`` declaring what the task **uses** and what
    it **produces**. The wire shape is the canonical projection per the
    spec; the v1 storage column (``inventory_consumption_json``, a flat
    SKU → positive int map) is a consume-only subset and is preserved on
    the wire alongside this richer projection while the storage migration
    lands. See :func:`TaskTemplatePayload.from_view` for the derivation.
    """

    item_ref: str
    kind: Literal["consume", "produce"]
    qty: int


class TaskTemplatePayload(BaseModel):
    """HTTP projection of :class:`TaskTemplateView`.

    The checklist items ride through as plain dicts (rather than the
    domain :class:`ChecklistTemplateItemPayload`) so the OpenAPI schema
    matches the shape the caller POSTed — a round-trip-identical wire
    format means the SPA can post a template and echo the response
    back on PATCH without a reshape step.

    ``inventory_effects`` is the spec-canonical projection of the
    template's inventory rules — a list of ``{item_ref, kind, qty}``
    entries (§08). Today the v1 storage column is a flat
    ``inventory_consumption_json`` map (consume-only, integer qty); the
    derived array re-projects each entry as ``kind="consume"`` so the
    SPA can render the spec shape directly. ``inventory_consumption_json``
    is kept on the wire as the authoring shape until the storage widens
    to ``inventory_effects_json`` per spec §06.

    Round-trip note: the request bodies (:class:`TaskTemplateCreate` /
    :class:`TaskTemplateUpdate`) accept ``inventory_consumption_json``
    only — ``inventory_effects`` is read-only on the wire. A SPA that
    POSTs back the response shape must drop ``inventory_effects`` (and
    the audit fields ``id``, ``workspace_id``, ``created_at``,
    ``deleted_at``) before sending; ``model_config = extra="forbid"``
    on the body rejects the fuller projection. Once storage widens to
    ``inventory_effects_json`` the request body will accept the array
    directly and the asymmetry resolves.
    """

    id: str
    workspace_id: str
    name: str
    description_md: str
    role_id: str | None
    duration_minutes: int
    property_scope: str
    listed_property_ids: list[str]
    area_scope: str
    listed_area_ids: list[str]
    checklist_template_json: list[dict[str, Any]]
    photo_evidence: str
    linked_instruction_ids: list[str]
    priority: str
    auto_shift_from_occurrence: bool
    inventory_consumption_json: dict[str, int]
    inventory_effects: list[InventoryEffectPayload]
    llm_hints_md: str | None
    created_at: datetime
    deleted_at: datetime | None

    @classmethod
    def from_view(cls, view: TaskTemplateView) -> TaskTemplatePayload:
        """Copy a :class:`TaskTemplateView` into its HTTP payload."""
        consumption = dict(view.inventory_consumption_json)
        effects = [
            InventoryEffectPayload(item_ref=sku, kind="consume", qty=qty)
            for sku, qty in consumption.items()
        ]
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            name=view.name,
            description_md=view.description_md,
            role_id=view.role_id,
            duration_minutes=view.duration_minutes,
            property_scope=view.property_scope,
            listed_property_ids=list(view.listed_property_ids),
            area_scope=view.area_scope,
            listed_area_ids=list(view.listed_area_ids),
            checklist_template_json=[
                item.model_dump(mode="json") for item in view.checklist_template_json
            ],
            photo_evidence=view.photo_evidence,
            linked_instruction_ids=list(view.linked_instruction_ids),
            priority=view.priority,
            auto_shift_from_occurrence=view.auto_shift_from_occurrence,
            inventory_consumption_json=consumption,
            inventory_effects=effects,
            llm_hints_md=view.llm_hints_md,
            created_at=view.created_at,
            deleted_at=view.deleted_at,
        )


class SchedulePayload(BaseModel):
    """HTTP projection of :class:`ScheduleView`.

    Two fields are derived on the wire to spare the SPA a fan-out join:

    * ``default_assignee_id`` mirrors the domain's
      :attr:`ScheduleView.default_assignee` under a clearer wire name
      (it's a user id; the SPA's ``Schedule`` TS type matches).
    * ``rrule_human`` is a short English summary of the recurrence —
      "Every Monday at 10:00" — composed from the schedule's RRULE +
      ``dtstart_local`` so the manager Schedules page can render the
      cadence column without re-implementing the parser in TypeScript.
    """

    id: str
    workspace_id: str
    name: str
    template_id: str
    property_id: str | None
    area_id: str | None
    default_assignee_id: str | None
    backup_assignee_user_ids: list[str]
    rrule: str
    rrule_human: str
    dtstart_local: str
    duration_minutes: int | None
    rdate_local: str
    exdate_local: str
    active_from: str | None
    active_until: str | None
    paused_at: datetime | None
    created_at: datetime
    deleted_at: datetime | None

    @classmethod
    def from_view(cls, view: ScheduleView) -> SchedulePayload:
        """Copy a :class:`ScheduleView` into its HTTP payload."""
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            name=view.name,
            template_id=view.template_id,
            property_id=view.property_id,
            area_id=view.area_id,
            default_assignee_id=view.default_assignee,
            backup_assignee_user_ids=list(view.backup_assignee_user_ids),
            rrule=view.rrule,
            rrule_human=_humanize_rrule(view.rrule, view.dtstart_local),
            dtstart_local=view.dtstart_local,
            duration_minutes=view.duration_minutes,
            rdate_local=view.rdate_local,
            exdate_local=view.exdate_local,
            active_from=(
                view.active_from.isoformat() if view.active_from is not None else None
            ),
            active_until=(
                view.active_until.isoformat() if view.active_until is not None else None
            ),
            paused_at=view.paused_at,
            created_at=view.created_at,
            deleted_at=view.deleted_at,
        )


class NlTaskPreviewRequest(BaseModel):
    """Request body for ``POST /tasks/from_nl``."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=10_000)
    dry_run: bool = True


class NlTaskCommitRequest(BaseModel):
    """Request body for ``POST /tasks/from_nl/commit``."""

    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(..., min_length=1, max_length=96)
    resolved: ResolvedTask | None = None
    assumptions: list[str] | None = None
    ambiguities: list[Ambiguity] | None = None


class NlTaskPreviewPayload(BaseModel):
    """HTTP projection of an NL task preview."""

    preview_id: str
    resolved: ResolvedTask
    assumptions: list[str]
    ambiguities: list[Ambiguity]
    expires_at: datetime

    @classmethod
    def from_preview(cls, preview: NlPreview) -> NlTaskPreviewPayload:
        return cls(
            preview_id=preview.preview_id,
            resolved=preview.resolved,
            assumptions=list(preview.assumptions),
            ambiguities=list(preview.ambiguities),
            expires_at=preview.expires_at,
        )


class NlTaskCommitPayload(BaseModel):
    """HTTP projection of the rows created from an NL task preview."""

    template: TaskTemplatePayload
    schedule: SchedulePayload

    @classmethod
    def from_scheduled(cls, scheduled: ScheduledTask) -> NlTaskCommitPayload:
        return cls(
            template=TaskTemplatePayload.from_view(scheduled.template),
            schedule=SchedulePayload.from_view(scheduled.schedule),
        )


class TaskPayload(BaseModel):
    """HTTP projection of :class:`TaskView` with two derived fields.

    * ``overdue`` — boolean: the task is past its scheduled UTC
      anchor and not yet in a terminal state. Mirrors §06's soft-
      overdue rule. The cd-hurw column ``overdue_since`` (set by the
      sweeper worker) wins when present; for rows the sweeper has
      not yet visited (between the slip and the next 5-minute tick)
      we fall back to the time-derived projection so the manager
      surface does not show a stale "on time" chip.
    * ``time_window_local`` — ``"HH:MM-HH:MM"`` in the property
      timezone, computed from ``scheduled_for_utc`` + the task's
      ``duration_minutes`` (fallback to 30 minutes when the column
      is ``NULL``, matching the :func:`create_oneoff` default). Only
      populated when the task carries a resolvable ``property_id``;
      workspace-scoped (personal) tasks render as ``None``.
    """

    id: str
    workspace_id: str
    template_id: str | None
    schedule_id: str | None
    property_id: str | None
    area_id: str | None
    unit_id: str | None
    title: str
    description_md: str | None
    priority: str
    state: str
    scheduled_for_local: str
    scheduled_for_utc: datetime
    duration_minutes: int | None
    photo_evidence: str
    linked_instruction_ids: list[str]
    inventory_consumption_json: dict[str, int]
    expected_role_id: str | None
    assigned_user_id: str | None
    created_by: str
    is_personal: bool
    created_at: datetime
    overdue: bool
    time_window_local: str | None

    @classmethod
    def from_view(
        cls,
        view: TaskView,
        *,
        property_timezone: str | None = None,
        now_utc: datetime | None = None,
    ) -> TaskPayload:
        """Copy a :class:`TaskView` into its HTTP payload.

        ``property_timezone`` resolves ``time_window_local`` — callers
        that already know the zone pass it in (saving a redundant
        property lookup); an omitted zone leaves the window unrendered.
        ``now_utc`` drives the ``overdue`` bool; defaults to
        :func:`datetime.now` in UTC so unit tests can pin a fixed clock
        without mocking module state.
        """
        moment = now_utc if now_utc is not None else datetime.now(tz=ZoneInfo("UTC"))
        overdue = _compute_overdue(view, moment)
        window = _compute_time_window_local(view, property_timezone)
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            template_id=view.template_id,
            schedule_id=view.schedule_id,
            property_id=view.property_id,
            area_id=view.area_id,
            unit_id=view.unit_id,
            title=view.title,
            description_md=view.description_md,
            priority=view.priority,
            state=view.state,
            scheduled_for_local=view.scheduled_for_local,
            scheduled_for_utc=view.scheduled_for_utc,
            duration_minutes=view.duration_minutes,
            photo_evidence=view.photo_evidence,
            linked_instruction_ids=list(view.linked_instruction_ids),
            inventory_consumption_json=dict(view.inventory_consumption_json),
            expected_role_id=view.expected_role_id,
            assigned_user_id=view.assigned_user_id,
            created_by=view.created_by,
            is_personal=view.is_personal,
            created_at=view.created_at,
            overdue=overdue,
            time_window_local=window,
        )


class TaskDetailPropertyPayload(BaseModel):
    """Worker task-detail property summary.

    Kept local to the tasks router so the worker detail endpoint can
    expose the property chip without broadening the existing
    ``/properties`` governance surface.
    """

    id: str
    name: str
    city: str
    timezone: str
    color: Literal["moss", "sky", "rust"]
    kind: Literal["str", "vacation", "residence", "mixed"]
    areas: list[str]
    evidence_policy: Literal["inherit", "require", "optional", "forbid"]
    country: str
    locale: str
    settings_override: dict[str, object]
    client_org_id: str | None
    owner_user_id: str | None


class TaskDetailInstructionPayload(BaseModel):
    """Instruction resolved for the worker task detail screen."""

    id: str
    title: str
    scope: Literal["global", "property", "area"]
    property_id: str | None
    area: str | None
    tags: list[str]
    body_md: str
    version: int
    updated_at: datetime


class TaskChecklistItemPayload(BaseModel):
    """Runtime checklist row attached to one task occurrence."""

    id: str
    text: str
    label: str
    required: bool
    guest_visible: bool
    checked: bool
    done: bool
    completed_at: datetime | None
    checked_at: datetime | None
    completed_by_user_id: str | None
    evidence_blob_hash: str | None

    @classmethod
    def from_row(cls, row: ChecklistItem) -> TaskChecklistItemPayload:
        """Copy a :class:`ChecklistItem` row into the worker wire shape."""
        return cls(
            id=row.id,
            text=row.label,
            label=row.label,
            required=row.requires_photo,
            guest_visible=False,
            checked=row.checked,
            done=row.checked,
            completed_at=_aware_utc(row.checked_at),
            checked_at=_aware_utc(row.checked_at),
            completed_by_user_id=None,
            evidence_blob_hash=row.evidence_blob_hash,
        )


class ResolvedInventoryEffectPayload(BaseModel):
    """Worker detail inventory effect projection.

    The v1 task row only stores the consume-only
    ``inventory_consumption_json`` map. The fuller inventory resolver
    can widen these fields later without changing the envelope shape.
    """

    item_ref: str
    kind: Literal["consume", "produce"]
    qty: int
    item_id: str | None
    item_name: str
    unit: str
    on_hand: int | None


class TaskDetailPayload(BaseModel):
    """Worker task-detail envelope."""

    task: TaskPayload
    property: TaskDetailPropertyPayload | None
    instructions: list[TaskDetailInstructionPayload]
    checklist: list[TaskChecklistItemPayload]
    inventory_effects: list[ResolvedInventoryEffectPayload]


class ChecklistPatchRequest(BaseModel):
    """Idempotent checklist tick/untick request."""

    model_config = ConfigDict(extra="forbid")

    checked: bool


class TaskStatePayload(BaseModel):
    """HTTP projection of :class:`TaskState`.

    Returned by ``start`` / ``complete`` / ``skip`` / ``cancel``. The
    router echoes every field the SPA renders to draw the toast + chip
    after a state transition.
    """

    task_id: str
    state: str
    completed_at: datetime | None
    completed_by_user_id: str | None
    reason: str | None

    @classmethod
    def from_view(cls, view: TaskState) -> TaskStatePayload:
        """Copy a :class:`TaskState` into its HTTP payload."""
        return cls(
            task_id=view.task_id,
            state=view.state,
            completed_at=view.completed_at,
            completed_by_user_id=view.completed_by_user_id,
            reason=view.reason,
        )


class AssignmentPayload(BaseModel):
    """HTTP projection of :class:`AssignmentResult`.

    Returned by ``POST /tasks/{id}/assign``. Keeps the shape distinct
    from :class:`TaskStatePayload` so callers don't confuse "state
    transition" with "assignee changed" — two different events on
    the bus (``task.assigned`` vs ``task.state_changed``).

    ``state`` echoes the task's current :attr:`Occurrence.state` after
    the assignment so the SPA can refresh the chip without a second
    round-trip; assignment never changes the state machine, so this
    mirrors the pre-call value.
    """

    task_id: str
    assigned_user_id: str | None
    assignment_source: str
    candidate_count: int
    backup_index: int | None
    state: str


class CommentPayload(BaseModel):
    """HTTP projection of :class:`CommentView`."""

    id: str
    occurrence_id: str
    kind: str
    author_user_id: str | None
    body_md: str
    mentioned_user_ids: list[str]
    attachments: list[dict[str, Any]]
    created_at: datetime
    edited_at: datetime | None
    deleted_at: datetime | None
    llm_call_id: str | None

    @classmethod
    def from_view(cls, view: CommentView) -> CommentPayload:
        """Copy a :class:`CommentView` into its HTTP payload."""
        return cls(
            id=view.id,
            occurrence_id=view.occurrence_id,
            kind=view.kind,
            author_user_id=view.author_user_id,
            body_md=view.body_md,
            mentioned_user_ids=list(view.mentioned_user_ids),
            attachments=[dict(item) for item in view.attachments],
            created_at=view.created_at,
            edited_at=view.edited_at,
            deleted_at=view.deleted_at,
            llm_call_id=view.llm_call_id,
        )


class EvidencePayload(BaseModel):
    """HTTP projection of :class:`EvidenceView`."""

    id: str
    workspace_id: str
    occurrence_id: str
    kind: str
    blob_hash: str | None
    note_md: str | None
    created_at: datetime
    created_by_user_id: str | None

    @classmethod
    def from_view(cls, view: EvidenceView) -> EvidencePayload:
        """Copy an :class:`EvidenceView` into its HTTP payload."""
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            occurrence_id=view.occurrence_id,
            kind=view.kind,
            blob_hash=view.blob_hash,
            note_md=view.note_md,
            created_at=view.created_at,
            created_by_user_id=view.created_by_user_id,
        )


class TaskTemplateListResponse(BaseModel):
    """Collection envelope for ``GET /task_templates``."""

    data: list[TaskTemplatePayload]
    next_cursor: str | None = None
    has_more: bool = False


class ScheduleListResponse(BaseModel):
    """Collection envelope for ``GET /schedules``.

    Carries the standard cursor-paginated ``{data, next_cursor, has_more}``
    envelope plus a ``templates_by_id`` sidecar — a one-call shape the
    SPA's manager Schedules page joins against without a second
    ``GET /task_templates`` round-trip. Schedules and their parent
    templates are tightly coupled (a Schedule rotates a template), so
    bundling them is semantically appropriate; the sidecar only carries
    templates referenced on *this page* (pagination-respecting), so the
    payload size scales with the page, not the workspace. See
    ``docs/specs/12-rest-api.md`` §"Tasks / templates / schedules".
    """

    data: list[SchedulePayload]
    next_cursor: str | None = None
    has_more: bool = False
    templates_by_id: dict[str, TaskTemplatePayload] = Field(default_factory=dict)


class TaskListResponse(BaseModel):
    """Collection envelope for ``GET /tasks``."""

    data: list[TaskPayload]
    next_cursor: str | None = None
    has_more: bool = False


class CommentListResponse(BaseModel):
    """Collection envelope for ``GET /tasks/{id}/comments``.

    Cursor is a base64-url-encoded ``(created_at_iso, id)`` tuple so
    two comments sharing a clock tick still paginate deterministically
    per the service's tuple-cursor contract.
    """

    data: list[CommentPayload]
    next_cursor: str | None = None
    has_more: bool = False


class EvidenceListResponse(BaseModel):
    """Collection envelope for ``GET /tasks/{id}/evidence``."""

    data: list[EvidencePayload]
    next_cursor: str | None = None
    has_more: bool = False


class OccurrencePreviewItem(BaseModel):
    """One occurrence in :class:`SchedulePreviewResponse.occurrences`."""

    starts_local: str


class SchedulePreviewResponse(BaseModel):
    """Response body for ``GET /schedules/{id}/preview``."""

    occurrences: list[OccurrencePreviewItem]


# ---------------------------------------------------------------------------
# Request shapes
# ---------------------------------------------------------------------------


class AssignRequest(BaseModel):
    """Body for ``POST /tasks/{id}/assign``."""

    model_config = ConfigDict(extra="forbid")

    assignee_user_id: str = Field(..., min_length=1, max_length=64)


class ReasonRequest(BaseModel):
    """Shared body for ``/skip`` and ``/cancel``."""

    model_config = ConfigDict(extra="forbid")

    reason_md: str = Field(..., min_length=1, max_length=20_000)


class CompleteRequest(BaseModel):
    """Body for ``POST /tasks/{id}/complete``."""

    model_config = ConfigDict(extra="forbid")

    note_md: str | None = Field(default=None, max_length=20_000)
    photo_evidence_ids: list[str] = Field(default_factory=list, max_length=50)


class CommentEditRequest(BaseModel):
    """Body for ``PATCH /tasks/{id}/comments/{comment_id}``."""

    model_config = ConfigDict(extra="forbid")

    body_md: str = Field(..., min_length=1, max_length=20_000)
