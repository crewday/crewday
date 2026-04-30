"""Unit tests for instruction scope resolution."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.instructions.repositories import SqlAlchemyInstructionsRepository
from app.adapters.db.session import make_engine
from app.services.instructions.service import resolve_instructions
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.unit.test_service_instructions import (
    _bootstrap_owner,
    _load_all_models,
    _seed_area,
    _seed_property,
)

_PINNED = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(name="engine_instructions_resolver")
def fixture_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(name="session_instructions_resolver")
def fixture_session(engine_instructions_resolver: Engine) -> Iterator[Session]:
    factory = sessionmaker(
        bind=engine_instructions_resolver,
        expire_on_commit=False,
        class_=Session,
    )
    with factory() as s:
        yield s


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


def _seed_current_instruction(
    repo: SqlAlchemyInstructionsRepository,
    ctx: WorkspaceContext,
    *,
    slug: str,
    scope_kind: str,
    scope_id: str | None,
    body_md: str,
    archived: bool = False,
) -> str:
    instruction_id = new_ulid()
    revision_id = new_ulid()
    repo.insert_instruction(
        instruction_id=instruction_id,
        workspace_id=ctx.workspace_id,
        slug=slug,
        title=slug,
        scope_kind=scope_kind,
        scope_id=scope_id,
        tags=(),
        created_by=ctx.actor_id,
        created_at=_PINNED,
    )
    repo.insert_version(
        version_id=revision_id,
        workspace_id=ctx.workspace_id,
        instruction_id=instruction_id,
        version_num=1,
        body_md=body_md,
        body_hash=hashlib.sha256(body_md.encode("utf-8")).hexdigest(),
        author_id=ctx.actor_id,
        change_note=None,
        created_at=_PINNED,
    )
    repo.set_current_version(
        workspace_id=ctx.workspace_id,
        instruction_id=instruction_id,
        version_id=revision_id,
    )
    if archived:
        repo.set_archived_at(
            workspace_id=ctx.workspace_id,
            instruction_id=instruction_id,
            archived_at=_PINNED,
        )
    return instruction_id


def _current_revision_id(
    session: Session, *, instruction_id: str, workspace_id: str
) -> str:
    repo = SqlAlchemyInstructionsRepository(session)
    row = repo.get_instruction(
        workspace_id=workspace_id,
        instruction_id=instruction_id,
    )
    assert row is not None
    assert row.current_version_id is not None
    return row.current_version_id


def _link(
    repo: SqlAlchemyInstructionsRepository,
    ctx: WorkspaceContext,
    *,
    instruction_id: str,
    target_kind: str,
    target_id: str,
) -> None:
    repo.insert_instruction_link(
        link_id=new_ulid(),
        workspace_id=ctx.workspace_id,
        instruction_id=instruction_id,
        target_kind=target_kind,
        target_id=target_id,
        added_by=ctx.actor_id,
        added_at=_PINNED,
    )


def test_empty_context_returns_live_globals_only(
    session_instructions_resolver: Session,
    clock: FrozenClock,
) -> None:
    session = session_instructions_resolver
    ws, _, ctx = _bootstrap_owner(session, slug="resolver-empty", clock=clock)
    prop = _seed_property(session, ws=ws, label="Villa")
    repo = SqlAlchemyInstructionsRepository(session)
    global_id = _seed_current_instruction(
        repo,
        ctx,
        slug="global",
        scope_kind="workspace",
        scope_id=None,
        body_md="global body",
    )
    _seed_current_instruction(
        repo,
        ctx,
        slug="archived-global",
        scope_kind="workspace",
        scope_id=None,
        body_md="archived body",
        archived=True,
    )
    _seed_current_instruction(
        repo,
        ctx,
        slug="property",
        scope_kind="property",
        scope_id=prop.id,
        body_md="property body",
    )

    resolved = resolve_instructions(repo, ctx)

    assert [
        (
            row.instruction_id,
            row.current_revision_id,
            row.provenance,
            row.body_md,
        )
        for row in resolved
    ] == [
        (
            global_id,
            _current_revision_id(
                session,
                instruction_id=global_id,
                workspace_id=ctx.workspace_id,
            ),
            "scope:global",
            "global body",
        )
    ]


def test_property_context_returns_property_before_global(
    session_instructions_resolver: Session,
    clock: FrozenClock,
) -> None:
    session = session_instructions_resolver
    ws, _, ctx = _bootstrap_owner(session, slug="resolver-property", clock=clock)
    prop = _seed_property(session, ws=ws, label="Villa")
    repo = SqlAlchemyInstructionsRepository(session)
    global_id = _seed_current_instruction(
        repo,
        ctx,
        slug="global",
        scope_kind="workspace",
        scope_id=None,
        body_md="global body",
    )
    property_id = _seed_current_instruction(
        repo,
        ctx,
        slug="property",
        scope_kind="property",
        scope_id=prop.id,
        body_md="property body",
    )

    resolved = resolve_instructions(repo, ctx, property_id=prop.id)

    assert [(row.instruction_id, row.provenance) for row in resolved] == [
        (property_id, "scope:property"),
        (global_id, "scope:global"),
    ]


def test_area_context_implies_property_and_orders_specific_first(
    session_instructions_resolver: Session,
    clock: FrozenClock,
) -> None:
    session = session_instructions_resolver
    ws, _, ctx = _bootstrap_owner(session, slug="resolver-area", clock=clock)
    prop = _seed_property(session, ws=ws, label="Villa")
    area = _seed_area(session, prop=prop, label="Pool")
    repo = SqlAlchemyInstructionsRepository(session)
    global_id = _seed_current_instruction(
        repo,
        ctx,
        slug="global",
        scope_kind="workspace",
        scope_id=None,
        body_md="global body",
    )
    property_id = _seed_current_instruction(
        repo,
        ctx,
        slug="property",
        scope_kind="property",
        scope_id=prop.id,
        body_md="property body",
    )
    area_id = _seed_current_instruction(
        repo,
        ctx,
        slug="area",
        scope_kind="area",
        scope_id=area.id,
        body_md="area body",
    )

    resolved = resolve_instructions(repo, ctx, area_id=area.id)

    assert [(row.instruction_id, row.provenance) for row in resolved] == [
        (area_id, "scope:area"),
        (property_id, "scope:property"),
        (global_id, "scope:global"),
    ]


def test_area_context_uses_area_parent_over_mismatched_property(
    session_instructions_resolver: Session,
    clock: FrozenClock,
) -> None:
    session = session_instructions_resolver
    ws, _, ctx = _bootstrap_owner(session, slug="resolver-area-parent", clock=clock)
    area_prop = _seed_property(session, ws=ws, label="Villa")
    wrong_prop = _seed_property(session, ws=ws, label="Chalet")
    area = _seed_area(session, prop=area_prop, label="Pool")
    repo = SqlAlchemyInstructionsRepository(session)
    area_property_id = _seed_current_instruction(
        repo,
        ctx,
        slug="area-property",
        scope_kind="property",
        scope_id=area_prop.id,
        body_md="area property body",
    )
    wrong_property_id = _seed_current_instruction(
        repo,
        ctx,
        slug="wrong-property",
        scope_kind="property",
        scope_id=wrong_prop.id,
        body_md="wrong property body",
    )
    area_id = _seed_current_instruction(
        repo,
        ctx,
        slug="area",
        scope_kind="area",
        scope_id=area.id,
        body_md="area body",
    )

    resolved = resolve_instructions(
        repo,
        ctx,
        area_id=area.id,
        property_id=wrong_prop.id,
    )

    assert [(row.instruction_id, row.provenance) for row in resolved] == [
        (area_id, "scope:area"),
        (area_property_id, "scope:property"),
    ]
    assert wrong_property_id not in {row.instruction_id for row in resolved}


def test_unknown_area_does_not_resolve_area_scoped_instruction(
    session_instructions_resolver: Session,
    clock: FrozenClock,
) -> None:
    session = session_instructions_resolver
    ws, _, ctx = _bootstrap_owner(session, slug="resolver-unknown-area", clock=clock)
    prop = _seed_property(session, ws=ws, label="Villa")
    area = _seed_area(session, prop=prop, label="Pool")
    repo = SqlAlchemyInstructionsRepository(session)
    property_id = _seed_current_instruction(
        repo,
        ctx,
        slug="property",
        scope_kind="property",
        scope_id=prop.id,
        body_md="property body",
    )
    area_id = _seed_current_instruction(
        repo,
        ctx,
        slug="area",
        scope_kind="area",
        scope_id=area.id,
        body_md="area body",
    )
    area.deleted_at = _PINNED
    session.flush()

    resolved = resolve_instructions(
        repo,
        ctx,
        area_id=area.id,
        property_id=prop.id,
    )

    assert [(row.instruction_id, row.provenance) for row in resolved] == [
        (property_id, "scope:property")
    ]
    assert area_id not in {row.instruction_id for row in resolved}


def test_links_append_after_scopes_in_deterministic_order(
    session_instructions_resolver: Session,
    clock: FrozenClock,
) -> None:
    session = session_instructions_resolver
    ws, _, ctx = _bootstrap_owner(session, slug="resolver-links", clock=clock)
    prop = _seed_property(session, ws=ws, label="Villa")
    repo = SqlAlchemyInstructionsRepository(session)
    property_instruction_id = _seed_current_instruction(
        repo,
        ctx,
        slug="property",
        scope_kind="property",
        scope_id=prop.id,
        body_md="property body",
    )
    targets = (
        ("task_template", "template-1", "link:task_template"),
        ("schedule", "schedule-1", "link:schedule"),
        ("work_role", "role-1", "link:work_role"),
        ("task", "task-1", "link:task"),
        ("asset", "asset-1", "link:asset"),
        ("stay", "stay-1", "link:stay"),
    )
    linked_ids: list[str] = []
    for target_kind, target_id, _ in reversed(targets):
        instruction_id = _seed_current_instruction(
            repo,
            ctx,
            slug=f"linked-{target_kind}",
            scope_kind="template",
            scope_id=target_id,
            body_md=f"{target_kind} body",
        )
        _link(
            repo,
            ctx,
            instruction_id=instruction_id,
            target_kind=target_kind,
            target_id=target_id,
        )
        linked_ids.insert(0, instruction_id)

    resolved = resolve_instructions(
        repo,
        ctx,
        property_id=prop.id,
        template_id="template-1",
        schedule_id="schedule-1",
        work_role_id="role-1",
        task_id="task-1",
        asset_id="asset-1",
        stay_id="stay-1",
    )

    assert [(row.instruction_id, row.provenance) for row in resolved] == [
        (property_instruction_id, "scope:property"),
        *[
            (instruction_id, provenance)
            for instruction_id, (_, _, provenance) in zip(
                linked_ids,
                targets,
                strict=True,
            )
        ],
    ]


def test_deduplicates_with_highest_specificity_provenance(
    session_instructions_resolver: Session,
    clock: FrozenClock,
) -> None:
    session = session_instructions_resolver
    ws, _, ctx = _bootstrap_owner(session, slug="resolver-dedupe", clock=clock)
    prop = _seed_property(session, ws=ws, label="Villa")
    area = _seed_area(session, prop=prop, label="Pool")
    repo = SqlAlchemyInstructionsRepository(session)
    instruction_id = _seed_current_instruction(
        repo,
        ctx,
        slug="area",
        scope_kind="area",
        scope_id=area.id,
        body_md="area body",
    )
    _link(
        repo,
        ctx,
        instruction_id=instruction_id,
        target_kind="task",
        target_id="task-1",
    )

    resolved = resolve_instructions(
        repo,
        ctx,
        area_id=area.id,
        task_id="task-1",
    )

    assert [(row.instruction_id, row.provenance) for row in resolved] == [
        (instruction_id, "scope:area")
    ]


def test_link_insert_rejects_instruction_outside_workspace(
    session_instructions_resolver: Session,
    clock: FrozenClock,
) -> None:
    session = session_instructions_resolver
    _, _, source_ctx = _bootstrap_owner(
        session,
        slug="resolver-link-source",
        clock=clock,
    )
    _, _, other_ctx = _bootstrap_owner(
        session,
        slug="resolver-link-other",
        clock=clock,
    )
    repo = SqlAlchemyInstructionsRepository(session)
    instruction_id = _seed_current_instruction(
        repo,
        source_ctx,
        slug="source",
        scope_kind="workspace",
        scope_id=None,
        body_md="source body",
    )

    with pytest.raises(LookupError):
        repo.insert_instruction_link(
            link_id=new_ulid(),
            workspace_id=other_ctx.workspace_id,
            instruction_id=instruction_id,
            target_kind="task",
            target_id="task-1",
            added_by=other_ctx.actor_id,
            added_at=_PINNED,
        )
