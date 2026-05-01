"""Unit tests for the cd-ayvn ``CommentsRepository`` + authorizer seams.

The full integration round-trip with a real DB session, the tenant
filter, and ``write_audit`` lives under
``tests/unit/test_tasks_comments.py`` (the SA-backed concretion path).
These tests exercise the **seam** — confirming
:mod:`app.domain.tasks.comments` runs against a stub repo + stub
authorizer without reaching for SQLAlchemy at all. Catches a
regression where a domain function silently re-imports the SA model
classes or :func:`app.authz.require` (the very stopgaps cd-cfe4 /
cd-ayvn close).

The fake repo also routes through the audit writer via
:attr:`CommentsRepository.session`, so we cover the shared accessor
by passing a fake session that records :meth:`add` calls (the same
shape ``write_audit`` performs).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.domain.tasks.comments import (
    CommentAttachmentInvalid,
    CommentCreate,
    CommentKindForbidden,
    CommentMentionInvalid,
    CommentNotFound,
    delete_comment,
    edit_comment,
    list_comments,
    post_comment,
)
from app.domain.tasks.ports import (
    CommentModerationAuthorizer,
    CommentRow,
    CommentsRepository,
    EvidenceAttachmentRow,
    MentionCandidateRow,
    ModerationDenied,
    OccurrenceCommentScopeRow,
)
from app.events.bus import EventBus
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_WS_ID = "01HWA00000000000000000WS01"
_OTHER_WS_ID = "01HWA00000000000000000WS02"
_PROP_ID = "01HWA00000000000000000PRP1"
_OCC_ID = "01HWA00000000000000000OCC1"
_AUTHOR_ID = "01HWA00000000000000000USR1"
_OUTSIDER_ID = "01HWA00000000000000000USR2"
_MAYA_ID = "01HWA00000000000000000USR3"
_OWNER_ID = "01HWA00000000000000000USR4"


def _ctx(
    *,
    workspace_id: str = _WS_ID,
    actor_id: str = _AUTHOR_ID,
    actor_kind: str = "user",
    actor_was_owner_member: bool = False,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="ws",
        actor_id=actor_id,
        actor_kind=actor_kind,  # type: ignore[arg-type]
        actor_grant_role="manager",
        actor_was_owner_member=actor_was_owner_member,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


class _FakeSession:
    """Tiny stand-in for :class:`sqlalchemy.orm.Session`.

    ``write_audit`` only calls ``.add`` on the audit row inside the
    UoW; anything else raises so a missed migration shows up loudly.
    """

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, instance: object) -> None:
        self.added.append(instance)


class _FakeRepo(CommentsRepository):
    """In-memory :class:`CommentsRepository` stub.

    Models the workspace-scoped surface the domain consumes — one
    occurrence-scope dict, one evidence dict, one mention-candidate
    list, one comment table keyed by id. No tenant filter — the
    domain code always passes ``ctx.workspace_id`` explicitly and the
    fake re-asserts the predicate as defence-in-depth.
    """

    def __init__(self) -> None:
        self._session = _FakeSession()
        self._occurrences: dict[tuple[str, str], OccurrenceCommentScopeRow] = {}
        self._evidence: dict[tuple[str, str, str], EvidenceAttachmentRow] = {}
        self._mention_candidates: dict[str, list[MentionCandidateRow]] = {}
        self._comments: dict[tuple[str, str], CommentRow] = {}
        # Recording surfaces.
        self.inserted: list[CommentRow] = []
        self.updated: list[CommentRow] = []
        self.soft_deleted: list[CommentRow] = []

    @property
    def session(self) -> Any:
        # Protocol declares ``Session`` but the domain only routes
        # the accessor into ``write_audit``, which itself only calls
        # ``.add`` on the audit row. Returning ``Any`` avoids the SA
        # import in this unit test; mypy accepts ``Any`` as
        # covariantly compatible with ``Session`` without a
        # type-ignore.
        return self._session

    # -- bootstrap helpers (test-only) -----------------------------------

    def add_occurrence(
        self,
        *,
        occurrence_id: str = _OCC_ID,
        workspace_id: str = _WS_ID,
        property_id: str | None = _PROP_ID,
        is_personal: bool = False,
        created_by_user_id: str | None = None,
    ) -> None:
        self._occurrences[(workspace_id, occurrence_id)] = OccurrenceCommentScopeRow(
            id=occurrence_id,
            workspace_id=workspace_id,
            property_id=property_id,
            is_personal=is_personal,
            created_by_user_id=created_by_user_id,
        )

    def add_evidence(
        self,
        *,
        evidence_id: str,
        workspace_id: str = _WS_ID,
        occurrence_id: str = _OCC_ID,
        kind: str = "photo",
        blob_hash: str | None = None,
    ) -> None:
        self._evidence[(workspace_id, occurrence_id, evidence_id)] = (
            EvidenceAttachmentRow(
                id=evidence_id,
                kind=kind,
                blob_hash=blob_hash if blob_hash is not None else f"blob-{evidence_id}",
            )
        )

    def add_member(
        self, *, user_id: str, display_name: str, workspace_id: str = _WS_ID
    ) -> None:
        self._mention_candidates.setdefault(workspace_id, []).append(
            MentionCandidateRow(id=user_id, display_name=display_name)
        )

    # -- CommentsRepository (occurrence + evidence + mention reads) ------

    def get_occurrence_scope(
        self, *, workspace_id: str, occurrence_id: str
    ) -> OccurrenceCommentScopeRow | None:
        return self._occurrences.get((workspace_id, occurrence_id))

    def list_evidence_attachments(
        self,
        *,
        workspace_id: str,
        occurrence_id: str,
        evidence_ids: Sequence[str],
    ) -> Sequence[EvidenceAttachmentRow]:
        return [
            row
            for key, row in self._evidence.items()
            if key[0] == workspace_id
            and key[1] == occurrence_id
            and key[2] in evidence_ids
        ]

    def list_mention_candidates(
        self, *, workspace_id: str
    ) -> Sequence[MentionCandidateRow]:
        return list(self._mention_candidates.get(workspace_id, []))

    # -- CommentsRepository (comment reads) ------------------------------

    def get_comment(self, *, workspace_id: str, comment_id: str) -> CommentRow | None:
        return self._comments.get((workspace_id, comment_id))

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
        rows = [
            row
            for (ws_id, _cid), row in self._comments.items()
            if ws_id == workspace_id and row.occurrence_id == occurrence_id
        ]
        if not include_deleted:
            rows = [r for r in rows if r.deleted_at is None]
        if after is not None:
            if after_id is None:
                rows = [r for r in rows if r.created_at > after]
            else:
                rows = [r for r in rows if (r.created_at, r.id) > (after, after_id)]
        rows.sort(key=lambda r: (r.created_at, r.id))
        return rows[:limit]

    # -- CommentsRepository (writes) -------------------------------------

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
        row = CommentRow(
            id=comment_id,
            workspace_id=workspace_id,
            occurrence_id=occurrence_id,
            kind=kind,
            author_user_id=author_user_id,
            body_md=body_md,
            mentioned_user_ids=tuple(mentioned_user_ids),
            attachments=tuple(dict(item) for item in attachments),
            created_at=created_at,
            edited_at=None,
            deleted_at=None,
            llm_call_id=llm_call_id,
        )
        self._comments[(workspace_id, comment_id)] = row
        self.inserted.append(row)
        return row

    def update_comment_body(
        self,
        *,
        workspace_id: str,
        comment_id: str,
        body_md: str,
        mentioned_user_ids: Sequence[str],
        edited_at: datetime,
    ) -> CommentRow:
        existing = self._comments[(workspace_id, comment_id)]
        updated = CommentRow(
            id=existing.id,
            workspace_id=existing.workspace_id,
            occurrence_id=existing.occurrence_id,
            kind=existing.kind,
            author_user_id=existing.author_user_id,
            body_md=body_md,
            mentioned_user_ids=tuple(mentioned_user_ids),
            attachments=existing.attachments,
            created_at=existing.created_at,
            edited_at=edited_at,
            deleted_at=existing.deleted_at,
            llm_call_id=existing.llm_call_id,
        )
        self._comments[(workspace_id, comment_id)] = updated
        self.updated.append(updated)
        return updated

    def soft_delete_comment(
        self,
        *,
        workspace_id: str,
        comment_id: str,
        deleted_at: datetime,
    ) -> CommentRow:
        existing = self._comments[(workspace_id, comment_id)]
        updated = CommentRow(
            id=existing.id,
            workspace_id=existing.workspace_id,
            occurrence_id=existing.occurrence_id,
            kind=existing.kind,
            author_user_id=existing.author_user_id,
            body_md=existing.body_md,
            mentioned_user_ids=existing.mentioned_user_ids,
            attachments=existing.attachments,
            created_at=existing.created_at,
            edited_at=existing.edited_at,
            deleted_at=deleted_at,
            llm_call_id=existing.llm_call_id,
        )
        self._comments[(workspace_id, comment_id)] = updated
        self.soft_deleted.append(updated)
        return updated


class _RecordingAuthorizer(CommentModerationAuthorizer):
    """Authorizer that records every call and either allows or denies.

    Drives the moderator-delete branch deterministically without
    touching :func:`app.authz.require` or the SA session.
    """

    def __init__(self, *, deny: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._deny = deny

    def require_moderator(
        self,
        ctx: WorkspaceContext,
        *,
        scope_kind: str,
        scope_id: str,
    ) -> None:
        self.calls.append((ctx.actor_id, scope_kind, scope_id))
        if self._deny:
            raise ModerationDenied("tasks.comment_moderate")


class TestPostCommentSeam:
    """Drive :func:`post_comment` against a fake repo with no SA session."""

    def test_user_kind_inserts_through_repo_and_writes_audit(self) -> None:
        repo = _FakeRepo()
        repo.add_occurrence(created_by_user_id=_AUTHOR_ID)
        clock = FrozenClock(_PINNED)
        ctx = _ctx()
        bus = EventBus()
        captured: list[Any] = []
        from app.events.types import TaskCommentAdded

        bus.subscribe(TaskCommentAdded)(captured.append)

        view = post_comment(
            repo,
            ctx,
            _OCC_ID,
            CommentCreate(body_md="All set."),
            clock=clock,
            event_bus=bus,
        )

        # One row hit the repo.
        assert len(repo.inserted) == 1
        assert repo.inserted[0].id == view.id
        assert repo.inserted[0].kind == "user"
        assert repo.inserted[0].author_user_id == _AUTHOR_ID
        # One audit row landed via the threaded session accessor.
        assert len(repo.session.added) == 1
        # Event fired.
        assert len(captured) == 1
        assert captured[0].comment_id == view.id

    def test_unknown_occurrence_raises_not_found(self) -> None:
        repo = _FakeRepo()
        # No occurrence seeded.
        clock = FrozenClock(_PINNED)

        with pytest.raises(CommentNotFound):
            post_comment(
                repo,
                _ctx(),
                _OCC_ID,
                CommentCreate(body_md="hi"),
                clock=clock,
                event_bus=EventBus(),
            )

    def test_mention_resolution_routes_through_repo(self) -> None:
        repo = _FakeRepo()
        repo.add_occurrence()
        repo.add_member(user_id=_MAYA_ID, display_name="Maya")
        clock = FrozenClock(_PINNED)

        view = post_comment(
            repo,
            _ctx(),
            _OCC_ID,
            CommentCreate(body_md="Hey @maya, filter replaced."),
            clock=clock,
            event_bus=EventBus(),
        )

        assert view.mentioned_user_ids == (_MAYA_ID,)

    def test_mention_of_non_member_raises_invalid(self) -> None:
        repo = _FakeRepo()
        repo.add_occurrence()
        # No member seeded — every @-tag is unknown.

        with pytest.raises(CommentMentionInvalid) as excinfo:
            post_comment(
                repo,
                _ctx(),
                _OCC_ID,
                CommentCreate(body_md="cc @stranger"),
                clock=FrozenClock(_PINNED),
                event_bus=EventBus(),
            )
        assert "stranger" in excinfo.value.unknown_slugs

    def test_attachment_resolution_routes_through_repo(self) -> None:
        repo = _FakeRepo()
        repo.add_occurrence()
        repo.add_evidence(evidence_id="ev-1")
        clock = FrozenClock(_PINNED)

        view = post_comment(
            repo,
            _ctx(),
            _OCC_ID,
            CommentCreate(body_md="see photo", attachments=["ev-1"]),
            clock=clock,
            event_bus=EventBus(),
        )

        assert len(view.attachments) == 1
        assert view.attachments[0]["evidence_id"] == "ev-1"
        assert view.attachments[0]["kind"] == "photo"

    def test_attachment_unknown_id_raises_invalid(self) -> None:
        repo = _FakeRepo()
        repo.add_occurrence()

        with pytest.raises(CommentAttachmentInvalid):
            post_comment(
                repo,
                _ctx(),
                _OCC_ID,
                CommentCreate(body_md="hi", attachments=["ev-missing"]),
                clock=FrozenClock(_PINNED),
                event_bus=EventBus(),
            )

    def test_personal_task_gate_collapses_to_404(self) -> None:
        """Non-creator non-owner gets :class:`CommentNotFound` (404)."""
        repo = _FakeRepo()
        repo.add_occurrence(is_personal=True, created_by_user_id=_AUTHOR_ID)
        ctx = _ctx(actor_id=_OUTSIDER_ID, actor_was_owner_member=False)

        with pytest.raises(CommentNotFound):
            post_comment(
                repo,
                ctx,
                _OCC_ID,
                CommentCreate(body_md="snoop"),
                clock=FrozenClock(_PINNED),
                event_bus=EventBus(),
            )


class TestEditCommentSeam:
    """Drive :func:`edit_comment` against a fake repo."""

    def _seed_comment(self, repo: _FakeRepo, clock: FrozenClock) -> str:
        repo.add_occurrence()
        view = post_comment(
            repo,
            _ctx(),
            _OCC_ID,
            CommentCreate(body_md="original"),
            clock=clock,
            event_bus=EventBus(),
        )
        return view.id

    def test_author_within_window_routes_update_through_repo(self) -> None:
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        comment_id = self._seed_comment(repo, clock)

        clock.advance(timedelta(minutes=2))
        updated = edit_comment(repo, _ctx(), comment_id, "edited", clock=clock)

        assert updated.body_md == "edited"
        assert updated.edited_at is not None
        assert len(repo.updated) == 1
        # Two audit rows now: create + edit.
        assert len(repo.session.added) == 2


class TestDeleteCommentSeam:
    """Drive :func:`delete_comment` against fake repo + fake authorizer."""

    def _seed_comment(
        self,
        repo: _FakeRepo,
        clock: FrozenClock,
        *,
        author_id: str = _AUTHOR_ID,
    ) -> str:
        repo.add_occurrence()
        view = post_comment(
            repo,
            _ctx(actor_id=author_id),
            _OCC_ID,
            CommentCreate(body_md="oops"),
            clock=clock,
            event_bus=EventBus(),
        )
        return view.id

    def test_author_path_skips_authorizer(self) -> None:
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        comment_id = self._seed_comment(repo, clock)
        authorizer = _RecordingAuthorizer()

        deleted = delete_comment(
            repo, _ctx(), comment_id, clock=clock, authorizer=authorizer
        )

        assert deleted.deleted_at is not None
        # Author shortcut bypasses the authz seam entirely.
        assert authorizer.calls == []
        assert len(repo.soft_deleted) == 1

    def test_owner_fast_path_skips_authorizer(self) -> None:
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        comment_id = self._seed_comment(repo, clock, author_id=_AUTHOR_ID)
        authorizer = _RecordingAuthorizer()

        owner_ctx = _ctx(actor_id=_OWNER_ID, actor_was_owner_member=True)
        deleted = delete_comment(
            repo, owner_ctx, comment_id, clock=clock, authorizer=authorizer
        )

        assert deleted.deleted_at is not None
        assert authorizer.calls == []

    def test_moderator_path_invokes_authorizer_with_property_scope(self) -> None:
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        comment_id = self._seed_comment(repo, clock, author_id=_AUTHOR_ID)
        authorizer = _RecordingAuthorizer()

        # Stranger — neither author nor owner. The seam is the only
        # path through; the authorizer must be called with the
        # property scope (occurrence has property_id seeded).
        stranger_ctx = _ctx(actor_id=_OUTSIDER_ID, actor_was_owner_member=False)
        delete_comment(
            repo, stranger_ctx, comment_id, clock=clock, authorizer=authorizer
        )

        assert authorizer.calls == [(_OUTSIDER_ID, "property", _PROP_ID)]

    def test_moderator_denied_collapses_to_kind_forbidden(self) -> None:
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        comment_id = self._seed_comment(repo, clock, author_id=_AUTHOR_ID)
        authorizer = _RecordingAuthorizer(deny=True)

        stranger_ctx = _ctx(actor_id=_OUTSIDER_ID, actor_was_owner_member=False)
        with pytest.raises(CommentKindForbidden):
            delete_comment(
                repo, stranger_ctx, comment_id, clock=clock, authorizer=authorizer
            )
        # The authorizer was called once and refused; no soft-delete
        # landed on the repo.
        assert len(authorizer.calls) == 1
        assert repo.soft_deleted == []

    def test_no_authorizer_short_circuits_to_kind_forbidden(self) -> None:
        """No authorizer wired -> stranger path 403's immediately."""
        repo = _FakeRepo()
        clock = FrozenClock(_PINNED)
        comment_id = self._seed_comment(repo, clock, author_id=_AUTHOR_ID)

        stranger_ctx = _ctx(actor_id=_OUTSIDER_ID, actor_was_owner_member=False)
        with pytest.raises(CommentKindForbidden):
            delete_comment(repo, stranger_ctx, comment_id, clock=clock, authorizer=None)
        assert repo.soft_deleted == []

    def test_workspace_scope_when_occurrence_has_no_property(self) -> None:
        """Personal / property-less tasks fall back to workspace scope."""
        repo = _FakeRepo()
        repo.add_occurrence(property_id=None, is_personal=False)
        clock = FrozenClock(_PINNED)
        view = post_comment(
            repo,
            _ctx(),
            _OCC_ID,
            CommentCreate(body_md="hi"),
            clock=clock,
            event_bus=EventBus(),
        )
        authorizer = _RecordingAuthorizer()

        stranger_ctx = _ctx(actor_id=_OUTSIDER_ID, actor_was_owner_member=False)
        delete_comment(repo, stranger_ctx, view.id, clock=clock, authorizer=authorizer)

        assert authorizer.calls == [(_OUTSIDER_ID, "workspace", _WS_ID)]


