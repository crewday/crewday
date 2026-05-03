"""Worker history aggregator (cd-wnsr) — ``GET /history``.

Mounted inside ``/w/<slug>/api/v1`` by the app factory, sibling of
``/me/schedule`` and ``/dashboard``. Surface per
``docs/specs/12-rest-api.md`` §"Self-service shortcuts":

```
GET    /history?tab=tasks|chats|expenses|leaves
```

The page is the worker self-service "Everything already wrapped up"
view — the SPA at ``app/web/src/pages/employee/HistoryPage.tsx``
reads exactly this path and renders one of four tabs from a single
``HistoryPayload`` envelope.

**Self-only by construction.** Every read keys on ``ctx.actor_id`` /
``ctx.workspace_id``; the service does not accept a ``user_id``
parameter. A worker cannot widen the response to another user's
history. ``ctx.actor_was_owner_member`` is **not** consulted: managers
peeking at their own history is fine; cross-user inspection happens
through the per-resource managerial surfaces (``/tasks?assignee_user_id=…``,
``/expenses?user_id=…``, ``/employees/{id}/leaves``).

**Wire shape (cd-wnsr Option A — non-§12 envelope).** The SPA's
``HistoryPayload`` (``app/web/src/types/dashboard.ts``) is a single
object carrying all four arrays plus the active ``tab`` echo:

```ts
{
  tab: "tasks" | "chats" | "expenses" | "leaves",
  tasks: Task[],
  expenses: Expense[],
  leaves: Leave[],
  chats: { id, title, last_at, summary }[]
}
```

This deliberately deviates from spec §12's
``{data, next_cursor, has_more}`` envelope: §12's cursor envelope
doesn't accommodate the four-array fan-out the SPA renders, and the
mock backend (``mocks/app/main.py:3539-3562``) already returns this
exact shape. Keeping the production response shape identical means
the SPA needs no migration.

**Pagination via fixed cap.** Each tab is bounded to
:data:`_TAB_CAP` rows (newest-first). Crossing the cap silently
truncates — the SPA's history page is a "recent activity" surface,
not an exhaustive audit log. A cursor-paginated migration is
filed as a follow-up (see the route docstring); production data
volumes for self-service history (a single user's last few months)
fit comfortably under the cap.

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
* ``tab=chats``: archived agent chats. The chat-archive surface
  doesn't exist yet in production — returns ``[]`` and is tracked
  as a follow-up Beads task.

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

from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.time.models import Leave
from app.api.deps import current_workspace_context, db_session
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

# Maximum rows returned per tab. Capped well below the §12 ``MAX_LIMIT``
# (500) because the history page is a "recent activity" surface — a
# worker's last 50 completed tasks / decided expenses / past leaves
# is what the SPA renders. Hard truncation past the cap is documented
# in the route docstring + spec §12 row.
_TAB_CAP: int = 50

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


class HistoryChatItem(BaseModel):
    """One archived agent chat row.

    Mirrors the SPA's ``HistoryPayload.chats`` shape
    (``app/web/src/types/dashboard.ts``). Production has no chat-
    archive surface yet; this type exists so the OpenAPI schema
    documents the eventual contract and the SPA can render the
    empty state without conditional types.
    """

    id: str
    title: str
    last_at: str
    summary: str


class HistoryPayload(BaseModel):
    """Worker history envelope.

    Single object carrying all four arrays plus the active ``tab``
    echo, matching the SPA's ``HistoryPayload`` interface verbatim.
    See module docstring for why this deviates from the §12
    cursor envelope.

    ``tasks`` reuses :class:`DashboardTask` (not the production
    ``TaskPayload``) so the row shape lines up with the SPA's
    ``Task`` interface in ``app/web/src/types/task.ts`` —
    ``scheduled_start`` / ``status`` / ``area`` / ``checklist`` etc.
    The dashboard endpoint already projects this exact shape, so
    re-using it keeps the SPA's task renderer single-sourced
    across ``/dashboard`` and ``/history``. Every array is
    required (``chats=[]`` is always emitted; the SPA reads
    ``q.data.chats.length`` unconditionally).
    """

    model_config = ConfigDict(extra="forbid")

    tab: HistoryTab
    tasks: list[DashboardTask]
    expenses: list[ExpenseClaimPayload]
    leaves: list[DashboardLeave]
    chats: list[HistoryChatItem]


_TabQuery = Annotated[
    HistoryTab,
    Query(
        description=(
            "Active tab the SPA is rendering. The response carries every "
            "tab's array regardless of this value (so the page can switch "
            "tabs client-side without a refetch); ``tab`` echoes back so "
            "the caller can assert which view it asked for. Unknown "
            "values surface as 422 via FastAPI's default Pydantic "
            "Literal validation."
        ),
    ),
]


def _list_history_tasks(
    session: Session,
    ctx: WorkspaceContext,
) -> list[DashboardTask]:
    """Return up to :data:`_TAB_CAP` ``completed``/``skipped`` tasks for the caller.

    Sorted newest-first by the row's ULID id (correlates with creation
    time per :func:`app.util.ulid.new_ulid`). The projection re-uses
    :func:`app.api.v1.dashboard._task_from_row` so the wire shape lines
    up with the SPA's ``Task`` interface
    (``app/web/src/types/task.ts``) — same as
    ``/dashboard``'s ``by_status`` buckets.
    """
    rows = list(
        session.scalars(
            select(Occurrence)
            .where(
                Occurrence.workspace_id == ctx.workspace_id,
                Occurrence.assignee_user_id == ctx.actor_id,
                Occurrence.state.in_(_HISTORY_TASK_STATES),
            )
            .order_by(Occurrence.id.desc())
            .limit(_TAB_CAP)
        ).all()
    )
    if not rows:
        return []
    area_labels = _area_labels(session, [row.area_id for row in rows if row.area_id])
    return [_task_from_row(row, area_labels=area_labels) for row in rows]


def _list_history_expenses(
    session: Session,
    ctx: WorkspaceContext,
) -> list[ExpenseClaimPayload]:
    """Return up to :data:`_TAB_CAP` decided expense claims for the caller.

    Calls :func:`app.domain.expenses.claims.list_for_user` once per
    history-eligible state (``approved`` / ``reimbursed`` /
    ``rejected``), merges the results, and trims to :data:`_TAB_CAP`
    rows newest-first. Routing through the public service keeps the
    history surface honest against any future tightening of the read
    seam (cap on cross-user reads, audit hooks, etc.) without us
    having to mirror the rules here.

    The seam is keyed on ``ctx.actor_id``; a worker cannot widen the
    listing to another user. ``user_id`` is left unset so the service
    defaults to the caller — no capability check is performed.
    """
    repo, checker = _expenses_seam_pair(session, ctx)
    merged: list[ExpenseClaimView] = []
    for state in _HISTORY_EXPENSE_STATES:
        views, _ = list_for_user(
            repo,
            checker,
            ctx,
            state=state,
            limit=_TAB_CAP,
        )
        merged.extend(views)
    # ``list_for_user`` orders by ``id DESC``; merging three pages
    # disturbs that order, so we re-sort here. Trim to the global cap
    # so a worker with 50 approved + 50 reimbursed claims gets the
    # newest 50 across both states, not 100 rows.
    merged.sort(key=lambda v: v.id, reverse=True)
    return [ExpenseClaimPayload.from_view(view) for view in merged[:_TAB_CAP]]


def _list_history_leaves(
    session: Session,
    ctx: WorkspaceContext,
    *,
    now: datetime,
) -> list[DashboardLeave]:
    """Return up to :data:`_TAB_CAP` past approved leaves for the caller.

    Mirrors the mock filter exactly: ``approved_at IS NOT NULL AND
    ends_on < today``. We re-use :class:`DashboardLeave` (already
    surfaced on ``/dashboard``) so the SPA's ``Leave`` shape lines
    up field-for-field.
    """
    today_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
    rows = list(
        session.scalars(
            select(Leave)
            .where(
                Leave.workspace_id == ctx.workspace_id,
                Leave.user_id == ctx.actor_id,
                Leave.status == "approved",
                Leave.ends_at < today_start,
            )
            .order_by(Leave.id.desc())
            .limit(_TAB_CAP)
        ).all()
    )
    return [_leave_from_row(row) for row in rows]


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
    ) -> HistoryPayload:
        """Return the four-array history envelope for the caller.

        Each array is bounded to :data:`_TAB_CAP` rows (newest-
        first). The ``tab`` query param echoes back on the response;
        every array is populated regardless of the active tab so the
        SPA can switch tabs without a refetch (matches the mock
        behaviour at ``mocks/app/main.py:3539``).

        Unknown ``tab`` values surface as 422 from FastAPI's default
        Pydantic ``Literal`` validation. Anonymous callers surface
        as 401 from :func:`current_workspace_context`.

        Pagination follows the cd-wnsr Option A "fixed cap" strategy
        (see module docstring); a cursor envelope migration is
        tracked as a follow-up Beads task.
        """
        now = datetime.now(tz=UTC)
        return HistoryPayload(
            tab=tab,
            tasks=_list_history_tasks(session, ctx),
            expenses=_list_history_expenses(session, ctx),
            leaves=_list_history_leaves(session, ctx, now=now),
            chats=[],
        )

    return api


router = build_history_router()
