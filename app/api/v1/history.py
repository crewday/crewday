"""Worker history feed — ``GET /history``.

Mounted inside ``/w/<slug>/api/v1`` by the app factory, sibling of
``/me/schedule`` and ``/dashboard``. Surface per
``docs/specs/12-rest-api.md`` §"Self-service shortcuts":

```
GET    /history?tab=tasks|chats|expenses|leaves
```

The page is the worker self-service "Everything already wrapped up"
view — the SPA at ``app/web/src/pages/employee/HistoryPage.tsx``
reads exactly this path and renders one of four independently paged
tabs.

**Self-only by construction.** Every read keys on ``ctx.actor_id`` /
``ctx.workspace_id``; the service does not accept a ``user_id``
parameter. A worker cannot widen the response to another user's
history. ``ctx.actor_was_owner_member`` is **not** consulted: managers
peeking at their own history is fine; cross-user inspection happens
through the per-resource managerial surfaces (``/tasks?assignee_user_id=…``,
``/expenses?user_id=…``, ``/employees/{id}/leaves``).

**Wire shape.** The endpoint follows spec §12's standard cursor
envelope. ``tab`` selects which row projection lands in ``data``:

```json
{"data": [...], "next_cursor": null, "has_more": false}
```

**Filters mirror the mock reference.**

* ``tab=tasks``: ``Occurrence`` rows assigned to the caller with
  ``state IN ('completed', 'skipped')``. Matches the mock's
  ``status in {completed, skipped}`` rule.
* ``tab=expenses``: ``ExpenseClaim`` rows whose ``work_engagement``
  belongs to the caller, with ``state IN ('approved', 'rejected',
  'reimbursed')``. Matches the mock's ``status in {approved,
  reimbursed, rejected}`` rule.
* ``tab=leaves``: ``Leave`` rows for the caller with
  ``status='approved'`` and ``ends_at < today (UTC)``. Matches the
  mock's ``approved_at IS NOT NULL AND ends_on < today`` rule.
* ``tab=chats``: archived agent chat channels owned by the caller
  (``external_ref = agent:<scope>:<actor_id>``), newest first.

See ``docs/specs/12-rest-api.md`` §"Self-service shortcuts",
``docs/specs/14-web-frontend.md`` §"Worker history".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.adapters.db.messaging.models import ChatChannel, ChatMessage
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.time.models import Leave
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.api.v1._problem_json import IDENTITY_PROBLEM_RESPONSES
from app.api.v1.dashboard import (
    DashboardLeave,
    DashboardTask,
    _area_labels,
    _leave_from_row,
    _task_from_row,
)
from app.api.v1.expenses import ExpenseClaimPayload
from app.api.v1.expenses import make_seam_pair as _expenses_seam_pair
from app.domain.expenses.claims import ExpenseClaimView, ExpenseState, list_for_user
from app.tenancy import WorkspaceContext

__all__ = [
    "HistoryChatItem",
    "HistoryPayload",
    "HistoryTab",
    "build_history_router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]

# §06 ``occurrence.state`` values that count as "history" — the mock
# at ``mocks/app/main.py:3550`` filters tasks to ``status in
# {completed, skipped}``; production mirrors this exactly.
_HISTORY_TASK_STATES: tuple[str, ...] = ("completed", "skipped")

# §09 ``expense_claim.state`` values that count as "history" — the
# mock at ``mocks/app/main.py:3553`` filters claims to ``status in
# {approved, reimbursed, rejected}``.
_HISTORY_EXPENSE_STATES: tuple[ExpenseState, ...] = (
    "approved",
    "reimbursed",
    "rejected",
)


HistoryTab = Literal["tasks", "chats", "expenses", "leaves"]
type HistoryItem = (
    DashboardTask | ExpenseClaimPayload | DashboardLeave | HistoryChatItem
)


class HistoryChatItem(BaseModel):
    """One archived agent chat row.

    Mirrors the SPA's history chat row shape
    (``app/web/src/types/dashboard.ts``). Rows are projected from
    archived per-user agent channels; the endpoint never accepts a
    user selector.
    """

    id: str
    title: str
    last_at: str
    summary: str


class HistoryPayload(BaseModel):
    """Standard §12 cursor envelope for one selected history tab."""

    model_config = ConfigDict(extra="forbid")

    data: list[HistoryItem]
    next_cursor: str | None
    has_more: bool


_TabQuery = Annotated[
    HistoryTab,
    Query(
        description=(
            "History tab to return. Unknown values surface as 422 via "
            "FastAPI's default Pydantic Literal validation."
        ),
    ),
]


def _history_tasks_statement(ctx: WorkspaceContext) -> Select[tuple[Occurrence]]:
    return select(Occurrence).where(
        Occurrence.workspace_id == ctx.workspace_id,
        Occurrence.assignee_user_id == ctx.actor_id,
        Occurrence.state.in_(_HISTORY_TASK_STATES),
    )


def _page_history_tasks(
    session: Session,
    ctx: WorkspaceContext,
    *,
    limit: int,
    cursor: str | None,
) -> HistoryPayload:
    after_id = decode_cursor(cursor)
    statement = _history_tasks_statement(ctx)
    if after_id is not None:
        statement = statement.where(Occurrence.id < after_id)
    rows = list(
        session.scalars(statement.order_by(Occurrence.id.desc()).limit(limit + 1)).all()
    )
    if not rows:
        return HistoryPayload(data=[], next_cursor=None, has_more=False)
    area_labels = _area_labels(session, [row.area_id for row in rows if row.area_id])
    page = paginate(rows, limit=limit, key_getter=lambda row: row.id)
    return HistoryPayload(
        data=[_task_from_row(row, area_labels=area_labels) for row in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


def _list_history_expense_views(
    session: Session,
    ctx: WorkspaceContext,
    *,
    limit: int,
    cursor: str | None,
) -> list[ExpenseClaimView]:
    repo, checker = _expenses_seam_pair(session, ctx)
    merged: list[ExpenseClaimView] = []
    for state in _HISTORY_EXPENSE_STATES:
        views, _ = list_for_user(
            repo,
            checker,
            ctx,
            state=state,
            limit=limit + 1,
            cursor=cursor,
        )
        merged.extend(views)
    merged.sort(key=lambda v: v.id, reverse=True)
    return merged


def _page_history_expenses(
    session: Session,
    ctx: WorkspaceContext,
    *,
    limit: int,
    cursor: str | None,
) -> HistoryPayload:
    after_id = decode_cursor(cursor)
    views = _list_history_expense_views(session, ctx, limit=limit, cursor=after_id)
    page = paginate(views, limit=limit, key_getter=lambda view: view.id)
    return HistoryPayload(
        data=[ExpenseClaimPayload.from_view(view) for view in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


def _history_leaves_statement(
    ctx: WorkspaceContext,
    *,
    now: datetime,
) -> Select[tuple[Leave]]:
    today_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
    return select(Leave).where(
        Leave.workspace_id == ctx.workspace_id,
        Leave.user_id == ctx.actor_id,
        Leave.status == "approved",
        Leave.ends_at < today_start,
    )


def _page_history_leaves(
    session: Session,
    ctx: WorkspaceContext,
    *,
    now: datetime,
    limit: int,
    cursor: str | None,
) -> HistoryPayload:
    after_id = decode_cursor(cursor)
    statement = _history_leaves_statement(ctx, now=now)
    if after_id is not None:
        statement = statement.where(Leave.id < after_id)
    rows = list(
        session.scalars(statement.order_by(Leave.id.desc()).limit(limit + 1)).all()
    )
    page = paginate(rows, limit=limit, key_getter=lambda row: row.id)
    return HistoryPayload(
        data=[_leave_from_row(row) for row in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


def _history_chats_statement(ctx: WorkspaceContext) -> Select[tuple[ChatChannel]]:
    return select(ChatChannel).where(
        ChatChannel.workspace_id == ctx.workspace_id,
        ChatChannel.source == "app",
        ChatChannel.kind.in_(("staff", "manager")),
        ChatChannel.external_ref.in_(
            (
                f"agent:employee:{ctx.actor_id}",
                f"agent:manager:{ctx.actor_id}",
            )
        ),
        ChatChannel.archived_at.is_not(None),
    )


def _page_history_chats(
    session: Session,
    ctx: WorkspaceContext,
    *,
    limit: int,
    cursor: str | None,
) -> HistoryPayload:
    after_id = decode_cursor(cursor)
    statement = _history_chats_statement(ctx)
    if after_id is not None:
        boundary = session.scalar(
            _history_chats_statement(ctx).where(ChatChannel.id == after_id)
        )
        if boundary is not None and boundary.archived_at is not None:
            statement = statement.where(
                (ChatChannel.archived_at < boundary.archived_at)
                | (
                    (ChatChannel.archived_at == boundary.archived_at)
                    & (ChatChannel.id < boundary.id)
                )
            )
        else:
            statement = statement.where(ChatChannel.id < after_id)
    rows = list(
        session.scalars(
            statement.order_by(
                ChatChannel.archived_at.desc(), ChatChannel.id.desc()
            ).limit(limit + 1)
        ).all()
    )
    page = paginate(rows, limit=limit, key_getter=lambda row: row.id)
    return HistoryPayload(
        data=[_chat_from_channel(session, row) for row in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


def _chat_from_channel(session: Session, row: ChatChannel) -> HistoryChatItem:
    summary = session.scalar(
        select(ChatMessage.body_md)
        .where(
            ChatMessage.workspace_id == row.workspace_id,
            ChatMessage.channel_id == row.id,
            ChatMessage.kind == "summary",
            ChatMessage.compacted_into_id.is_(None),
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(1)
    )
    latest_at = session.scalar(
        select(ChatMessage.created_at)
        .where(
            ChatMessage.workspace_id == row.workspace_id,
            ChatMessage.channel_id == row.id,
            ChatMessage.kind != "summary",
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(1)
    )
    return HistoryChatItem(
        id=row.id,
        title=row.title or _chat_fallback_title(row),
        last_at=_history_time(latest_at or row.archived_at),
        summary=summary or "",
    )


def _chat_fallback_title(row: ChatChannel) -> str:
    return "Manager agent chat" if row.kind == "manager" else "Employee agent chat"


def _history_time(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def build_history_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for the history surface."""
    api = APIRouter(
        tags=["identity", "me"],
        responses=IDENTITY_PROBLEM_RESPONSES,
    )

    @api.get(
        "/history",
        response_model=HistoryPayload,
        operation_id="me.history.get",
        summary=(
            "Worker history feed — completed tasks, decided expenses, "
            "past leaves, archived chats"
        ),
        openapi_extra={"x-cli": {"group": "me", "verb": "history"}},
    )
    def get_history(
        ctx: _Ctx,
        session: _Db,
        tab: _TabQuery = "tasks",
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> HistoryPayload:
        """Return one cursor-paginated history tab for the caller.

        Unknown ``tab`` values surface as 422 from FastAPI's default
        Pydantic ``Literal`` validation. Anonymous callers surface
        as 401 from :func:`current_workspace_context`.
        """
        now = datetime.now(tz=UTC)
        if tab == "tasks":
            return _page_history_tasks(session, ctx, limit=limit, cursor=cursor)
        if tab == "expenses":
            return _page_history_expenses(session, ctx, limit=limit, cursor=cursor)
        if tab == "leaves":
            return _page_history_leaves(
                session,
                ctx,
                now=now,
                limit=limit,
                cursor=cursor,
            )
        return _page_history_chats(session, ctx, limit=limit, cursor=cursor)

    return api


router = build_history_router()
