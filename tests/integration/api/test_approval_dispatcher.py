"""Integration test — :class:`InProcessApprovalDispatcher` error branches.

The dispatcher replays an approved ``ApprovalRequest`` row through the
domain services for the two supported tools (``messaging.broadcast`` and
``cancel_task``). The happy paths live in
``tests/unit/domain/messaging/test_broadcasts.py``
(``test_approved_broadcast_replay_creates_notification_rows``); this
module pins every *failure* branch so a refactor that swallows a domain
error or mis-maps a status code fails loudly:

* unsupported tool name → ``404 unsupported_tool``;
* missing replay-actor header → ``422 missing_replay_actor``;
* broadcast — malformed input, unknown workspace slug, and the
  ``execute_broadcast`` :class:`Validation` domain error;
* cancel_task — malformed input, unknown workspace slug, and the
  :class:`TaskNotFound` / :class:`PermissionDenied` /
  :class:`InvalidStateTransition` domain errors.

The dispatcher runs against a real in-memory SQLite engine wired through
``make_uow`` (the same shape production uses), so the domain services —
audience resolution, the tenancy-scoped workspace lookup, and the task
state machine — execute for real. See ``docs/specs/11-llm-and-agents.md``
§"Agent action approval" and ``docs/specs/17-testing-quality.md``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.base import Base
from app.adapters.db.places.models import Property
from app.adapters.db.session import FilteredSession, make_engine
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import Workspace
from app.api.middleware.approval import (
    InProcessApprovalDispatcher,
    _cancel_task_underlying_action,
)
from app.domain.agent.runtime import DelegatedToken, ToolCall, ToolResult
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
_MANAGER_HEADERS = {
    "X-Crewday-Replay-Actor-Id": "placeholder",
    "X-Crewday-Replay-Actor-Role": "manager",
    "X-Crewday-Replay-Actor-Is-Owner": "1",
}


def _load_all_models() -> None:
    import importlib
    import pkgutil

    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def mem_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(mem_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=mem_engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def wire_uow(
    mem_engine: Engine,
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Point ``make_uow`` at the test engine (mirrors the broadcast tests)."""
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = mem_engine
    _session_mod._default_sessionmaker_ = cast("sessionmaker[FilteredSession]", factory)
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