class TestListCommentsSeam:
    """Drive :func:`list_comments` against a fake repo."""

    def test_owner_sees_soft_deleted_rows(self) -> None:
        repo = _FakeRepo()
        repo.add_occurrence()
        clock = FrozenClock(_PINNED)
        live_view = post_comment(
            repo,
            _ctx(),
            _OCC_ID,
            CommentCreate(body_md="live"),
            clock=clock,
            event_bus=EventBus(),
        )
        clock.advance(timedelta(seconds=1))
        deleted_view = post_comment(
            repo,
            _ctx(),
            _OCC_ID,
            CommentCreate(body_md="to-delete"),
            clock=clock,
            event_bus=EventBus(),
        )
        # Author-delete the second one.
        delete_comment(repo, _ctx(), deleted_view.id, clock=clock, authorizer=None)

        owner_ctx = _ctx(actor_id=_OWNER_ID, actor_was_owner_member=True)
        rows = list_comments(repo, owner_ctx, _OCC_ID)
        # Owner sees both — the soft-deleted row included.
        assert {r.id for r in rows} == {live_view.id, deleted_view.id}

    def test_non_owner_hides_soft_deleted(self) -> None:
        repo = _FakeRepo()
        repo.add_occurrence()
        clock = FrozenClock(_PINNED)
        live_view = post_comment(
            repo,
            _ctx(),
            _OCC_ID,
            CommentCreate(body_md="live"),
            clock=clock,
            event_bus=EventBus(),
        )
        clock.advance(timedelta(seconds=1))
        deleted_view = post_comment(
            repo,
            _ctx(),
            _OCC_ID,
            CommentCreate(body_md="to-delete"),
            clock=clock,
            event_bus=EventBus(),
        )
        delete_comment(repo, _ctx(), deleted_view.id, clock=clock, authorizer=None)

        rows = list_comments(repo, _ctx(), _OCC_ID)
        # Non-owner sees only the live row.
        assert [r.id for r in rows] == [live_view.id]
