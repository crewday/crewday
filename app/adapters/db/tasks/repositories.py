"""SA-backed concretions for the tasks-context Protocol seams.

Two surfaces live here:

* :class:`SqlAlchemyTasksCreateOccurrencePort` — the cd-ncbdb stay-
  driven occurrence create-or-patch state machine.
* :class:`SqlAlchemyCommentsRepository` plus
  :class:`AuthzCommentModerationAuthorizer` — the cd-ayvn /
  cd-cfe4 seams that close
  :mod:`app.domain.tasks.comments`'s direct ORM and authz imports.
  Mirror the cd-hso7 ``MembershipRepository`` shape — frozen row
  projections, workspace-scoping pinned per call, no commit (caller
  UoW owns the boundary).

The cd-ncbdb adapter persists a turnover :class:`Occurrence` row
when the stays-side bundle service / turnover generator decides one
should exist. Until cd-ncbdb landed the
:class:`~app.ports.tasks_create_occurrence.NoopTasksCreateOccurrencePort`
stub kept the call surface honest while the actual write surface was
absent; this module flips that wiring to a live concretion that
implements the full create-or-patch state machine the port
docstring promises.

**Idempotency.** The natural key is
``(workspace_id, reservation_id, lifecycle_rule_id, occurrence_key)``
matching the partial unique index added in migration cd-ncbdb. The
adapter falls back to an empty-string ``occurrence_key`` when the
caller leaves it ``None`` so single-shot rules (the default
``after_checkout`` rule and ``before_checkin``) dedup on
``(reservation_id, lifecycle_rule_id)`` alone.

**State machine.** Mirrors the port docstring:

1. Look up an existing row by the natural key.
2. Miss → INSERT a fresh ``occurrence`` row tagged with the triple,
   return ``"created"``.
3. Hit + identical ``starts_at`` / ``ends_at`` → no-op, return the
   stored id.
4. Hit + state is terminal / in-progress (``completed``,
   ``approved``, ``skipped``, ``overdue``, ``in_progress``) → no-op,
   return the stored id. Historical rows are immutable; the new
   bundle keeps the existing occurrence as its tasks_json entry.
5. Hit + |Δstarts_at| < ``patch_in_place_threshold`` and the row is
   in ``scheduled | pending`` → patch ``starts_at`` / ``ends_at`` /
   ``due_by_utc`` / ``scheduled_for_local`` in place, return
   ``"patched"``.
6. Hit + |Δstarts_at| ≥ threshold and the row is in
   ``scheduled | pending`` → cancel the existing row
   (``state='cancelled'``,
   ``cancellation_reason=request.regenerate_cancellation_reason``),
   INSERT a fresh row, return ``"regenerated"``.

Reaches across the workspace boundary (the active
:class:`~sqlalchemy.orm.Session`) but the caller's UoW owns the
commit boundary — the adapter only flushes so a peer read in the
same UoW sees the row. Mirrors §01 "Key runtime invariants" #3.

See ``docs/specs/04-properties-and-stays.md`` §"Stay task bundles"
§"Edit semantics" + ``docs/specs/06-tasks-and-scheduling.md``
§"Task row".
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.db.places.models import Property
from app.adapters.db.tasks.models import Comment, Evidence, Occurrence
from app.adapters.db.workspace.models import UserWorkspace
from app.authz import PermissionDenied, require
from app.domain.tasks.ports import (
    CommentModerationAuthorizer,
    CommentRow,
    CommentsRepository,
    EvidenceAttachmentRow,
    MentionCandidateRow,
    ModerationDenied,
    OccurrenceCommentScopeRow,
)
from app.ports.tasks_create_occurrence import (
    TasksCreateOccurrenceOutcome,
    TurnoverOccurrenceRequest,
    TurnoverOccurrenceResult,
)
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid

__all__ = [
    "AuthzCommentModerationAuthorizer",
    "SqlAlchemyCommentsRepository",
    "SqlAlchemyTasksCreateOccurrencePort",
]


# Occurrence states the spec gates in-place patches on. cd-04
# "Edit semantics" pins ``scheduled | pending`` as the writable
# window — anything past that (``in_progress``, ``completed``,
# ``approved``, ``skipped``, ``overdue``, ``cancelled``) means the
# patch path can't safely mutate the row, so a window shift larger
# than the threshold OR an unwritable state both fall through to
# the regenerate branch.
_PATCHABLE_STATES: frozenset[str] = frozenset({"scheduled", "pending"})


class SqlAlchemyTasksCreateOccurrencePort:
    """Live SA concretion of :class:`TasksCreateOccurrencePort`.

    Stateless — every call resolves the triple against the open
    session. The adapter is intentionally trivial: it does not own
    the bundle service's tasks_json bookkeeping, the audit row, or
    any cross-context fan-out. Those belong to the caller. This
    class is the single seam between the stays generator's request
    shape and the ``occurrence`` table's rows.
    """

    def create_or_patch_turnover_occurrence(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        request: TurnoverOccurrenceRequest,
        now: datetime,
    ) -> TurnoverOccurrenceResult:
        # Defence-in-depth: every timestamp that reaches the row is
        # tz-aware UTC. The caller (turnover generator + bundle
        # service) already enforces this; restamp here so a future
        # caller that forgets cannot smuggle a naive datetime into
        # ``starts_at`` / ``ends_at`` / ``due_by_utc``.
        starts_at = _to_utc(request.starts_at)
        ends_at = _to_utc(request.ends_at)
        due_by = _to_utc(request.due_by_utc) if request.due_by_utc is not None else None
        occurrence_key = request.occurrence_key or ""

        existing = session.scalars(
            select(Occurrence)
            .where(Occurrence.workspace_id == ctx.workspace_id)
            .where(Occurrence.reservation_id == request.reservation_id)
            .where(Occurrence.lifecycle_rule_id == request.rule_id)
            .where(Occurrence.occurrence_key == occurrence_key)
            .where(Occurrence.state != "cancelled")
            .limit(1)
        ).one_or_none()

        if existing is None:
            return _insert_occurrence(
                session,
                ctx,
                request=request,
                starts_at=starts_at,
                ends_at=ends_at,
                due_by=due_by,
                occurrence_key=occurrence_key,
                now=now,
                outcome="created",
            )

        existing_starts = _to_utc(existing.starts_at)
        existing_ends = _to_utc(existing.ends_at)
        if existing_starts == starts_at and existing_ends == ends_at:
            return TurnoverOccurrenceResult(occurrence_id=existing.id, outcome="noop")

        # Both the patch and regenerate branches require the existing
        # row to be in ``scheduled | pending`` per §04 "Edit semantics"
        # ("cancel the existing bundle's `scheduled | pending` tasks").
        # A terminal row (``completed``, ``approved``, ``skipped``,
        # ``overdue``) carries history we MUST NOT overwrite —
        # silently flipping ``state`` to ``cancelled`` would clobber
        # the completion record that ``completed_at`` /
        # ``completion_note_md`` already pin. ``in_progress`` is
        # mid-flight and shouldn't be yanked out from under the
        # worker either. Return ``noop`` with the existing id so the
        # caller's audit logs the no-action and the historical row
        # stands. The new bundle keeps the old occurrence as its
        # tasks_json entry; a fresh INSERT would collide with the
        # partial unique index anyway (the predicate is ``state !=
        # 'cancelled'``, so terminal / in-progress rows are still in
        # the index).
        if existing.state not in _PATCHABLE_STATES:
            return TurnoverOccurrenceResult(occurrence_id=existing.id, outcome="noop")

        delta = abs(starts_at - existing_starts)
        if delta < request.patch_in_place_threshold:
            existing.starts_at = starts_at
            existing.ends_at = ends_at
            if due_by is not None:
                existing.due_by_utc = due_by
            existing.scheduled_for_local = _scheduled_for_local(
                session, request.property_id, starts_at
            )
            session.flush()
            return TurnoverOccurrenceResult(
                occurrence_id=existing.id, outcome="patched"
            )

        # Regenerate: cancel the existing scheduled / pending row and
        # insert a fresh one. The partial unique index excludes
        # ``state='cancelled'`` so the cancelled tombstone keeps its
        # triple visible for audit while the regenerated row inherits
        # the live triple.
        existing.state = "cancelled"
        existing.cancellation_reason = request.regenerate_cancellation_reason
        session.flush()

        return _insert_occurrence(
            session,
            ctx,
            request=request,
            starts_at=starts_at,
            ends_at=ends_at,
            due_by=due_by,
            occurrence_key=occurrence_key,
            now=now,
            outcome="regenerated",
        )


def _insert_occurrence(
    session: Session,
    ctx: WorkspaceContext,
    *,
    request: TurnoverOccurrenceRequest,
    starts_at: datetime,
    ends_at: datetime,
    due_by: datetime | None,
    occurrence_key: str,
    now: datetime,
    outcome: TasksCreateOccurrenceOutcome,
) -> TurnoverOccurrenceResult:
    """Insert a fresh ``occurrence`` row and return the port outcome."""
    scheduled_for_local = _scheduled_for_local(session, request.property_id, starts_at)
    row = Occurrence(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        schedule_id=None,
        template_id=None,
        property_id=request.property_id,
        unit_id=request.unit_id,
        starts_at=starts_at,
        ends_at=ends_at,
        scheduled_for_local=scheduled_for_local,
        originally_scheduled_for=scheduled_for_local,
        state="scheduled",
        due_by_utc=due_by,
        reservation_id=request.reservation_id,
        lifecycle_rule_id=request.rule_id,
        occurrence_key=occurrence_key,
        created_at=now,
    )
    session.add(row)
    session.flush()
    return TurnoverOccurrenceResult(occurrence_id=row.id, outcome=outcome)


def _scheduled_for_local(
    session: Session,
    property_id: str,
    starts_at: datetime,
) -> str:
    """Return the ISO-8601 property-local timestamp for ``starts_at``.

    Mirrors :func:`app.worker.tasks.generator._iso_local`'s shape so
    a stay-driven occurrence reads the same column the schedule
    generator's rows do. Falls back to the UTC ISO string if the
    property row is missing — defence against a race where the
    reservation row outlives its property; never crash the port.
    """
    prop = session.get(Property, property_id)
    if prop is None:
        return starts_at.astimezone(UTC).isoformat(timespec="minutes")
    tz = ZoneInfo(prop.timezone)
    local = starts_at.astimezone(tz).replace(tzinfo=None)
    return local.isoformat(timespec="minutes")


def _to_utc(value: datetime) -> datetime:
    """Restamp UTC tz on a possibly-naive datetime.

    SQLite drops tzinfo off ``DateTime(timezone=True)`` columns on
    read; Postgres preserves it. The column is always written as
    aware UTC, so a naive read is a UTC value that has lost its
    zone. Mirrors the helper in
    :mod:`app.domain.stays.turnover_generator`.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# Comments repository (cd-ayvn / cd-cfe4)