def _seed_workspace(factory: sessionmaker[Session], *, slug: str) -> tuple[str, str]:
    """Seed a workspace + owner; return ``(workspace_id, owner_user_id)``."""
    with factory() as s:
        owner = bootstrap_user(
            s, email=f"owner-{slug}@example.com", display_name="Owner"
        )
        ws = Workspace(
            id=new_ulid(),
            slug=slug,
            name=slug.title(),
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
        s.add(ws)
        s.flush()
        s.commit()
        return ws.id, owner.id


def _seed_task(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    state: str,
) -> str:
    """Insert a minimal :class:`Occurrence` in ``state``; return its id."""
    with factory() as s, tenant_agnostic():
        pid = new_ulid()
        s.add(
            Property(
                id=pid,
                address="1 Villa Sud Way",
                timezone="Europe/Paris",
                tags_json=[],
                created_at=_PINNED,
            )
        )
        oid = new_ulid()
        s.add(
            Occurrence(
                id=oid,
                workspace_id=workspace_id,
                schedule_id=None,
                template_id=None,
                property_id=pid,
                assignee_user_id=None,
                starts_at=_PINNED,
                ends_at=_PINNED + timedelta(minutes=30),
                scheduled_for_local="2026-05-05T14:00",
                originally_scheduled_for="2026-05-05T14:00",
                state=state,
                overdue_since=None,
                due_by_utc=None,
                completion_note_md=None,
                skipped_reason=None,
                cancellation_reason=None,
                title="Pool clean",
                description_md="",
                priority="normal",
                photo_evidence="disabled",
                duration_minutes=30,
                area_id=None,
                unit_id=None,
                expected_role_id=None,
                asset_id=None,
                asset_action_id=None,
                linked_instruction_ids=[],
                inventory_consumption_json={},
                is_personal=False,
                created_by_user_id=None,
                created_at=_PINNED,
            )
        )
        s.commit()
        return oid


def _seed_scoped_task(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    with_property: bool,
) -> tuple[str, str | None]:
    """Insert a task; return ``(task_id, property_id)`` (pid ``None`` when
    workspace-scoped)."""
    with factory() as s, tenant_agnostic():
        pid: str | None = None
        if with_property:
            pid = new_ulid()
            s.add(
                Property(
                    id=pid,
                    address="1 Villa Sud Way",
                    timezone="Europe/Paris",
                    tags_json=[],
                    created_at=_PINNED,
                )
            )
        oid = new_ulid()
        s.add(
            Occurrence(
                id=oid,
                workspace_id=workspace_id,
                schedule_id=None,
                template_id=None,
                property_id=pid,
                assignee_user_id=None,
                starts_at=_PINNED,
                ends_at=_PINNED + timedelta(minutes=30),
                scheduled_for_local="2026-05-05T14:00",
                originally_scheduled_for="2026-05-05T14:00",
                state="pending",
                overdue_since=None,
                due_by_utc=None,
                completion_note_md=None,
                skipped_reason=None,
                cancellation_reason=None,
                title="Pool clean",
                description_md="",
                priority="normal",
                photo_evidence="disabled",
                duration_minutes=30,
                area_id=None,
                unit_id=None,
                expected_role_id=None,
                asset_id=None,
                asset_action_id=None,
                linked_instruction_ids=[],
                inventory_consumption_json={},
                is_personal=False,
                created_by_user_id=None,
                created_at=_PINNED,
            )
        )
        s.commit()
        return oid, pid


def _cancel_ctx(workspace_id: str, slug: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=new_ulid(),
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )


def test_cancel_task_underlying_action_workspace_scoped(
    factory: sessionmaker[Session],
) -> None:
    """A workspace-scoped task cancel records ``tasks.skip_other`` at
    workspace scope (cd-9tsjw path-2 resolution)."""
    ws_id, _owner = _seed_workspace(factory, slug="cancel-ws")
    task_id, _pid = _seed_scoped_task(factory, workspace_id=ws_id, with_property=False)
    ctx = _cancel_ctx(ws_id, "cancel-ws")
    with factory() as s:
        ref = _cancel_task_underlying_action(s, ctx=ctx, task_id=task_id)
    assert ref is not None
    assert ref.action_key == "tasks.skip_other"
    assert ref.scope_kind == "workspace"
    assert ref.scope_id == ws_id


def test_cancel_task_underlying_action_property_scoped(
    factory: sessionmaker[Session],
) -> None:
    """A property-scoped task cancel records ``tasks.skip_other`` scoped to
    the task's property (cd-9tsjw path-2 resolution)."""
    ws_id, _owner = _seed_workspace(factory, slug="cancel-prop")
    task_id, pid = _seed_scoped_task(factory, workspace_id=ws_id, with_property=True)
    ctx = _cancel_ctx(ws_id, "cancel-prop")
    with factory() as s:
        ref = _cancel_task_underlying_action(s, ctx=ctx, task_id=task_id)
    assert ref is not None
    assert ref.scope_kind == "property"
    assert ref.scope_id == pid


def test_cancel_task_underlying_action_missing_task_is_none(
    factory: sessionmaker[Session],
) -> None:
    """A cancel targeting a nonexistent task resolves to ``None`` — the
    consumer falls back to own-conversation ownership + replay."""
    ws_id, _owner = _seed_workspace(factory, slug="cancel-missing")
    ctx = _cancel_ctx(ws_id, "cancel-missing")
    with factory() as s:
        ref = _cancel_task_underlying_action(s, ctx=ctx, task_id=new_ulid())
    assert ref is None


def _dispatch(call: ToolCall, *, headers: dict[str, str]) -> ToolResult:
    return InProcessApprovalDispatcher().dispatch(
        call,
        token=DelegatedToken(plaintext="replay", token_id="token_replay"),
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Guard branches (dispatch entry)
# ---------------------------------------------------------------------------


def test_unsupported_tool_returns_404() -> None:
    result = _dispatch(
        ToolCall(id="c1", name="tasks.frobnicate", input={}),
        headers=_MANAGER_HEADERS,
    )
    assert result.status_code == 404
    assert result.body == {"error": "unsupported_tool", "tool": "tasks.frobnicate"}
    assert result.mutated is False


def test_missing_replay_actor_returns_422() -> None:
    result = _dispatch(
        ToolCall(id="c1", name="cancel_task", input={}),
        headers={},
    )
    assert result.status_code == 422
    assert result.body == {"error": "missing_replay_actor"}
    assert result.mutated is False


# ---------------------------------------------------------------------------
# messaging.broadcast branches
# ---------------------------------------------------------------------------


def test_broadcast_invalid_input_returns_422() -> None:
    # ``recipient_user_ids`` is not a list → ``broadcast_tool_input``
    # returns None → the dispatcher rejects before opening a UoW.
    result = _dispatch(
        ToolCall(
            id="c1",
            name="messaging.broadcast",
            input={
                "workspace_slug": "ws",
                "broadcast_id": "b1",
                "subject": "Hi",
                "body_md": "Body",
                "recipient_user_ids": "not-a-list",
            },
        ),
        headers=_MANAGER_HEADERS,
    )
    assert result.status_code == 422
    assert result.body == {"error": "invalid_broadcast_input"}
    assert result.mutated is False


def test_broadcast_workspace_not_found_returns_404(
    factory: sessionmaker[Session], wire_uow: None
) -> None:
    result = _dispatch(
        ToolCall(
            id="c1",
            name="messaging.broadcast",
            input={
                "workspace_slug": "does-not-exist",
                "broadcast_id": "b1",
                "subject": "Hi",
                "body_md": "Body",
                "recipient_user_ids": ["user_1"],
            },
        ),
        headers=_MANAGER_HEADERS,
    )
    assert result.status_code == 404
    assert result.body == {"error": "workspace_not_found"}
    assert result.mutated is False


def test_broadcast_domain_validation_returns_422(
    factory: sessionmaker[Session], wire_uow: None
) -> None:
    # Valid input + a real workspace, but a recipient who is not
    # workspace staff → ``execute_broadcast`` raises ``Validation``
    # (``recipient_not_in_workspace``), which the dispatcher maps to
    # ``422`` carrying the domain error code.
    _, owner_id = _seed_workspace(factory, slug="broad-val")
    result = _dispatch(
        ToolCall(
            id="c1",
            name="messaging.broadcast",
            input={
                "workspace_slug": "broad-val",
                "broadcast_id": "b1",
                "subject": "Hi",
                "body_md": "Body",
                "recipient_user_ids": [new_ulid()],
            },
        ),
        headers={**_MANAGER_HEADERS, "X-Crewday-Replay-Actor-Id": owner_id},
    )
    assert result.status_code == 422
    assert result.body == {"error": "recipient_not_in_workspace"}
    assert result.mutated is False


# ---------------------------------------------------------------------------
# cancel_task branches
# ---------------------------------------------------------------------------


def test_cancel_task_invalid_input_returns_422() -> None:
    # Missing ``reason_md`` → the dispatcher rejects before any UoW.
    result = _dispatch(
        ToolCall(
            id="c1",
            name="cancel_task",
            input={"workspace_slug": "ws", "task_id": "t1"},
        ),
        headers=_MANAGER_HEADERS,
    )
    assert result.status_code == 422
    assert result.body == {"error": "invalid_cancel_task_input"}
    assert result.mutated is False


def test_cancel_task_workspace_not_found_returns_404(
    factory: sessionmaker[Session], wire_uow: None
) -> None:
    result = _dispatch(
        ToolCall(
            id="c1",
            name="cancel_task",
            input={
                "workspace_slug": "does-not-exist",
                "task_id": "t1",
                "reason_md": "cancel_requested",
            },
        ),
        headers=_MANAGER_HEADERS,
    )
    assert result.status_code == 404
    assert result.body == {"error": "workspace_not_found"}
    assert result.mutated is False


def test_cancel_task_not_found_returns_404(
    factory: sessionmaker[Session], wire_uow: None
) -> None:
    _, owner_id = _seed_workspace(factory, slug="cancel-missing")
    result = _dispatch(
        ToolCall(
            id="c1",
            name="cancel_task",
            input={
                "workspace_slug": "cancel-missing",
                "task_id": new_ulid(),
                "reason_md": "cancel_requested",
            },
        ),
        headers={**_MANAGER_HEADERS, "X-Crewday-Replay-Actor-Id": owner_id},
    )
    assert result.status_code == 404
    assert result.body == {"error": "task_not_found"}
    assert result.mutated is False


def test_cancel_task_permission_denied_returns_403(
    factory: sessionmaker[Session], wire_uow: None
) -> None:
    # A real, cancellable task — but the replay actor is a worker, and
    # ``cancel`` is owner / manager only → ``PermissionDenied`` → 403.
    ws_id, owner_id = _seed_workspace(factory, slug="cancel-forbidden")
    task_id = _seed_task(factory, workspace_id=ws_id, state="pending")
    result = _dispatch(
        ToolCall(
            id="c1",
            name="cancel_task",
            input={
                "workspace_slug": "cancel-forbidden",
                "task_id": task_id,
                "reason_md": "cancel_requested",
            },
        ),
        headers={
            "X-Crewday-Replay-Actor-Id": owner_id,
            "X-Crewday-Replay-Actor-Role": "worker",
            "X-Crewday-Replay-Actor-Is-Owner": "0",
        },
    )
    assert result.status_code == 403
    assert result.body == {"error": "task_cancel_forbidden"}
    assert result.mutated is False


def test_cancel_task_invalid_state_returns_409(
    factory: sessionmaker[Session], wire_uow: None
) -> None:
    # An already-cancelled task cannot transition to ``cancelled`` again
    # → ``InvalidStateTransition`` → 409.
    ws_id, owner_id = _seed_workspace(factory, slug="cancel-terminal")
    task_id = _seed_task(factory, workspace_id=ws_id, state="cancelled")
    result = _dispatch(
        ToolCall(
            id="c1",
            name="cancel_task",
            input={
                "workspace_slug": "cancel-terminal",
                "task_id": task_id,
                "reason_md": "cancel_requested",
            },
        ),
        headers={**_MANAGER_HEADERS, "X-Crewday-Replay-Actor-Id": owner_id},
    )
    assert result.status_code == 409
    assert result.body == {"error": "invalid_task_state"}
    assert result.mutated is False
