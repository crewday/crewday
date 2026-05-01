"""Tasks context — repository + authorizer ports for comments service.

Defines the seams :mod:`app.domain.tasks.comments` uses to read and
write ``comment`` / ``occurrence`` / ``evidence`` rows, resolve
``@mention`` slugs against ``user`` + ``user_workspace`` rows, and
enforce the ``tasks.comment_moderate`` capability without importing
SQLAlchemy model classes or :mod:`app.authz` directly.

Spec: ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 —
each context defines its own repository port in its public surface
and a SQLAlchemy adapter under ``app/adapters/db/<context>/``. The
SA-backed concretions (:class:`SqlAlchemyCommentsRepository` plus
the thin :class:`AuthzCommentModerationAuthorizer`) live in
:mod:`app.adapters.db.tasks.repositories` so the seams call into the
matching ORM layer. Tests substitute fakes and never touch SA.

Two protocols live here:

* :class:`CommentsRepository` — cursored comment CRUD plus the small
  occurrence / evidence / user / membership reads the comments
  service performs (author resolution + workspace-membership filter
  + attachment scope check). Returns immutable row projections so
  the domain never sees an ORM row.
* :class:`CommentModerationAuthorizer` — the ``tasks.comment_moderate``
  capability check the moderator-delete branch invokes. A single
  ``require_moderator`` method that mirrors the §05 rule-driven
  shape; the SA concretion delegates to :func:`app.authz.require`.
  Carving the call out of the domain closes the cd-cfe4
  ``app.domain.tasks.comments -> app.authz`` stopgap — the domain
  stops touching :mod:`app.authz` directly.

The repo exposes a ``session`` accessor so :mod:`app.domain.tasks.comments`
can thread the same UoW through :func:`app.audit.write_audit` (which
still takes a concrete ``Session`` today). Once that helper gains its
own seam, the accessor can drop.

Frozen value objects (:class:`CommentRow`, :class:`OccurrenceCommentScopeRow`,
:class:`EvidenceAttachmentRow`, :class:`MentionCandidateRow`) mirror the
domain's read view (:class:`~app.domain.tasks.comments.CommentView`)
and the inputs the helpers consume. They live on the seam so the SA
adapter has a domain-owned shape to project ORM rows into without
importing the service module that produces the views (which would
create a circular dependency between ``comments`` and this module).

Protocols are deliberately **not** ``runtime_checkable``: structural
compatibility is checked statically by mypy. Runtime ``isinstance``
against these protocols would mask typos and invite duck-typing
shortcuts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.tenancy import WorkspaceContext

__all__ = [
    "CommentModerationAuthorizer",
    "CommentRow",
    "CommentsRepository",
    "EvidenceAttachmentRow",
    "MentionCandidateRow",
    "ModerationDenied",
    "OccurrenceCommentScopeRow",
]


# ---------------------------------------------------------------------------
# Row shapes (value objects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OccurrenceCommentScopeRow:
    """Immutable projection of an ``occurrence`` row for the comments service.

    Carries only the columns the comments service reads:

    * ``id`` / ``workspace_id`` — identity + tenant predicate.
    * ``property_id`` — moderator-delete scopes the
      ``tasks.comment_moderate`` check against the property scope when
      available; ``NULL`` falls back to the workspace scope.
    * ``is_personal`` / ``created_by_user_id`` — drive the §06
      "Self-created and personal tasks" visibility gate.

    Other ``occurrence`` columns (state machine, completion, …) ride
    on sibling services and are not surfaced here.
    """

    id: str
    workspace_id: str
    property_id: str | None
    is_personal: bool
    created_by_user_id: str | None


@dataclass(frozen=True, slots=True)
class EvidenceAttachmentRow:
    """Immutable projection of an ``evidence`` row for attachment resolution.

    The comments service persists ``{evidence_id, blob_hash, kind}``
    onto ``Comment.attachments_json`` so a later evidence soft-delete
    does not break the thread view. Only those three fields are
    surfaced; richer evidence columns ride on the evidence service.
    """

    id: str
    blob_hash: str | None
    kind: str


@dataclass(frozen=True, slots=True)
class MentionCandidateRow:
    """Immutable projection of a ``user`` row that is a workspace member.

    Returned by :meth:`CommentsRepository.list_mention_candidates` — the
    join of ``user`` + ``user_workspace`` scoped to the caller's
    workspace. Only ``id`` + ``display_name`` are surfaced; the
    domain's ``_normalise_slug`` helper derives the slug.
    """

    id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class CommentRow:
    """Immutable projection of a ``comment`` row.

    Mirrors :class:`app.domain.tasks.comments.CommentView` minus the
    ``CommentKind`` literal narrowing — the domain re-narrows on the
    way out. The ``attachments`` payload round-trips the denormalised
    list persisted on :attr:`Comment.attachments_json`; each entry is
    a ``{evidence_id, blob_hash, kind}`` mapping.

    Datetime columns are raw SA reads (SQLite strips tzinfo on
    ``DateTime(timezone=True)`` columns); the domain's ``_ensure_utc``
    helper restamps before comparison.
    """

    id: str
    workspace_id: str
    occurrence_id: str
    kind: str
    author_user_id: str | None
    body_md: str
    mentioned_user_ids: tuple[str, ...]
    attachments: tuple[dict[str, Any], ...]
    created_at: datetime
    edited_at: datetime | None
    deleted_at: datetime | None
    llm_call_id: str | None


# ---------------------------------------------------------------------------
# CommentsRepository
# ---------------------------------------------------------------------------


class CommentsRepository(Protocol):
    """Read + write seam for ``comment`` rows plus the comments-adjacent reads.

    Wraps the workspace-scoped surface :mod:`app.domain.tasks.comments`
    consumes: comment CRUD, occurrence visibility lookup, evidence
    attachment resolution, and mention-candidate enumeration. Closes
    the cd-cfe4 stopgap by isolating ``app.adapters.db.tasks.models``,
    ``app.adapters.db.identity.models``, and
    ``app.adapters.db.workspace.models`` behind one Protocol.

    Carries an open SQLAlchemy ``Session`` via the :attr:`session`
    accessor so callers that also write audit through
    :func:`app.audit.write_audit` (which still takes a concrete
    ``Session`` today) and publish events on the request-local bus
    can thread the same UoW. Once the audit writer gains its own
    seam, the accessor can drop.

    Every method honours the workspace-scoping invariant: the SA
    concretion always pins reads + writes to the ``workspace_id``
    passed by the caller, mirroring the ORM tenant filter as
    defence-in-depth (§01 "Key runtime invariants" #2 — a
    misconfigured filter must fail loud).

    The repo never commits and only flushes where the underlying
    statements require — the caller's UoW owns the transaction
    boundary (§01 "Key runtime invariants" #3).
    """

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session for audit threading."""
        ...

    # -- occurrence + evidence + mention reads ---------------------------

    def get_occurrence_scope(
        self, *, workspace_id: str, occurrence_id: str
    ) -> OccurrenceCommentScopeRow | None:
        """Return the occurrence-scope shape or ``None`` if not visible.

        Used at the start of every comments service entry-point to
        load the parent occurrence + drive the personal-task gate.
        """
        ...

    def list_evidence_attachments(
        self,
        *,
        workspace_id: str,
        occurrence_id: str,
        evidence_ids: Sequence[str],
    ) -> Sequence[EvidenceAttachmentRow]:
        """Return every evidence row matching ``evidence_ids`` in scope.

        Pin on ``workspace_id`` AND ``occurrence_id`` — the comments
        service rejects attachments that resolve to a different
        occurrence (or workspace) so the agent inbox never cross-
        links artefacts across threads.
        """
        ...

    def list_mention_candidates(
        self, *, workspace_id: str
    ) -> Sequence[MentionCandidateRow]:
        """Return every workspace member's ``(id, display_name)`` pair.

        Drives the ``_resolve_mentions`` join — the domain derives
        the slug from ``display_name`` via ``_normalise_slug`` and
        matches against the body's ``@<slug>`` tokens.
        """
        ...

    # -- comment reads ---------------------------------------------------

    def get_comment(self, *, workspace_id: str, comment_id: str) -> CommentRow | None:
        """Return the comment row scoped to ``workspace_id`` or ``None``.

        Used by the edit / delete / get paths; pairs with
        :meth:`get_occurrence_scope` for the personal-task gate.
        """
        ...

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
        """Return ``limit`` comments oldest-first matching the cursor.

        ``include_deleted=False`` hides ``deleted_at IS NOT NULL``
        rows — owners pass ``True`` to see moderation history.
        ``after`` / ``after_id`` form the ``(created_at, id)`` tuple
        cursor; passing both narrows to strictly greater than the
        pair, ``after`` alone narrows to strictly greater than the
        instant.
        """
        ...

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
        """Insert a fresh comment row and return its projection.

        Flushes so the caller's audit writer + event bus see the new
        id in the same UoW.
        """
        ...

    def update_comment_body(
        self,
        *,
        workspace_id: str,
        comment_id: str,
        body_md: str,
        mentioned_user_ids: Sequence[str],
        edited_at: datetime,
    ) -> CommentRow:
        """Rewrite ``body_md`` + re-resolve mentions; stamp ``edited_at``.

        Caller is responsible for the kind / window / authorship
        guards (the domain enforces them above this seam). Flushes
        so a peer read in the same UoW sees the new shape.
        """
        ...

    def soft_delete_comment(
        self,
        *,
        workspace_id: str,
        comment_id: str,
        deleted_at: datetime,
    ) -> CommentRow:
        """Stamp ``deleted_at`` on the comment — soft delete.

        Caller is responsible for the already-deleted / authorisation
        guards. Flushes so a peer read in the same UoW sees the
        tombstone.
        """
        ...


# ---------------------------------------------------------------------------
# CommentModerationAuthorizer
# ---------------------------------------------------------------------------


class CommentModerationAuthorizer(Protocol):
    """Capability seam for the moderator-delete branch of the comments service.

    Carving the ``tasks.comment_moderate`` enforcement out of the
    domain closes the cd-cfe4 ``app.domain.tasks.comments -> app.authz``
    stopgap — :mod:`app.domain.tasks.comments` calls
    :meth:`require_moderator` instead of importing
    :func:`app.authz.require` directly. The SA concretion in
    :mod:`app.adapters.db.tasks.repositories` delegates to the
    canonical authz path; tests substitute a fake authorizer that
    records or refuses.

    The single method takes the same shape the domain previously
    passed to :func:`app.authz.require`: a workspace context, a
    scope kind / id (property when available, workspace fallback)
    so a property-scoped allow / deny rule is honoured. Authorisers
    raise the seam-level :class:`ModerationDenied` (or a subclass)
    on refusal — the domain catches it and collapses to the public
    :class:`~app.domain.tasks.comments.CommentKindForbidden` shape so
    HTTP error mapping stays untouched.
    """

    def require_moderator(
        self,
        ctx: WorkspaceContext,
        *,
        scope_kind: str,
        scope_id: str,
    ) -> None:
        """Enforce the ``tasks.comment_moderate`` gate or raise.

        ``scope_kind`` is ``"property"`` when the occurrence is
        property-anchored; ``"workspace"`` falls back when the
        occurrence carries no property (personal tasks). The SA
        concretion threads the repo's session into the underlying
        :func:`app.authz.require` call. Raises
        :class:`ModerationDenied` on refusal.
        """
        ...


class ModerationDenied(Exception):
    """The caller is not allowed to moderate the targeted comment.

    Seam-level twin of :class:`app.authz.PermissionDenied`. The
    domain catches this and re-raises the public
    :class:`~app.domain.tasks.comments.CommentKindForbidden` so HTTP
    error mapping remains stable. Carries the action key for forensic
    logs even though the domain throws away the inner detail.
    """

    def __init__(self, action_key: str = "tasks.comment_moderate") -> None:
        super().__init__(f"moderation denied: {action_key}")
        self.action_key = action_key