# ---------------------------------------------------------------------------


def _to_occurrence_scope_row(row: Occurrence) -> OccurrenceCommentScopeRow:
    """Project an ORM ``Occurrence`` into the seam-level scope shape."""
    return OccurrenceCommentScopeRow(
        id=row.id,
        workspace_id=row.workspace_id,
        property_id=row.property_id,
        is_personal=row.is_personal,
        created_by_user_id=row.created_by_user_id,
    )


def _to_evidence_attachment_row(row: Evidence) -> EvidenceAttachmentRow:
    """Project an ORM ``Evidence`` into the seam-level attachment shape."""
    return EvidenceAttachmentRow(id=row.id, blob_hash=row.blob_hash, kind=row.kind)


def _to_comment_row(row: Comment) -> CommentRow:
    """Project an ORM ``Comment`` into the seam-level row.

    ``mentioned_user_ids`` and ``attachments_json`` are tuple-ified so
    the seam shape stays immutable; the domain re-projects to its
    own ``CommentView`` shape on the way out.
    """
    return CommentRow(
        id=row.id,
        workspace_id=row.workspace_id,
        occurrence_id=row.occurrence_id,
        kind=row.kind,
        author_user_id=row.author_user_id,
        body_md=row.body_md,
        mentioned_user_ids=tuple(row.mentioned_user_ids),
        attachments=tuple(dict(item) for item in row.attachments_json),
        created_at=row.created_at,
        edited_at=row.edited_at,
        deleted_at=row.deleted_at,
        llm_call_id=row.llm_call_id,
    )


