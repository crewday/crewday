"""Unit tests for :mod:`app.domain.identity.user_workspace_refresh` (cd-yqm4).

The reconciler walks every upstream (workspace-scoped role_grants,
property-scoped role_grants resolved through ``property_workspace``,
``work_engagement`` rows; plus the org-grant forward-compat seam) and
brings the derived ``user_workspace`` junction in line.

This suite covers the algorithm at unit scope — small in-memory engine,
manually-seeded rows, no production-flow plumbing. The integration
counterpart in
``tests/integration/worker/test_user_workspace_refresh_fanout.py``
drives the worker fan-out body end-to-end.

Edge cases pinned here:

* Add cycle — a fresh role_grant materialises a row on the next tick.
* Revoke cycle — orphaned rows drop on the next tick.
* Source precedence — ``workspace_grant`` outranks ``work_engagement``.
* Persistence on source flip — when the dominant upstream is revoked
  but a weaker one remains, the row keeps ``added_at`` and flips
  ``source`` to the next-strongest active source.
* Property-scoped grants — joined through ``property_workspace``;
  produces ``source = 'property_grant'``.
* Forward-compat seam — ``org_grant`` returns no rows today.
* Empty state — no upstreams, no rows; reconcile is a no-op.

See ``docs/specs/02-domain-model.md`` §"user_workspace" and
``docs/specs/17-testing-quality.md`` §"Unit".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.workspace.models import (
    UserWorkspace,
    WorkEngagement,
    Workspace,
)
from app.domain.identity.user_workspace_refresh import (
    is_higher_precedence,
    reconcile_user_workspace,
    reconcile_user_workspace_for,
)
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 4, 24, 13, 0, 0, tzinfo=UTC)


def _utc(value: datetime) -> datetime:
    """SQLite's ``DateTime(timezone=True)`` round-trip drops tzinfo;
    pin a comparison helper that re-applies UTC so the assertion still
    means "this is the same instant in time" rather than coupling to
    the storage layer's tz-naivety.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _seed_workspace(session: Session, *, slug: str) -> str:
    """Insert one :class:`Workspace` and return its id."""
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


def _seed_user(session: Session, *, email: str) -> str:
    """Insert one :class:`User` row and return its id."""
    from app.adapters.db.identity.models import User, canonicalise_email

    user_id = new_ulid()
    with tenant_agnostic():
        session.add(
            User(
                id=user_id,
                email=email,
                email_lower=canonicalise_email(email),
                display_name=email.split("@", 1)[0],
                created_at=_PINNED,
            )
        )
        session.flush()
    return user_id


