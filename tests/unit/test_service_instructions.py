"""Unit tests for :mod:`app.services.instructions.service`.

Mirrors the in-memory SQLite bootstrap in
``tests/unit/services/test_service_employees.py``: a fresh engine
per test, pull every sibling ``models`` module onto the shared
``Base.metadata``, run ``Base.metadata.create_all``, drive the
service through the SA-backed
:class:`SqlAlchemyInstructionsRepository` with a
:class:`FrozenClock`.

Covers cd-oyq:

* :func:`create` mints instruction + first revision atomically;
  ``instruction.current_version_id == revision.id``;
  ``revision.version_num == 1``.
* :func:`update_metadata` title / tags / scope changes do NOT
  create a revision; ``current_version_id`` unchanged.
* :func:`update_body` with the same body content — no version
  bump (idempotent).
* :func:`update_body` with new content — version monotonic;
  ``current_version_id`` updated; previous revision still exists.
* Scope validation: each violation case (global+property,
  property+missing-property, area+missing-area, area+property
  mismatch).
* Tag normalisation: trim/lowercase/dedupe/cap-20.
* :func:`archive` is idempotent; archiving twice doesn't raise.
* Editing an archived instruction raises
  :class:`ArchivedInstructionError`.
* Audit rows include ``revision_id`` for body edits but NOT for
  metadata-only edits.
* :func:`restore_to_revision` raises :class:`NotImplementedError`
  with a future-task pointer (the cd-t5j seam).

See ``docs/specs/07-instructions-kb.md`` §"Editing semantics" /
§"Retractable" and ``docs/specs/02-domain-model.md`` §"instruction"
/ §"instruction_version".
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.bootstrap import seed_owners_system_group
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User
from app.adapters.db.instructions.models import Instruction, InstructionVersion
from app.adapters.db.instructions.repositories import (
    SqlAlchemyInstructionsRepository,
)
from app.adapters.db.places.models import Area, Property, PropertyWorkspace
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.services.instructions.service import (
    ArchivedInstructionError,
    InstructionNotFound,
    ScopeValidationError,
    TagValidationError,
    archive,
    create,
    restore_to_revision,
    update_body,
    update_metadata,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="engine_instructions")
def fixture_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(name="session_instructions")
def fixture_session(engine_instructions: Engine) -> Iterator[Session]:
    factory = sessionmaker(
        bind=engine_instructions, expire_on_commit=False, class_=Session
    )
    with factory() as s:
        yield s


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------


def _bootstrap_user(session: Session, *, email: str, display_name: str) -> User:
    user = User(
        id=new_ulid(),
        email=email,
        email_lower=email.lower(),
        display_name=display_name,
        locale=None,
        timezone=None,
        created_at=_PINNED,
    )
    session.add(user)
    session.flush()
    return user


def _bootstrap_workspace(session: Session, *, slug: str) -> Workspace:
    ws = Workspace(
        id=new_ulid(),
        slug=slug,
        name=f"Workspace {slug}",
        plan="free",
        quota_json={},
        settings_json={},
        created_at=_PINNED,
    )
    session.add(ws)
    session.flush()
    return ws


def _seed_property(session: Session, *, ws: Workspace, label: str) -> Property:
    prop = Property(
        id=new_ulid(),
        name=label,
        kind="vacation",
        address=label,
        address_json={},
        country="FR",
        timezone="Europe/Paris",
        tags_json=[],
        welcome_defaults_json={},
        property_notes_md="",
        created_at=_PINNED,
        updated_at=_PINNED,
        deleted_at=None,
    )
    session.add(prop)
    session.flush()
    junction = PropertyWorkspace(
        property_id=prop.id,
        workspace_id=ws.id,
        label=label,
        membership_role="owner_workspace",
        share_guest_identity=False,
        auto_shift_from_occurrence=False,
        status="active",
        created_at=_PINNED,
    )
    session.add(junction)
    session.flush()
    return prop


def _seed_area(session: Session, *, prop: Property, label: str) -> Area:
    area = Area(
        id=new_ulid(),
        property_id=prop.id,
        unit_id=None,
        name=label,
        label=label,
        kind="indoor_room",
        icon=None,
        ordering=0,
        parent_area_id=None,
        notes_md="",
        created_at=_PINNED,
        updated_at=_PINNED,
        deleted_at=None,
    )
    session.add(area)
    session.flush()
    return area


def _owner_ctx(
    session: Session,
    *,
    user: User,
    ws: Workspace,
    clock: FrozenClock,
) -> WorkspaceContext:
    """Seed owners group + grant so the owner fixture passes ``instructions.edit``."""
    ctx = WorkspaceContext(
        workspace_id=ws.id,
        workspace_slug=ws.slug,
        actor_id=user.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )
    seed_owners_system_group(
        session,
        ctx,
        workspace_id=ws.id,
        owner_user_id=user.id,
        clock=clock,
    )
    session.flush()
    return ctx


def _audit_rows(session: Session, *, entity_id: str) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == entity_id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
    )


def _versions(session: Session, *, instruction_id: str) -> list[InstructionVersion]:
    return list(
        session.scalars(
            select(InstructionVersion)
            .where(InstructionVersion.instruction_id == instruction_id)
            .order_by(InstructionVersion.version_num.asc())
        ).all()
    )


def _bootstrap_owner(
    session: Session, *, slug: str, clock: FrozenClock
) -> tuple[Workspace, User, WorkspaceContext]:
    ws = _bootstrap_workspace(session, slug=slug)
    owner = _bootstrap_user(
        session, email=f"owner-{slug}@example.com", display_name=f"Owner {slug}"
    )
    ctx = _owner_ctx(session, user=owner, ws=ws, clock=clock)
    return ws, owner, ctx


# ---------------------------------------------------------------------------
# create()
# ---------------------------------------------------------------------------


class TestCreate:
    """``create`` mints instruction + v1 atomically."""

    def test_creates_instruction_and_first_revision(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        ws, _, ctx = _bootstrap_owner(session, slug="ws-create", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)

        result = create(
            repo,
            ctx,
            slug="house-rules",
            title="House Rules",
            body_md="# House rules\n\nNo shoes inside.",
            scope="global",
            tags=["safety", "general"],
            change_note="initial",
            clock=clock,
        )
        assert result.instruction.title == "House Rules"
        assert result.instruction.scope == "global"
        assert result.instruction.property_id is None
        assert result.instruction.area_id is None
        assert result.instruction.current_version_id == result.revision.id
        assert result.revision.version_num == 1
        assert result.revision.body_md.startswith("# House rules")
        assert result.revision.author_id == ctx.actor_id
        assert result.revision.change_note == "initial"

        # The DB carries one instruction + one version row, the
        # current_version_id is set, and the audit row carries the
        # freshly-minted revision_id (cd-oyq acceptance).
        instructions = list(
            session.scalars(
                select(Instruction).where(Instruction.workspace_id == ws.id)
            ).all()
        )
        assert len(instructions) == 1
        assert instructions[0].current_version_id == result.revision.id
        versions = _versions(session, instruction_id=result.instruction.id)
        assert [v.version_num for v in versions] == [1]
        audit = _audit_rows(session, entity_id=result.instruction.id)
        assert [r.action for r in audit] == ["instruction.created"]
        assert audit[0].diff["revision_id"] == result.revision.id
        assert audit[0].diff["version_num"] == 1

    def test_property_scope_records_property_id(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        ws, _, ctx = _bootstrap_owner(session, slug="ws-prop", clock=clock)
        prop = _seed_property(session, ws=ws, label="Villa Sud")
        repo = SqlAlchemyInstructionsRepository(session)

        result = create(
            repo,
            ctx,
            slug="villa-sud-keys",
            title="Keys at Villa Sud",
            body_md="Keys are under the pot.",
            scope="property",
            property_id=prop.id,
            tags=[],
            clock=clock,
        )
        # Stored shape projects back to the spec-narrow scope on the view.
        assert result.instruction.scope == "property"
        assert result.instruction.property_id == prop.id
        assert result.instruction.area_id is None

    def test_area_scope_mirrors_parent_property(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        ws, _, ctx = _bootstrap_owner(session, slug="ws-area", clock=clock)
        prop = _seed_property(session, ws=ws, label="Villa Sud")
        area = _seed_area(session, prop=prop, label="Pool")
        repo = SqlAlchemyInstructionsRepository(session)

        result = create(
            repo,
            ctx,
            slug="pool-safety",
            title="Pool safety",
            body_md="Never mix chlorine and pH-down.",
            scope="area",
            area_id=area.id,
            property_id=prop.id,
            tags=["safety"],
            clock=clock,
        )
        assert result.instruction.scope == "area"
        assert result.instruction.area_id == area.id
        assert result.instruction.property_id == prop.id


class TestCreateScopeValidation:
    """Scope-validation errors per the §07 constraint table."""

    def test_global_with_property_id_rejected(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        ws, _, ctx = _bootstrap_owner(session, slug="ws-gp", clock=clock)
        prop = _seed_property(session, ws=ws, label="Villa")
        repo = SqlAlchemyInstructionsRepository(session)

        with pytest.raises(ScopeValidationError) as exc_info:
            create(
                repo,
                ctx,
                slug="bad",
                title="Bad",
                body_md="x",
                scope="global",
                property_id=prop.id,
                clock=clock,
            )
        assert exc_info.value.field == "property_id"

    def test_global_with_area_id_rejected(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        ws, _, ctx = _bootstrap_owner(session, slug="ws-ga", clock=clock)
        prop = _seed_property(session, ws=ws, label="Villa")
        area = _seed_area(session, prop=prop, label="Pool")
        repo = SqlAlchemyInstructionsRepository(session)

        with pytest.raises(ScopeValidationError) as exc_info:
            create(
                repo,
                ctx,
                slug="bad",
                title="Bad",
                body_md="x",
                scope="global",
                area_id=area.id,
                clock=clock,
            )
        assert exc_info.value.field == "area_id"

    def test_property_without_property_id_rejected(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-pmiss", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)

        with pytest.raises(ScopeValidationError) as exc_info:
            create(
                repo,
                ctx,
                slug="bad",
                title="Bad",
                body_md="x",
                scope="property",
                clock=clock,
            )
        assert exc_info.value.field == "property_id"

    def test_property_with_unknown_property_id_rejected(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-punk", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)

        with pytest.raises(ScopeValidationError) as exc_info:
            create(
                repo,
                ctx,
                slug="bad",
                title="Bad",
                body_md="x",
                scope="property",
                property_id=new_ulid(),
                clock=clock,
            )
        assert exc_info.value.field == "property_id"

    def test_area_without_area_id_rejected(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-amiss", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)

        with pytest.raises(ScopeValidationError) as exc_info:
            create(
                repo,
                ctx,
                slug="bad",
                title="Bad",
                body_md="x",
                scope="area",
                clock=clock,
            )
        assert exc_info.value.field == "area_id"

    def test_area_with_property_mismatch_rejected(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        ws, _, ctx = _bootstrap_owner(session, slug="ws-amix", clock=clock)
        prop_a = _seed_property(session, ws=ws, label="Villa A")
        prop_b = _seed_property(session, ws=ws, label="Villa B")
        area_a = _seed_area(session, prop=prop_a, label="Kitchen")
        repo = SqlAlchemyInstructionsRepository(session)

        with pytest.raises(ScopeValidationError) as exc_info:
            create(
                repo,
                ctx,
                slug="bad",
                title="Bad",
                body_md="x",
                scope="area",
                area_id=area_a.id,
                property_id=prop_b.id,
                clock=clock,
            )
        assert exc_info.value.field == "property_id"

    def test_area_under_deleted_property_rejected(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        ws, _, ctx = _bootstrap_owner(session, slug="ws-adel", clock=clock)
        prop = _seed_property(session, ws=ws, label="Deleted Villa")
        area = _seed_area(session, prop=prop, label="Pool")
        prop.deleted_at = _PINNED
        session.flush()
        repo = SqlAlchemyInstructionsRepository(session)

        with pytest.raises(ScopeValidationError) as exc_info:
            create(
                repo,
                ctx,
                slug="bad",
                title="Bad",
                body_md="x",
                scope="area",
                area_id=area.id,
                clock=clock,
            )
        assert exc_info.value.field == "area_id"


class TestTagNormalisation:
    """Tags are trimmed, lower-cased, deduped, capped at 20."""

    def test_trim_lower_dedupe(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-tags", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)

        result = create(
            repo,
            ctx,
            slug="t",
            title="t",
            body_md="x",
            scope="global",
            tags=[" Safety ", "safety", "PETS", "pets", "  "],
            clock=clock,
        )
        # ``  `` collapses to empty; ``Safety``/``safety`` dedupe;
        # ``PETS``/``pets`` dedupe.
        assert result.instruction.tags == ("safety", "pets")

    def test_cap_at_twenty(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-tagcap", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)

        too_many = [f"tag-{i}" for i in range(21)]
        with pytest.raises(TagValidationError) as exc_info:
            create(
                repo,
                ctx,
                slug="t",
                title="t",
                body_md="x",
                scope="global",
                tags=too_many,
                clock=clock,
            )
        assert exc_info.value.limit == 20
        assert exc_info.value.field == "tags"


# ---------------------------------------------------------------------------
# update_metadata()
# ---------------------------------------------------------------------------


class TestUpdateMetadata:
    """Metadata edits do NOT bump the revision pointer."""

    def test_title_change_does_not_mint_revision(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-um", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)
        created = create(
            repo,
            ctx,
            slug="x",
            title="Old",
            body_md="body",
            scope="global",
            clock=clock,
        )
        original_rev = created.instruction.current_version_id

        view = update_metadata(
            repo,
            ctx,
            instruction_id=created.instruction.id,
            title="New Title",
            clock=clock,
        )
        assert view.title == "New Title"
        # Revision pointer unchanged.
        assert view.current_version_id == original_rev

        # Only one version row exists (cd-oyq acceptance: metadata
        # edits don't mint revisions).
        versions = _versions(session, instruction_id=created.instruction.id)
        assert [v.version_num for v in versions] == [1]

        audit = _audit_rows(session, entity_id=created.instruction.id)
        actions = [r.action for r in audit]
        assert actions == ["instruction.created", "instruction.metadata_updated"]
        # Metadata-update audit row does NOT carry revision_id (cd-oyq
        # "Audit rows include `revision_id` for body edits but NOT
        # for metadata-only edits").
        assert "revision_id" not in audit[-1].diff

    def test_tags_and_scope_changes_do_not_mint_revision(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        ws, _, ctx = _bootstrap_owner(session, slug="ws-um2", clock=clock)
        prop = _seed_property(session, ws=ws, label="Villa")
        repo = SqlAlchemyInstructionsRepository(session)
        created = create(
            repo,
            ctx,
            slug="x",
            title="t",
            body_md="body",
            scope="global",
            tags=["safety"],
            clock=clock,
        )
        original_rev = created.instruction.current_version_id

        view = update_metadata(
            repo,
            ctx,
            instruction_id=created.instruction.id,
            tags=["pets", "safety"],
            scope="property",
            property_id=prop.id,
            clock=clock,
        )
        assert view.scope == "property"
        assert view.property_id == prop.id
        assert view.tags == ("pets", "safety")
        assert view.current_version_id == original_rev
        versions = _versions(session, instruction_id=created.instruction.id)
        assert len(versions) == 1

    def test_no_op_metadata_update_writes_no_audit(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-noop", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)
        created = create(
            repo,
            ctx,
            slug="x",
            title="Same",
            body_md="body",
            scope="global",
            clock=clock,
        )
        update_metadata(
            repo,
            ctx,
            instruction_id=created.instruction.id,
            title="Same",
            clock=clock,
        )
        audit = _audit_rows(session, entity_id=created.instruction.id)
        assert [r.action for r in audit] == ["instruction.created"]

    def test_property_id_without_scope_rejected(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        """Refuse to guess at a scope change."""
        session = session_instructions
        ws, _, ctx = _bootstrap_owner(session, slug="ws-no-scope", clock=clock)
        prop = _seed_property(session, ws=ws, label="Villa")
        repo = SqlAlchemyInstructionsRepository(session)
        created = create(
            repo,
            ctx,
            slug="x",
            title="t",
            body_md="body",
            scope="global",
            clock=clock,
        )
        with pytest.raises(ScopeValidationError) as exc_info:
            update_metadata(
                repo,
                ctx,
                instruction_id=created.instruction.id,
                property_id=prop.id,
                clock=clock,
            )
        assert exc_info.value.field == "scope"


# ---------------------------------------------------------------------------
# update_body()
# ---------------------------------------------------------------------------


class TestUpdateBody:
    """Body edits hash + idempotency-check + monotonic version-num."""

    def test_same_body_is_idempotent(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-idem", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)
        created = create(
            repo,
            ctx,
            slug="x",
            title="t",
            body_md="hello world",
            scope="global",
            clock=clock,
        )
        result = update_body(
            repo,
            ctx,
            instruction_id=created.instruction.id,
            body_md="hello world",
            clock=clock,
        )
        # No version bump — current pointer + revision id unchanged.
        assert (
            result.instruction.current_version_id
            == created.instruction.current_version_id
        )
        assert result.revision.id == created.revision.id
        assert result.revision.version_num == 1
        versions = _versions(session, instruction_id=created.instruction.id)
        assert len(versions) == 1

        audit = _audit_rows(session, entity_id=created.instruction.id)
        # Idempotent save fires NO audit row.
        assert [r.action for r in audit] == ["instruction.created"]

    def test_new_body_bumps_version_monotonically(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-bump", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)
        created = create(
            repo,
            ctx,
            slug="x",
            title="t",
            body_md="v1 body",
            scope="global",
            clock=clock,
        )
        result_v2 = update_body(
            repo,
            ctx,
            instruction_id=created.instruction.id,
            body_md="v2 body",
            change_note="second pass",
            clock=clock,
        )
        assert result_v2.revision.version_num == 2
        assert result_v2.revision.id != created.revision.id
        assert result_v2.instruction.current_version_id == result_v2.revision.id

        # Previous revision is still in the DB (immutable history).
        versions = _versions(session, instruction_id=created.instruction.id)
        assert [v.version_num for v in versions] == [1, 2]
        assert versions[0].id == created.revision.id
        assert versions[0].body_md == "v1 body"
        assert versions[1].body_md == "v2 body"
        assert versions[1].change_note == "second pass"

        # cd-oyq acceptance: audit on body edit carries revision_id.
        audit = _audit_rows(session, entity_id=created.instruction.id)
        actions = [r.action for r in audit]
        assert actions == ["instruction.created", "instruction.body_updated"]
        body_diff = audit[-1].diff
        assert body_diff["revision_id"] == result_v2.revision.id
        assert body_diff["version_num"] == 2
        assert body_diff["previous_revision_id"] == created.revision.id

    def test_third_edit_lands_v3(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-vN", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)
        created = create(
            repo,
            ctx,
            slug="x",
            title="t",
            body_md="v1",
            scope="global",
            clock=clock,
        )
        update_body(
            repo, ctx, instruction_id=created.instruction.id, body_md="v2", clock=clock
        )
        v3 = update_body(
            repo, ctx, instruction_id=created.instruction.id, body_md="v3", clock=clock
        )
        assert v3.revision.version_num == 3
        versions = _versions(session, instruction_id=created.instruction.id)
        assert [v.version_num for v in versions] == [1, 2, 3]

    def test_body_hash_matches_migration_backfill_convention(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        """Pin the hash convention to the cd-d00j migration backfill.

        The migration computes ``sha256(body_md.encode("utf-8"))``
        with NO normalisation (raw UTF-8 bytes). If the service ever
        drifts (adds whitespace stripping, NFC normalisation, etc.)
        every existing row's hash would diverge from new writes,
        making every save after migration mint a fresh revision —
        breaking idempotency. This regression test pins the exact
        digest for one ASCII body and one UTF-8 body so a drift is
        caught at unit-test time, not in production.
        """
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-hash", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)

        ascii_body = "hello world"
        utf8_body = "café — naïve résumé"
        ascii_expected = hashlib.sha256(ascii_body.encode("utf-8")).hexdigest()
        utf8_expected = hashlib.sha256(utf8_body.encode("utf-8")).hexdigest()

        ascii_created = create(
            repo,
            ctx,
            slug="ascii",
            title="ascii",
            body_md=ascii_body,
            scope="global",
            clock=clock,
        )
        utf8_created = create(
            repo,
            ctx,
            slug="utf8",
            title="utf8",
            body_md=utf8_body,
            scope="global",
            clock=clock,
        )
        assert ascii_created.revision.body_hash == ascii_expected
        assert utf8_created.revision.body_hash == utf8_expected


# ---------------------------------------------------------------------------
# archive() + archived-edit guard
# ---------------------------------------------------------------------------


class TestArchive:
    """``archive`` is idempotent; archived rows refuse edits."""

    def test_archives_then_audit(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-arch", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)
        created = create(
            repo,
            ctx,
            slug="x",
            title="t",
            body_md="b",
            scope="global",
            clock=clock,
        )
        view = archive(repo, ctx, instruction_id=created.instruction.id, clock=clock)
        assert view.archived_at is not None
        audit = _audit_rows(session, entity_id=created.instruction.id)
        assert [r.action for r in audit] == [
            "instruction.created",
            "instruction.archived",
        ]
        assert audit[-1].diff["was_already_archived"] is False

    def test_archive_is_idempotent(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-arch2", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)
        created = create(
            repo,
            ctx,
            slug="x",
            title="t",
            body_md="b",
            scope="global",
            clock=clock,
        )
        first = archive(repo, ctx, instruction_id=created.instruction.id, clock=clock)
        # Second call: column is preserved (archive_at unchanged), no
        # exception.
        second = archive(repo, ctx, instruction_id=created.instruction.id, clock=clock)
        assert first.archived_at == second.archived_at

        audit = _audit_rows(session, entity_id=created.instruction.id)
        # Two archive audit rows; the second carries the
        # was_already_archived flag.
        assert [r.action for r in audit] == [
            "instruction.created",
            "instruction.archived",
            "instruction.archived",
        ]
        assert audit[-1].diff["was_already_archived"] is True

    def test_update_metadata_on_archived_raises(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-archfail", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)
        created = create(
            repo,
            ctx,
            slug="x",
            title="t",
            body_md="b",
            scope="global",
            clock=clock,
        )
        archive(repo, ctx, instruction_id=created.instruction.id, clock=clock)
        with pytest.raises(ArchivedInstructionError):
            update_metadata(
                repo,
                ctx,
                instruction_id=created.instruction.id,
                title="New",
                clock=clock,
            )

    def test_update_body_on_archived_raises(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-archbody", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)
        created = create(
            repo,
            ctx,
            slug="x",
            title="t",
            body_md="b",
            scope="global",
            clock=clock,
        )
        archive(repo, ctx, instruction_id=created.instruction.id, clock=clock)
        with pytest.raises(ArchivedInstructionError):
            update_body(
                repo,
                ctx,
                instruction_id=created.instruction.id,
                body_md="new",
                clock=clock,
            )


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


class TestLookups:
    """Cross-tenant + missing-id collapse to :class:`InstructionNotFound`."""

    def test_unknown_instruction_id_raises(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-404", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)
        with pytest.raises(InstructionNotFound):
            update_metadata(
                repo, ctx, instruction_id=new_ulid(), title="X", clock=clock
            )

    def test_cross_workspace_isolation(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        """A ws-A instruction is invisible to ws-B's owner."""
        session = session_instructions
        _, _, ctx_a = _bootstrap_owner(session, slug="ws-iso-a", clock=clock)
        _, _, ctx_b = _bootstrap_owner(session, slug="ws-iso-b", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)
        created = create(
            repo,
            ctx_a,
            slug="x",
            title="t",
            body_md="b",
            scope="global",
            clock=clock,
        )
        with pytest.raises(InstructionNotFound):
            update_metadata(
                repo,
                ctx_b,
                instruction_id=created.instruction.id,
                title="hijack",
                clock=clock,
            )


# ---------------------------------------------------------------------------
# restore_to_revision() seam
# ---------------------------------------------------------------------------


class TestRestoreSeam:
    """``restore_to_revision`` is a typed seam — raises for now."""

    def test_raises_not_implemented(
        self, session_instructions: Session, clock: FrozenClock
    ) -> None:
        session = session_instructions
        _, _, ctx = _bootstrap_owner(session, slug="ws-seam", clock=clock)
        repo = SqlAlchemyInstructionsRepository(session)
        with pytest.raises(NotImplementedError) as exc_info:
            restore_to_revision(
                repo,
                ctx,
                instruction_id=new_ulid(),
                revision_id=new_ulid(),
                clock=clock,
            )
        assert "p6.version.history" in str(exc_info.value)
        assert "cd-t5j" in str(exc_info.value)


# Silence the unused-import warnings for symbols imported purely to
# register metadata on :class:`Base`.
_ = (RoleGrant, PermissionGroup, PermissionGroupMember)