class SqlAlchemyCommentsRepository(CommentsRepository):
    """SA-backed concretion of :class:`CommentsRepository`.

    Wraps an open :class:`~sqlalchemy.orm.Session` and never commits —
    the caller's UoW owns the transaction boundary (§01 "Key runtime
    invariants" #3). Mutating methods flush so the audit writer's FK
    reference to ``entity_id`` and the request-local event bus see
    the new row.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # -- occurrence + evidence + mention reads ---------------------------

    def get_occurrence_scope(
        self, *, workspace_id: str, occurrence_id: str
    ) -> OccurrenceCommentScopeRow | None:
        # Defence-in-depth: pin ``workspace_id`` even though the ORM
        # tenant filter narrows it. The comments service relies on
        # the predicate to make the personal-task gate cross-tenant
        # safe.
        row = self._session.scalar(
            select(Occurrence).where(
                Occurrence.id == occurrence_id,
                Occurrence.workspace_id == workspace_id,
            )
        )
        if row is None:
            return None
        return _to_occurrence_scope_row(row)

    def list_evidence_attachments(
        self,
        *,
        workspace_id: str,
        occurrence_id: str,
        evidence_ids: Sequence[str],
    ) -> Sequence[EvidenceAttachmentRow]:
        if not evidence_ids:
            return []
        rows = self._session.scalars(
            select(Evidence).where(
                Evidence.workspace_id == workspace_id,
                Evidence.occurrence_id == occurrence_id,
                Evidence.id.in_(list(evidence_ids)),
            )
        ).all()
        return [_to_evidence_attachment_row(r) for r in rows]

    def list_mention_candidates(
        self, *, workspace_id: str
    ) -> Sequence[MentionCandidateRow]:
        rows = self._session.execute(
            select(User.id, User.display_name)
            .join(UserWorkspace, UserWorkspace.user_id == User.id)
            .where(UserWorkspace.workspace_id == workspace_id)
        ).all()
        return [MentionCandidateRow(id=uid, display_name=name) for uid, name in rows]

    # -- comment reads ---------------------------------------------------

    def get_comment(self, *, workspace_id: str, comment_id: str) -> CommentRow | None:
        row = self._session.scalar(
            select(Comment).where(
                Comment.id == comment_id,
                Comment.workspace_id == workspace_id,
            )
        )
        if row is None:
            return None
        return _to_comment_row(row)

    def list_comments(
        self,
        *,
        workspace_id: str,
        occurrence_id: str,
        include_deleted: bool,
        after: datetime | None,
        after_id: str | None,
        limit: int,
    ) -> Sequence[CommentRow]:
        stmt = select(Comment).where(
            Comment.workspace_id == workspace_id,
            Comment.occurrence_id == occurrence_id,
        )
        if not include_deleted:
            stmt = stmt.where(Comment.deleted_at.is_(None))
        if after is not None:
            if after_id is None:
                stmt = stmt.where(Comment.created_at > after)
            else:
                # Tuple cursor — strictly greater than (after, after_id).
                # Emulated as a disjunction so SQLite (no row-value tuple
                # comparison) and Postgres share one query plan.
                stmt = stmt.where(
                    (Comment.created_at > after)
                    | ((Comment.created_at == after) & (Comment.id > after_id))
                )
        stmt = stmt.order_by(Comment.created_at.asc(), Comment.id.asc()).limit(limit)
        rows = self._session.scalars(stmt).all()
        return [_to_comment_row(r) for r in rows]

    # -- comment writes --------------------------------------------------

    def insert_comment(
        self,
        *,
        comment_id: str,
        workspace_id: str,
        occurrence_id: str,
        author_user_id: str | None,
        kind: str,
        body_md: str,
        mentioned_user_ids: Sequence[str],
        attachments: Sequence[dict[str, Any]],
        llm_call_id: str | None,
        created_at: datetime,
    ) -> CommentRow:
        row = Comment(
            id=comment_id,
            workspace_id=workspace_id,
            occurrence_id=occurrence_id,
            author_user_id=author_user_id,
            body_md=body_md,
            created_at=created_at,
            attachments_json=[dict(item) for item in attachments],
            kind=kind,
            mentioned_user_ids=list(mentioned_user_ids),
            edited_at=None,
            deleted_at=None,
            llm_call_id=llm_call_id,
        )
        self._session.add(row)
        self._session.flush()
        return _to_comment_row(row)

    def update_comment_body(
        self,
        *,
        workspace_id: str,
        comment_id: str,
        body_md: str,
        mentioned_user_ids: Sequence[str],
        edited_at: datetime,
    ) -> CommentRow:
        # Re-fetch the row so the UPDATE flushes against fresh ORM
        # state. The caller already loaded it through
        # :meth:`get_comment` for the guards; that read goes through
        # the same session so the identity map returns the same
        # mapped instance here without an extra round-trip.
        row = self._session.scalars(
            select(Comment).where(
                Comment.id == comment_id,
                Comment.workspace_id == workspace_id,
            )
        ).one()
        row.body_md = body_md
        row.mentioned_user_ids = list(mentioned_user_ids)
        row.edited_at = edited_at
        self._session.flush()
        return _to_comment_row(row)

    def soft_delete_comment(
        self,
        *,
        workspace_id: str,
        comment_id: str,
        deleted_at: datetime,
    ) -> CommentRow:
        row = self._session.scalars(
            select(Comment).where(
                Comment.id == comment_id,
                Comment.workspace_id == workspace_id,
            )
        ).one()
        row.deleted_at = deleted_at
        self._session.flush()
        return _to_comment_row(row)


# ---------------------------------------------------------------------------
# Authz adapter for the comments-moderation seam (cd-ayvn / cd-cfe4)
# ---------------------------------------------------------------------------


class AuthzCommentModerationAuthorizer(CommentModerationAuthorizer):
    """SA-backed concretion of :class:`CommentModerationAuthorizer`.

    Adapter-layer module so calling :func:`app.authz.require` does
    not pull :mod:`app.authz` back into :mod:`app.domain` (the cd-cfe4
    stopgap that this seam closes). The authorizer threads the
    enforcer's session through the same UoW the comments service
    runs in.

    Collapses :class:`app.authz.PermissionDenied` to the seam-level
    :class:`ModerationDenied` so the domain catches a dependency-free
    exception type. Re-raises ``UnknownActionKey`` / ``InvalidScope``
    untouched — those are programmer errors and should fail loudly.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def require_moderator(
        self,
        ctx: WorkspaceContext,
        *,
        scope_kind: str,
        scope_id: str,
    ) -> None:
        try:
            # ``rule_repo=None`` lets ``app.authz.require`` fall through
            # to its process-wide default (an empty-rule repo) — the
            # v1 surface has no ``permission_rule`` table yet, so a
            # caller-pinned repo would be dead weight.
            require(
                self._session,
                ctx,
                action_key="tasks.comment_moderate",
                scope_kind=scope_kind,
                scope_id=scope_id,
            )
        except PermissionDenied as exc:
            raise ModerationDenied("tasks.comment_moderate") from exc
