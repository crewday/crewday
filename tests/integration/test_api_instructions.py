"""Integration test for :mod:`app.services.instructions.service`.

The cd-oyq slice ships the SERVICE layer only — the HTTP routes
land with cd-xkfe. This file drives the service against a real,
post-migration DB through the standard ``db_session`` rollback
fixture so the wiring is exercised end-to-end (migration columns +
audit writer + repository projections) without faking the SA layer.

Filename mirrors the test plan in the cd-oyq Beads task. Once
cd-xkfe lands the HTTP routes, the same file gains a sibling class
that drives the route through ``TestClient``; until then the
service-level happy-path here is what proves the wiring.

See ``docs/specs/07-instructions-kb.md`` §"Editing semantics" and
``docs/specs/02-domain-model.md`` §"instruction" /
§"instruction_version".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.instructions.models import Instruction, InstructionVersion
from app.adapters.db.instructions.repositories import (
    SqlAlchemyInstructionsRepository,
)
from app.adapters.db.places.models import Area, Property, PropertyWorkspace
from app.adapters.db.workspace.models import Workspace
from app.services.instructions.service import (
    archive,
    create,
    update_body,
    update_metadata,
)
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_LATER = _PINNED + timedelta(hours=1)


def _ctx_for(workspace: Workspace, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLI",
    )


def _seed_property(session: Session, *, ws: Workspace, label: str) -> Property:
    """Insert a Property + PropertyWorkspace junction in one helper."""
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


class TestServiceAgainstRealDb:
    """End-to-end service drive against the real (migrated) DB."""

    def test_create_then_body_edit_atomic_round_trip(self, db_session: Session) -> None:
        """create -> update_body -> archive against real migrations.

        Proves the full chain — repository projection, monotonic
        version-num, audit row carrying ``revision_id`` for body
        edits, archive idempotency — survives a real DB roundtrip
        with the standard rollback-on-exit fixture.
        """
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="owner@instructions.test",
            display_name="Owner",
            clock=clock,
        )
        workspace = bootstrap_workspace(
            db_session,
            slug="ws-instr-svc",
            name="Instructions Service Workspace",
            owner_user_id=user.id,
            clock=clock,
        )
        ctx = _ctx_for(workspace, user.id)
        token = set_current(ctx)
        try:
            prop = _seed_property(db_session, ws=workspace, label="Villa Sud")
            area = _seed_area(db_session, prop=prop, label="Pool")
            repo = SqlAlchemyInstructionsRepository(db_session)

            created = create(
                repo,
                ctx,
                slug="pool-safety",
                title="Pool safety",
                body_md="Never mix chlorine and pH-down.",
                scope="area",
                area_id=area.id,
                property_id=prop.id,
                tags=["safety", "pets"],
                change_note="initial",
                clock=clock,
            )
            assert created.revision.version_num == 1
            assert created.instruction.current_version_id == created.revision.id
            assert created.instruction.scope == "area"
            assert created.instruction.area_id == area.id
            assert created.instruction.property_id == prop.id

            # Same body — no bump (idempotent).
            idempotent = update_body(
                repo,
                ctx,
                instruction_id=created.instruction.id,
                body_md="Never mix chlorine and pH-down.",
                clock=clock,
            )
            assert idempotent.revision.id == created.revision.id

            # New body — version bumps to 2.
            clock.set(_LATER)
            bumped = update_body(
                repo,
                ctx,
                instruction_id=created.instruction.id,
                body_md="Never mix chlorine and pH-down. Call manager if cloudy.",
                change_note="cloudy water guidance",
                clock=clock,
            )
            assert bumped.revision.version_num == 2
            assert bumped.instruction.current_version_id == bumped.revision.id

            # Metadata-only edit doesn't move the version pointer.
            renamed = update_metadata(
                repo,
                ctx,
                instruction_id=created.instruction.id,
                title="Pool safety (revised)",
                clock=clock,
            )
            assert renamed.current_version_id == bumped.revision.id

            # Archive once — flag flips and the row's archived_at is
            # set; second archive is a no-op for the column.
            archived = archive(
                repo, ctx, instruction_id=created.instruction.id, clock=clock
            )
            assert archived.archived_at is not None
            second = archive(
                repo, ctx, instruction_id=created.instruction.id, clock=clock
            )
            assert second.archived_at == archived.archived_at

            # DB shape matches.
            versions = list(
                db_session.scalars(
                    select(InstructionVersion)
                    .where(InstructionVersion.instruction_id == created.instruction.id)
                    .order_by(InstructionVersion.version_num.asc())
                ).all()
            )
            assert [v.version_num for v in versions] == [1, 2]
            # Each version carries the sha256 hash of its body — matches
            # the cd-d00j migration's backfill convention.
            assert versions[0].body_md == "Never mix chlorine and pH-down."
            assert versions[1].body_md.startswith("Never mix chlorine")
            assert versions[0].body_hash != versions[1].body_hash

            instruction_row = db_session.get(Instruction, created.instruction.id)
            assert instruction_row is not None
            assert instruction_row.archived_at is not None
            assert instruction_row.current_version_id == bumped.revision.id

            # Audit row stream covers create + body bump + metadata
            # update + two archive calls. The body-update audit row
            # carries revision_id (cd-oyq acceptance).
            audit = list(
                db_session.scalars(
                    select(AuditLog)
                    .where(AuditLog.entity_id == created.instruction.id)
                    .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
                ).all()
            )
            actions = [r.action for r in audit]
            assert actions == [
                "instruction.created",
                "instruction.body_updated",
                "instruction.metadata_updated",
                "instruction.archived",
                "instruction.archived",
            ]
            # Body-update audit carries revision_id; metadata-update
            # audit does NOT.
            body_audit = audit[1]
            assert body_audit.diff["revision_id"] == bumped.revision.id
            metadata_audit = audit[2]
            assert "revision_id" not in metadata_audit.diff
        finally:
            reset_current(token)
