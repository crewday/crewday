"""Version-history unit tests for instruction revisions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import PermissionGroup, PermissionGroupMember
from app.adapters.db.instructions.repositories import SqlAlchemyInstructionsRepository
from app.services.instructions.service import (
    ArchivedInstructionError,
    CurrentRevisionRestoreRejected,
    InstructionNotFound,
    archive,
    create,
    list_revisions,
    restore_to_revision,
    update_body,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from tests.unit.test_service_instructions import (
    _audit_rows,
    _bootstrap_owner,
    _bootstrap_user,
    _versions,
)
from tests.unit.test_service_instructions import (
    fixture_engine as fixture_engine,
)
from tests.unit.test_service_instructions import (
    fixture_session as fixture_session,
)

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


def _seed_second_owner(
    session: Session,
    *,
    workspace_id: str,
    workspace_slug: str,
    first_owner_id: str,
    clock: FrozenClock,
) -> WorkspaceContext:
    user = _bootstrap_user(
        session,
        email=f"second-{workspace_id}@example.com",
        display_name="Second Owner",
    )
    group_id = session.scalars(
        select(PermissionGroup.id).where(
            PermissionGroup.workspace_id == workspace_id,
            PermissionGroup.slug == "owners",
        )
    ).one()
    session.add(
        PermissionGroupMember(
            group_id=group_id,
            user_id=user.id,
            workspace_id=workspace_id,
            added_at=clock.now(),
            added_by_user_id=first_owner_id,
        )
    )
    session.flush()
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=user.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL2",
    )


def test_list_revisions_newest_first_with_cursor(
    session_instructions: Session, clock: FrozenClock
) -> None:
    session = session_instructions
    _, _, ctx = _bootstrap_owner(session, slug="ws-rev-list", clock=clock)
    repo = SqlAlchemyInstructionsRepository(session)
    created = create(
        repo,
        ctx,
        slug="pool-rules",
        title="Pool rules",
        body_md="v1",
        scope="global",
        change_note="initial",
        clock=clock,
    )
    update_body(
        repo,
        ctx,
        instruction_id=created.instruction.id,
        body_md="v2",
        change_note="second",
        clock=clock,
    )
    update_body(
        repo,
        ctx,
        instruction_id=created.instruction.id,
        body_md="v3",
        change_note="third",
        clock=clock,
    )

    page1 = list_revisions(repo, ctx, created.instruction.id, limit=2)
    assert [row.version_num for row in page1.data] == [3, 2]
    assert [row.change_note for row in page1.data] == ["third", "second"]
    assert all(row.author_id == ctx.actor_id for row in page1.data)
    assert page1.has_more is True
    assert page1.next_cursor == page1.data[-1].id

    page2 = list_revisions(
        repo,
        ctx,
        created.instruction.id,
        limit=2,
        cursor=page1.next_cursor,
    )
    assert [row.version_num for row in page2.data] == [1]
    assert page2.data[0].body_md == "v1"
    assert page2.has_more is False
    assert page2.next_cursor is None


def test_list_revisions_cross_instruction_or_workspace_cursor_returns_empty_page(
    session_instructions: Session, clock: FrozenClock
) -> None:
    session = session_instructions
    _, _, ctx_a = _bootstrap_owner(session, slug="ws-rev-cursor-a", clock=clock)
    _, _, ctx_b = _bootstrap_owner(session, slug="ws-rev-cursor-b", clock=clock)
    repo = SqlAlchemyInstructionsRepository(session)
    first = create(
        repo,
        ctx_a,
        slug="first",
        title="First",
        body_md="first v1",
        scope="global",
        clock=clock,
    )
    second = create(
        repo,
        ctx_a,
        slug="second",
        title="Second",
        body_md="second v1",
        scope="global",
        clock=clock,
    )
    other_workspace = create(
        repo,
        ctx_b,
        slug="other",
        title="Other",
        body_md="other v1",
        scope="global",
        clock=clock,
    )

    cross_instruction = list_revisions(
        repo,
        ctx_a,
        first.instruction.id,
        cursor=second.revision.id,
    )
    cross_workspace = list_revisions(
        repo,
        ctx_a,
        first.instruction.id,
        cursor=other_workspace.revision.id,
    )

    assert cross_instruction.data == ()
    assert cross_instruction.next_cursor is None
    assert cross_instruction.has_more is False
    assert cross_workspace.data == ()
    assert cross_workspace.next_cursor is None
    assert cross_workspace.has_more is False


def test_restore_to_revision_mints_new_revision_and_audit(
    session_instructions: Session, clock: FrozenClock
) -> None:
    session = session_instructions
    _, _, ctx = _bootstrap_owner(session, slug="ws-restore", clock=clock)
    repo = SqlAlchemyInstructionsRepository(session)
    created = create(
        repo,
        ctx,
        slug="house-rules",
        title="House rules",
        body_md="v1 body",
        scope="global",
        clock=clock,
    )
    update_body(repo, ctx, instruction_id=created.instruction.id, body_md="v2 body")
    update_body(repo, ctx, instruction_id=created.instruction.id, body_md="v3 body")

    restored = restore_to_revision(
        repo,
        ctx,
        instruction_id=created.instruction.id,
        revision_id=created.revision.id,
        change_note="rollback",
        clock=clock,
    )

    assert restored.revision.version_num == 4
    assert restored.revision.body_md == "v1 body"
    assert restored.revision.change_note == "rollback"
    assert restored.revision.author_id == ctx.actor_id
    assert restored.instruction.current_version_id == restored.revision.id

    restore_audit = [
        row
        for row in _audit_rows(session, entity_id=created.instruction.id)
        if row.action == "instruction.restored"
    ]
    assert len(restore_audit) == 1
    assert restore_audit[0].diff["restored_from_revision_id"] == created.revision.id
    assert restore_audit[0].diff["revision_id"] == restored.revision.id
    assert restore_audit[0].diff["version_num"] == 4


def test_restore_to_revision_default_change_note_matches_restore_prefix(
    session_instructions: Session, clock: FrozenClock
) -> None:
    session = session_instructions
    _, _, ctx = _bootstrap_owner(session, slug="ws-restore-note", clock=clock)
    repo = SqlAlchemyInstructionsRepository(session)
    created = create(
        repo,
        ctx,
        slug="note",
        title="Note",
        body_md="v1",
        scope="global",
        clock=clock,
    )
    update_body(repo, ctx, instruction_id=created.instruction.id, body_md="v2")

    restored = restore_to_revision(
        repo,
        ctx,
        instruction_id=created.instruction.id,
        revision_id=created.revision.id,
        clock=clock,
    )

    assert restored.revision.change_note == "Restored from v1: "


def test_restore_current_revision_rejected(
    session_instructions: Session, clock: FrozenClock
) -> None:
    session = session_instructions
    _, _, ctx = _bootstrap_owner(session, slug="ws-current", clock=clock)
    repo = SqlAlchemyInstructionsRepository(session)
    created = create(
        repo,
        ctx,
        slug="current",
        title="Current",
        body_md="body",
        scope="global",
        clock=clock,
    )

    with pytest.raises(CurrentRevisionRestoreRejected):
        restore_to_revision(
            repo,
            ctx,
            instruction_id=created.instruction.id,
            revision_id=created.revision.id,
            clock=clock,
        )

    assert [
        row.version_num
        for row in _versions(session, instruction_id=created.instruction.id)
    ] == [1]


def test_restore_by_non_author_allowed_and_records_caller(
    session_instructions: Session, clock: FrozenClock
) -> None:
    session = session_instructions
    ws, owner, ctx = _bootstrap_owner(session, slug="ws-other", clock=clock)
    repo = SqlAlchemyInstructionsRepository(session)
    created = create(
        repo,
        ctx,
        slug="keys",
        title="Keys",
        body_md="v1",
        scope="global",
        clock=clock,
    )
    update_body(repo, ctx, instruction_id=created.instruction.id, body_md="v2")
    other_ctx = _seed_second_owner(
        session,
        workspace_id=ws.id,
        workspace_slug=ws.slug,
        first_owner_id=owner.id,
        clock=clock,
    )

    restored = restore_to_revision(
        repo,
        other_ctx,
        instruction_id=created.instruction.id,
        revision_id=created.revision.id,
        clock=clock,
    )

    assert restored.revision.author_id == other_ctx.actor_id
    assert restored.revision.body_md == "v1"


def test_restore_cross_instruction_revision_rejected(
    session_instructions: Session, clock: FrozenClock
) -> None:
    session = session_instructions
    _, _, ctx = _bootstrap_owner(session, slug="ws-cross-instruction", clock=clock)
    repo = SqlAlchemyInstructionsRepository(session)
    first = create(
        repo,
        ctx,
        slug="first",
        title="First",
        body_md="first",
        scope="global",
        clock=clock,
    )
    second = create(
        repo,
        ctx,
        slug="second",
        title="Second",
        body_md="second",
        scope="global",
        clock=clock,
    )

    with pytest.raises(InstructionNotFound):
        restore_to_revision(
            repo,
            ctx,
            instruction_id=first.instruction.id,
            revision_id=second.revision.id,
            clock=clock,
        )

    assert [
        row.version_num
        for row in _versions(session, instruction_id=first.instruction.id)
    ] == [1]


def test_restore_cross_workspace_revision_rejected(
    session_instructions: Session, clock: FrozenClock
) -> None:
    session = session_instructions
    _, _, ctx_a = _bootstrap_owner(session, slug="ws-cross-rev-a", clock=clock)
    _, _, ctx_b = _bootstrap_owner(session, slug="ws-cross-rev-b", clock=clock)
    repo = SqlAlchemyInstructionsRepository(session)
    local = create(
        repo,
        ctx_a,
        slug="local",
        title="Local",
        body_md="local v1",
        scope="global",
        clock=clock,
    )
    foreign = create(
        repo,
        ctx_b,
        slug="foreign",
        title="Foreign",
        body_md="foreign v1",
        scope="global",
        clock=clock,
    )

    with pytest.raises(InstructionNotFound):
        restore_to_revision(
            repo,
            ctx_a,
            instruction_id=local.instruction.id,
            revision_id=foreign.revision.id,
            clock=clock,
        )

    assert [
        row.version_num
        for row in _versions(session, instruction_id=local.instruction.id)
    ] == [1]


def test_restore_archived_instruction_rejected_without_mutation(
    session_instructions: Session, clock: FrozenClock
) -> None:
    session = session_instructions
    _, _, ctx = _bootstrap_owner(session, slug="ws-restore-archived", clock=clock)
    repo = SqlAlchemyInstructionsRepository(session)
    created = create(
        repo,
        ctx,
        slug="archived",
        title="Archived",
        body_md="v1",
        scope="global",
        clock=clock,
    )
    update_body(repo, ctx, instruction_id=created.instruction.id, body_md="v2")
    archive(repo, ctx, instruction_id=created.instruction.id, clock=clock)

    with pytest.raises(ArchivedInstructionError):
        restore_to_revision(
            repo,
            ctx,
            instruction_id=created.instruction.id,
            revision_id=created.revision.id,
            clock=clock,
        )

    assert [
        row.version_num
        for row in _versions(session, instruction_id=created.instruction.id)
    ] == [1, 2]
    assert [
        row.action
        for row in _audit_rows(session, entity_id=created.instruction.id)
        if row.action == "instruction.restored"
    ] == []


def test_restore_does_not_mutate_source_revision(
    session_instructions: Session, clock: FrozenClock
) -> None:
    session = session_instructions
    _, _, ctx = _bootstrap_owner(session, slug="ws-immutable", clock=clock)
    repo = SqlAlchemyInstructionsRepository(session)
    created = create(
        repo,
        ctx,
        slug="immutable",
        title="Immutable",
        body_md="original",
        scope="global",
        change_note="first",
        clock=clock,
    )
    update_body(repo, ctx, instruction_id=created.instruction.id, body_md="changed")
    source_before = repo.get_version(
        workspace_id=ctx.workspace_id,
        version_id=created.revision.id,
    )
    assert source_before is not None

    restored = restore_to_revision(
        repo,
        ctx,
        instruction_id=created.instruction.id,
        revision_id=created.revision.id,
        clock=clock,
    )

    source_after = repo.get_version(
        workspace_id=ctx.workspace_id,
        version_id=created.revision.id,
    )
    assert source_after == source_before
    assert restored.revision.id != created.revision.id
    assert restored.revision.body_md == source_before.body_md