def _seed_workspace_grant(session: Session, *, user_id: str, workspace_id: str) -> str:
    """Seed a workspace-scoped :class:`RoleGrant` and return its id."""
    grant_id = new_ulid()
    with tenant_agnostic():
        session.add(
            RoleGrant(
                id=grant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                grant_role="manager",
                scope_kind="workspace",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        session.flush()
    return grant_id


def _seed_property_grant(
    session: Session,
    *,
    user_id: str,
    workspace_id: str,
) -> tuple[str, str]:
    """Seed a property in ``workspace_id`` + a property-scoped grant.

    Returns ``(grant_id, property_id)``.
    """
    property_id = new_ulid()
    with tenant_agnostic():
        session.add(
            Property(
                id=property_id,
                address=f"property-{property_id}",
                timezone="UTC",
                tags_json=[],
                created_at=_PINNED,
            )
        )
        session.flush()
        session.add(
            PropertyWorkspace(
                property_id=property_id,
                workspace_id=workspace_id,
                label="primary",
                membership_role="owner_workspace",
                created_at=_PINNED,
            )
        )
        session.flush()
        grant_id = new_ulid()
        session.add(
            RoleGrant(
                id=grant_id,
                # Property-scoped grants in v1 carry the workspace_id
                # too; the reconciler joins through property_workspace
                # for the workspace it materialises in. Keep the
                # workspace_id matching so the schema CHECK passes.
                workspace_id=workspace_id,
                user_id=user_id,
                grant_role="worker",
                scope_kind="workspace",
                scope_property_id=property_id,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        session.flush()
    return grant_id, property_id


def _seed_engagement(session: Session, *, user_id: str, workspace_id: str) -> str:
    """Seed a non-archived :class:`WorkEngagement` and return its id."""
    eng_id = new_ulid()
    with tenant_agnostic():
        session.add(
            WorkEngagement(
                id=eng_id,
                user_id=user_id,
                workspace_id=workspace_id,
                engagement_kind="payroll",
                supplier_org_id=None,
                pay_destination_id=None,
                reimbursement_destination_id=None,
                started_on=_PINNED.date(),
                archived_on=None,
                notes_md="",
                created_at=_PINNED,
                updated_at=_PINNED,
            )
        )
        session.flush()
    return eng_id


def _user_workspace(
    session: Session, *, user_id: str, workspace_id: str
) -> UserWorkspace | None:
    with tenant_agnostic():
        return session.get(UserWorkspace, (user_id, workspace_id))


# ---------------------------------------------------------------------------
# is_higher_precedence helper
# ---------------------------------------------------------------------------


class TestPrecedenceHelper:
    """Pin the source precedence at the public surface."""

    def test_workspace_grant_outranks_property_grant(self) -> None:
        assert is_higher_precedence("workspace_grant", "property_grant") is True

    def test_property_grant_outranks_org_grant(self) -> None:
        assert is_higher_precedence("property_grant", "org_grant") is True

    def test_org_grant_outranks_work_engagement(self) -> None:
        assert is_higher_precedence("org_grant", "work_engagement") is True

    def test_workspace_grant_outranks_work_engagement(self) -> None:
        """Transitive precedence stays consistent."""
        assert is_higher_precedence("workspace_grant", "work_engagement") is True

    def test_same_source_is_not_higher(self) -> None:
        """A source is not strictly higher than itself."""
        assert is_higher_precedence("workspace_grant", "workspace_grant") is False

    def test_unknown_source_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown source"):
            is_higher_precedence("not_a_source", "workspace_grant")
        with pytest.raises(ValueError, match="unknown source"):
            is_higher_precedence("workspace_grant", "not_a_source")


# ---------------------------------------------------------------------------
# Add / revoke cycles
# ---------------------------------------------------------------------------


class TestAddCycle:
    """A fresh upstream materialises a row on the next reconcile."""

    def test_workspace_grant_creates_user_workspace_row(self, session: Session) -> None:
        ws_id = _seed_workspace(session, slug="ws-add")
        user_id = _seed_user(session, email="add@example.com")
        _seed_workspace_grant(session, user_id=user_id, workspace_id=ws_id)

        report = reconcile_user_workspace(session, now=_PINNED)

        row = _user_workspace(session, user_id=user_id, workspace_id=ws_id)
        assert row is not None
        assert row.source == "workspace_grant"
        assert _utc(row.added_at) == _PINNED
        assert report.rows_inserted == 1
        assert report.rows_deleted == 0
        assert report.rows_source_flipped == 0
        assert report.upstream_pairs_seen == 1

    def test_engagement_alone_creates_user_workspace_row(
        self, session: Session
    ) -> None:
        ws_id = _seed_workspace(session, slug="ws-eng")
        user_id = _seed_user(session, email="eng@example.com")
        _seed_engagement(session, user_id=user_id, workspace_id=ws_id)

        reconcile_user_workspace(session, now=_PINNED)

        row = _user_workspace(session, user_id=user_id, workspace_id=ws_id)
        assert row is not None
        assert row.source == "work_engagement"

    def test_property_grant_creates_user_workspace_row_with_property_grant_source(
        self, session: Session
    ) -> None:
        ws_id = _seed_workspace(session, slug="ws-prop")
        user_id = _seed_user(session, email="prop@example.com")
        _seed_property_grant(session, user_id=user_id, workspace_id=ws_id)

        reconcile_user_workspace(session, now=_PINNED)

        row = _user_workspace(session, user_id=user_id, workspace_id=ws_id)
        assert row is not None
        assert row.source == "property_grant"

    def test_second_reconcile_is_idempotent(self, session: Session) -> None:
        """Running the reconciler twice with the same upstreams is a no-op."""
        ws_id = _seed_workspace(session, slug="ws-idem")
        user_id = _seed_user(session, email="idem@example.com")
        _seed_workspace_grant(session, user_id=user_id, workspace_id=ws_id)

        reconcile_user_workspace(session, now=_PINNED)
        report = reconcile_user_workspace(session, now=_LATER)

        assert report.rows_inserted == 0
        assert report.rows_deleted == 0
        assert report.rows_source_flipped == 0
        # ``added_at`` did NOT roll forward — the row was already present.
        row = _user_workspace(session, user_id=user_id, workspace_id=ws_id)
        assert row is not None
        assert _utc(row.added_at) == _PINNED


class TestRevokeCycle:
    """Orphaned rows drop on the next reconcile."""

    def test_revoking_only_grant_drops_user_workspace_row(
        self, session: Session
    ) -> None:
        ws_id = _seed_workspace(session, slug="ws-revoke")
        user_id = _seed_user(session, email="revoke@example.com")
        grant_id = _seed_workspace_grant(session, user_id=user_id, workspace_id=ws_id)

        reconcile_user_workspace(session, now=_PINNED)
        assert _user_workspace(session, user_id=user_id, workspace_id=ws_id) is not None

        # Revoke the grant.
        with tenant_agnostic():
            session.execute(delete(RoleGrant).where(RoleGrant.id == grant_id))
            session.flush()

        report = reconcile_user_workspace(session, now=_LATER)

        assert _user_workspace(session, user_id=user_id, workspace_id=ws_id) is None
        assert report.rows_deleted == 1
        assert report.rows_inserted == 0

    def test_archiving_engagement_drops_user_workspace_row(
        self, session: Session
    ) -> None:
        ws_id = _seed_workspace(session, slug="ws-arch")
        user_id = _seed_user(session, email="arch@example.com")
        eng_id = _seed_engagement(session, user_id=user_id, workspace_id=ws_id)

        reconcile_user_workspace(session, now=_PINNED)
        assert _user_workspace(session, user_id=user_id, workspace_id=ws_id) is not None

        # Archive the engagement.
        with tenant_agnostic():
            eng = session.get(WorkEngagement, eng_id)
            assert eng is not None
            eng.archived_on = _LATER.date()
            session.flush()

        reconcile_user_workspace(session, now=_LATER)
        assert _user_workspace(session, user_id=user_id, workspace_id=ws_id) is None


# ---------------------------------------------------------------------------
# Source precedence + flip
# ---------------------------------------------------------------------------


class TestSourcePrecedence:
    """The strongest live upstream wins, and added_at survives a flip."""

    def test_workspace_grant_beats_engagement(self, session: Session) -> None:
        ws_id = _seed_workspace(session, slug="ws-precedence")
        user_id = _seed_user(session, email="precedence@example.com")
        # Both upstreams live — the grant outranks the engagement.
        _seed_workspace_grant(session, user_id=user_id, workspace_id=ws_id)
        _seed_engagement(session, user_id=user_id, workspace_id=ws_id)

        reconcile_user_workspace(session, now=_PINNED)

        row = _user_workspace(session, user_id=user_id, workspace_id=ws_id)
        assert row is not None
        assert row.source == "workspace_grant"

    def test_revoking_grant_flips_source_and_keeps_added_at(
        self, session: Session
    ) -> None:
        """Cd-yqm4 acceptance: persistence-on-source-change.

        Row exists with source=workspace_grant; the grant is revoked
        but a work_engagement still keeps the user materialised in the
        workspace. The row should persist with source=work_engagement
        and the original added_at preserved.
        """
        ws_id = _seed_workspace(session, slug="ws-flip")
        user_id = _seed_user(session, email="flip@example.com")
        grant_id = _seed_workspace_grant(session, user_id=user_id, workspace_id=ws_id)
        _seed_engagement(session, user_id=user_id, workspace_id=ws_id)

        reconcile_user_workspace(session, now=_PINNED)
        first = _user_workspace(session, user_id=user_id, workspace_id=ws_id)
        assert first is not None
        assert first.source == "workspace_grant"
        assert _utc(first.added_at) == _PINNED

        # Revoke the dominant upstream.
        with tenant_agnostic():
            session.execute(delete(RoleGrant).where(RoleGrant.id == grant_id))
            session.flush()

        report = reconcile_user_workspace(session, now=_LATER)

        second = _user_workspace(session, user_id=user_id, workspace_id=ws_id)
        assert second is not None
        assert second.source == "work_engagement"
        # added_at is preserved across the source flip.
        assert _utc(second.added_at) == _PINNED
        assert report.rows_source_flipped == 1
        assert report.rows_inserted == 0
        assert report.rows_deleted == 0


# ---------------------------------------------------------------------------
# Forward-compat seams + empty state
# ---------------------------------------------------------------------------


class TestEmptyState:
    """No upstreams, no rows; reconcile is a no-op."""

    def test_no_upstreams_no_rows(self, session: Session) -> None:
        report = reconcile_user_workspace(session, now=_PINNED)
        assert report.rows_inserted == 0
        assert report.rows_deleted == 0
        assert report.rows_source_flipped == 0
        assert report.upstream_pairs_seen == 0


class TestForwardCompatSeams:
    """Org-grant seam stays a no-op until the org_workspace table lands."""

    def test_org_grant_returns_empty_today(self, session: Session) -> None:
        """The seam returns no rows; presence of the org-scoped enum
        value in the source CHECK constraint is forward-compat only.
        """
        from app.domain.identity.user_workspace_refresh import _org_grant_pairs

        assert _org_grant_pairs(session) == []


# ---------------------------------------------------------------------------
# Multi-workspace + multi-user shape
# ---------------------------------------------------------------------------


class TestScopedReconcile:
    """``reconcile_user_workspace_for`` narrows the algorithm to one cell.

    Used by transactional flows (invite-accept, member removal) that
    cannot tolerate the worker tick's eventual-consistency lag — the
    helper applies the same source-precedence + ``added_at``-survives
    rules as the global reconciler, but only touches one
    ``(user_id, workspace_id)`` pair.
    """

    def test_inserts_when_upstream_present_and_no_row(self, session: Session) -> None:
        ws_id = _seed_workspace(session, slug="ws-scoped-add")
        user_id = _seed_user(session, email="scoped-add@example.com")
        _seed_workspace_grant(session, user_id=user_id, workspace_id=ws_id)

        report = reconcile_user_workspace_for(
            session, user_id=user_id, workspace_id=ws_id, now=_PINNED
        )

        row = _user_workspace(session, user_id=user_id, workspace_id=ws_id)
        assert row is not None
        assert row.source == "workspace_grant"
        assert _utc(row.added_at) == _PINNED
        assert report.rows_inserted == 1
        assert report.upstream_pairs_seen == 1

    def test_deletes_when_no_upstream_and_row_exists(self, session: Session) -> None:
        ws_id = _seed_workspace(session, slug="ws-scoped-del")
        user_id = _seed_user(session, email="scoped-del@example.com")
        grant_id = _seed_workspace_grant(session, user_id=user_id, workspace_id=ws_id)
        reconcile_user_workspace(session, now=_PINNED)
        assert _user_workspace(session, user_id=user_id, workspace_id=ws_id) is not None

        with tenant_agnostic():
            session.execute(delete(RoleGrant).where(RoleGrant.id == grant_id))
            session.flush()

        report = reconcile_user_workspace_for(
            session, user_id=user_id, workspace_id=ws_id, now=_LATER
        )
        assert _user_workspace(session, user_id=user_id, workspace_id=ws_id) is None
        assert report.rows_deleted == 1
        assert report.upstream_pairs_seen == 0

    def test_flips_source_and_keeps_added_at(self, session: Session) -> None:
        """Same persistence-on-source-flip semantics as the global pass."""
        ws_id = _seed_workspace(session, slug="ws-scoped-flip")
        user_id = _seed_user(session, email="scoped-flip@example.com")
        grant_id = _seed_workspace_grant(session, user_id=user_id, workspace_id=ws_id)
        _seed_engagement(session, user_id=user_id, workspace_id=ws_id)
        reconcile_user_workspace_for(
            session, user_id=user_id, workspace_id=ws_id, now=_PINNED
        )

        with tenant_agnostic():
            session.execute(delete(RoleGrant).where(RoleGrant.id == grant_id))
            session.flush()

        report = reconcile_user_workspace_for(
            session, user_id=user_id, workspace_id=ws_id, now=_LATER
        )
        row = _user_workspace(session, user_id=user_id, workspace_id=ws_id)
        assert row is not None
        assert row.source == "work_engagement"
        assert _utc(row.added_at) == _PINNED
        assert report.rows_source_flipped == 1
        assert report.rows_inserted == 0
        assert report.rows_deleted == 0

    def test_no_op_when_already_in_desired_state(self, session: Session) -> None:
        ws_id = _seed_workspace(session, slug="ws-scoped-noop")
        user_id = _seed_user(session, email="scoped-noop@example.com")
        _seed_workspace_grant(session, user_id=user_id, workspace_id=ws_id)
        reconcile_user_workspace_for(
            session, user_id=user_id, workspace_id=ws_id, now=_PINNED
        )

        report = reconcile_user_workspace_for(
            session, user_id=user_id, workspace_id=ws_id, now=_LATER
        )
        assert report.rows_inserted == 0
        assert report.rows_deleted == 0
        assert report.rows_source_flipped == 0

    def test_property_grant_via_scoped_path(self, session: Session) -> None:
        """Property-scoped grants resolve through ``property_workspace``."""
        ws_id = _seed_workspace(session, slug="ws-scoped-prop")
        user_id = _seed_user(session, email="scoped-prop@example.com")
        _seed_property_grant(session, user_id=user_id, workspace_id=ws_id)

        reconcile_user_workspace_for(
            session, user_id=user_id, workspace_id=ws_id, now=_PINNED
        )
        row = _user_workspace(session, user_id=user_id, workspace_id=ws_id)
        assert row is not None
        assert row.source == "property_grant"

    def test_does_not_touch_unrelated_cells(self, session: Session) -> None:
        """Scoped reconcile only acts on the (user_id, workspace_id) pair."""
        ws_a = _seed_workspace(session, slug="ws-scoped-other-a")
        ws_b = _seed_workspace(session, slug="ws-scoped-other-b")
        user_id = _seed_user(session, email="scoped-other@example.com")
        # User has a live grant in ws_a — the global reconcile would
        # have created the row, but the scoped reconcile aimed at ws_b
        # must NOT insert ws_a as a side effect.
        _seed_workspace_grant(session, user_id=user_id, workspace_id=ws_a)

        report = reconcile_user_workspace_for(
            session, user_id=user_id, workspace_id=ws_b, now=_PINNED
        )
        assert report.rows_inserted == 0
        assert report.upstream_pairs_seen == 0
        assert _user_workspace(session, user_id=user_id, workspace_id=ws_a) is None
        assert _user_workspace(session, user_id=user_id, workspace_id=ws_b) is None


class TestMultiTenantShape:
    """A single reconciler pass walks every workspace at once."""

    def test_independent_workspaces_independent_rows(self, session: Session) -> None:
        ws_a = _seed_workspace(session, slug="ws-a")
        ws_b = _seed_workspace(session, slug="ws-b")
        user_a = _seed_user(session, email="a@example.com")
        user_b = _seed_user(session, email="b@example.com")

        # User A lives in ws-a only; user B lives in ws-b only.
        _seed_workspace_grant(session, user_id=user_a, workspace_id=ws_a)
        _seed_workspace_grant(session, user_id=user_b, workspace_id=ws_b)

        report = reconcile_user_workspace(session, now=_PINNED)

        assert report.rows_inserted == 2
        assert _user_workspace(session, user_id=user_a, workspace_id=ws_a) is not None
        assert _user_workspace(session, user_id=user_a, workspace_id=ws_b) is None
        assert _user_workspace(session, user_id=user_b, workspace_id=ws_b) is not None
        assert _user_workspace(session, user_id=user_b, workspace_id=ws_a) is None

    def test_user_in_two_workspaces_gets_two_rows(self, session: Session) -> None:
        ws_a = _seed_workspace(session, slug="ws-multi-a")
        ws_b = _seed_workspace(session, slug="ws-multi-b")
        user_id = _seed_user(session, email="multi@example.com")

        _seed_workspace_grant(session, user_id=user_id, workspace_id=ws_a)
        _seed_workspace_grant(session, user_id=user_id, workspace_id=ws_b)

        reconcile_user_workspace(session, now=_PINNED)

        assert _user_workspace(session, user_id=user_id, workspace_id=ws_a) is not None
        assert _user_workspace(session, user_id=user_id, workspace_id=ws_b) is not None
