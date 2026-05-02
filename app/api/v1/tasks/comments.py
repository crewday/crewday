"""Task comment routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, status

from app.adapters.db.tasks.repositories import (
    AuthzCommentModerationAuthorizer,
    SqlAlchemyCommentsRepository,
)
from app.api.pagination import DEFAULT_LIMIT, LimitQuery, PageCursorQuery
from app.domain.tasks.comments import (
    CommentAttachmentInvalid,
    CommentCreate,
    CommentEditWindowExpired,
    CommentKindForbidden,
    CommentMentionAmbiguous,
    CommentMentionInvalid,
    CommentNotEditable,
    CommentNotFound,
    delete_comment,
    edit_comment,
    list_comments,
    post_comment,
)

from .cursor import _decode_comment_cursor, _encode_comment_cursor
from .deps import _Ctx, _Db
from .errors import _comment_not_found, _http_for_comment_mutation, _task_not_found
from .payloads import CommentEditRequest, CommentListResponse, CommentPayload

router = APIRouter()


@router.post(
    "/{task_id}/comments",
    status_code=status.HTTP_201_CREATED,
    response_model=CommentPayload,
    operation_id="post_task_comment",
    summary="Append a comment to a task's agent thread",
)
def post_task_comment_route(
    task_id: str,
    body: CommentCreate,
    ctx: _Ctx,
    session: _Db,
) -> CommentPayload:
    """Delegate to :func:`app.domain.tasks.comments.post_comment`.

    ``kind`` is inferred from ``ctx.actor_kind``: a user / system
    actor posts ``kind='user'``; an agent token posts ``kind='agent'``.
    The ``system`` kind is internal-only and not reachable through the
    HTTP surface — state-change markers come from the completion /
    assignment services with ``internal_caller=True``.
    """
    kind: Literal["user", "agent"] = "agent" if ctx.actor_kind == "agent" else "user"
    repo = SqlAlchemyCommentsRepository(session)
    try:
        view = post_comment(repo, ctx, task_id, body, kind=kind)
    except CommentNotFound as exc:
        # ``post_comment`` raises :class:`CommentNotFound` when the
        # parent task is missing / cross-tenant / gated by the
        # personal-task rule — *not* when a comment id is unknown
        # (POST creates). Surface the actual missing entity so the
        # 404 envelope is truthful.
        raise _task_not_found() from exc
    except (
        CommentKindForbidden,
        CommentMentionInvalid,
        CommentMentionAmbiguous,
        CommentAttachmentInvalid,
    ) as exc:
        raise _http_for_comment_mutation(exc) from exc
    return CommentPayload.from_view(view)


@router.get(
    "/{task_id}/comments",
    response_model=CommentListResponse,
    operation_id="list_task_comments",
    summary="List comments on a task (oldest-first, cursor-paginated)",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "comments-list"}},
)
def list_task_comments_route(
    task_id: str,
    ctx: _Ctx,
    session: _Db,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> CommentListResponse:
    """Return a cursor-paginated page of comments.

    The cursor is a tuple ``(created_at, id)`` so two comments
    sharing a clock tick still paginate deterministically.
    """
    repo = SqlAlchemyCommentsRepository(session)
    try:
        after_ts, after_id = _decode_comment_cursor(cursor)
        views = list(
            list_comments(
                repo,
                ctx,
                task_id,
                after=after_ts,
                after_id=after_id,
                limit=limit + 1,
            )
        )
    except CommentNotFound as exc:
        raise _task_not_found() from exc
    has_more = len(views) > limit
    items = views[:limit]
    next_cursor: str | None = None
    if has_more and items:
        last = items[-1]
        next_cursor = _encode_comment_cursor(last.created_at, last.id)
    return CommentListResponse(
        data=[CommentPayload.from_view(v) for v in items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.patch(
    "/{task_id}/comments/{comment_id}",
    response_model=CommentPayload,
    operation_id="patch_task_comment",
    summary="Edit a comment within the author grace window",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "comment-update"}},
)
def patch_task_comment_route(
    task_id: str,
    comment_id: str,
    body: CommentEditRequest,
    ctx: _Ctx,
    session: _Db,
) -> CommentPayload:
    """Delegate to :func:`app.domain.tasks.comments.edit_comment`.

    ``task_id`` in the URL is an addressing aid for the SPA / CLI —
    the service loads the comment by id and re-asserts the parent
    occurrence from the row, so a mismatched ``task_id`` does not
    allow cross-task rewrites. We still enforce the pairing defensively
    here so a caller that scraped the wrong id learns loudly.
    """
    repo = SqlAlchemyCommentsRepository(session)
    try:
        view = edit_comment(repo, ctx, comment_id, body.body_md)
    except (
        CommentNotFound,
        CommentKindForbidden,
        CommentEditWindowExpired,
        CommentNotEditable,
        CommentMentionInvalid,
        CommentMentionAmbiguous,
    ) as exc:
        raise _http_for_comment_mutation(exc) from exc
    if view.occurrence_id != task_id:
        # Cross-task request — collapse to 404 so we don't leak the
        # existence of the comment on a different task.
        raise _comment_not_found()
    return CommentPayload.from_view(view)


@router.delete(
    "/{task_id}/comments/{comment_id}",
    response_model=CommentPayload,
    operation_id="delete_task_comment",
    summary="Soft-delete a comment",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "comment-delete"}},
)
def delete_task_comment_route(
    task_id: str,
    comment_id: str,
    ctx: _Ctx,
    session: _Db,
) -> CommentPayload:
    """Delegate to :func:`app.domain.tasks.comments.delete_comment`."""
    repo = SqlAlchemyCommentsRepository(session)
    authorizer = AuthzCommentModerationAuthorizer(session)
    try:
        view = delete_comment(repo, ctx, comment_id, authorizer=authorizer)
    except (
        CommentNotFound,
        CommentKindForbidden,
        CommentNotEditable,
    ) as exc:
        raise _http_for_comment_mutation(exc) from exc
    if view.occurrence_id != task_id:
        raise _comment_not_found()
    return CommentPayload.from_view(view)
