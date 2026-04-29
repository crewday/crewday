"""Integration coverage for stay bundle lifecycle generation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import Property
from app.adapters.db.stays.models import Reservation, StayBundle
from app.adapters.db.workspace.models import Workspace
from app.domain.stays.bundle_service import (
    StayLifecycleRule,
    generate_bundles_for_stay,
    reapply_bundles_for_stay,
)
from app.ports.tasks_create_occurrence import RecordingTasksCreateOccurrencePort
from app.tenancy import reset_current, set_current, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_ACTOR_ID = "01HWA00000000000000000USR1"
_CORRELATION_ID = "01HWA00000000000000000CRL1"


def _bootstrap_workspace(session: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    with tenant_agnostic():
        session.add(
            Workspace(
                id=workspace_id,
                slug=slug,
                name=f"Workspace {slug}",
                plan="free",
                quota_json={},
                settings_json={},
                created_at=_PINNED,
            )
        )
        session.flush()
    return workspace_id


def _bootstrap_property(session: Session) -> str:
    pid = new_ulid()
    with tenant_agnostic():
        session.add(
            Property(
                id=pid,
                name="Villa Sud",
                kind="str",
                address="12 Chemin des Oliviers",
                address_json={"country": "FR"},
                country="FR",
                locale=None,
                default_currency=None,
                timezone="Europe/Paris",
                lat=None,
                lon=None,
                client_org_id=None,
                owner_user_id=None,
                tags_json=[],
                welcome_defaults_json={},
                property_notes_md="",
                created_at=_PINNED,
                updated_at=_PINNED,
                deleted_at=None,
            )
        )
        session.flush()
    return pid


def _bootstrap_reservation(
    session: Session,
    *,
    workspace_id: str,
    property_id: str,
    check_in: datetime,
    check_out: datetime,
) -> str:
    rid = new_ulid()
    session.add(
        Reservation(
            id=rid,
            workspace_id=workspace_id,
            property_id=property_id,
            ical_feed_id=None,
            external_uid=f"manual-{rid}",
            check_in=check_in,
            check_out=check_out,
            guest_name="A. Test Guest",
            guest_count=2,
            status="scheduled",
            source="manual",
            raw_summary=None,
            raw_description=None,
            guest_link_id=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return rid


def _ctx(workspace_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="bundle-lifecycle",
        actor_id=_ACTOR_ID,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=_CORRELATION_ID,
    )


def test_bundle_generation_is_idempotent_and_reapply_records_reschedule(
    db_session: Session,
) -> None:
    workspace_id = _bootstrap_workspace(
        db_session, slug=f"bundle-lifecycle-{new_ulid()[-6:]}"
    )
    ctx = _ctx(workspace_id)
    token = set_current(ctx)
    try:
        prop = _bootstrap_property(db_session)
        check_in = _PINNED + timedelta(days=1)
        check_out = _PINNED + timedelta(days=4)
        rid = _bootstrap_reservation(
            db_session,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_in,
            check_out=check_out,
        )
        _bootstrap_reservation(
            db_session,
            workspace_id=ctx.workspace_id,
            property_id=prop,
            check_in=check_out + timedelta(days=1),
            check_out=check_out + timedelta(days=4),
        )
        rules = (
            StayLifecycleRule(
                id="rule-after-integration",
                trigger="after_checkout",
                duration=timedelta(hours=2),
                kind="turnover",
                guest_kind_filter=("guest",),
            ),
        )
        port = RecordingTasksCreateOccurrencePort()

        generate_bundles_for_stay(
            db_session,
            ctx,
            reservation_id=rid,
            port=port,
            rules=rules,
            now=_PINNED,
        )
        generate_bundles_for_stay(
            db_session,
            ctx,
            reservation_id=rid,
            port=port,
            rules=rules,
            now=_PINNED,
        )
        bundles = db_session.scalars(
            select(StayBundle).where(StayBundle.reservation_id == rid)
        ).all()
        assert len(bundles) == 1
        assert port.calls[0].stay_task_bundle_id == bundles[0].id
        assert port.calls[1].stay_task_bundle_id == bundles[0].id

        reservation = db_session.get(Reservation, rid)
        assert reservation is not None
        previous_check_in = reservation.check_in
        previous_check_out = reservation.check_out
        reservation.check_out = previous_check_out + timedelta(hours=4)
        reapply = reapply_bundles_for_stay(
            db_session,
            ctx,
            reservation_id=rid,
            previous_check_in=previous_check_in,
            previous_check_out=previous_check_out,
            port=port,
            rules=rules,
            now=_PINNED,
        )
        assert reapply.per_rule[0].occurrences[0].port_outcome == "regenerated"
        assert bundles[0].tasks_json[0]["cancellation_reason"] == "stay rescheduled"
    finally:
        reset_current(token)
