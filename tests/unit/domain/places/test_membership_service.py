"""Unit tests for :mod:`app.domain.places.membership_service` (cd-hsk).

Mirrors the in-memory SQLite bootstrap in
``tests/unit/places/test_property_service.py``: a fresh engine per
test, pull every sibling ``models`` module onto the shared
``Base.metadata``, run ``Base.metadata.create_all``, drive the domain
code with a :class:`FrozenClock`.

Covers the cd-hsk acceptance criteria:

* Owner invites managed → row created with status=invited,
  share_guest_identity=False default.
* Non-owners-group caller → :class:`NotOwnerWorkspaceMember`. Mere
  worker / manager grant on the owner workspace is **not** enough;
  the §22 spec pins the invite-side gate to the ``owners`` group.
* Accept by ``owners`` member of accepting workspace → status flips
  to active. Accept by a worker on the accepting workspace
  (non-``owners``) → :class:`NotWorkspaceMember`.
* Revoke removes non-owner row; revoking the owner row raises
  :class:`CannotRevokeOwner`.
* :func:`update_membership_role` to ``"owner_workspace"`` raises
  :class:`InvalidMembershipRole` (must use :func:`transfer_ownership`).
* :func:`transfer_ownership` demotes old to observer + promotes new
  in one TX; exactly 1 owner row.
* :func:`transfer_ownership` with ``demote_to="revoke"`` deletes the
  old owner row.
* :func:`update_share_guest_identity` defaults False; toggling
  audits. Owner row is immutable on the field (§02).
* :func:`list_memberships` returns all rows for the property — the
  read gate is the broader worker-or-higher reach, not owners-only.

See ``docs/specs/04-properties-and-stays.md`` §"Multi-belonging
(sharing across workspaces)" and ``docs/specs/22-clients-and-vendors.md``
§"property_workspace_invite" (authority pinned to the ``owners``
group on either side of the invite).
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.places.models import PropertyWorkspace
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.places.membership_service import (
    CannotRevokeOwner,
    InvalidMembershipRole,
    MembershipAlreadyExists,
    MembershipNotFound,
    MembershipRead,
    NotOwnerWorkspaceMember,
    NotWorkspaceMember,
    OwnerWorkspaceMissing,
    accept_invite,
    invite_workspace,
    list_memberships,
    revoke_workspace,
    transfer_ownership,
    update_membership_role,
    update_share_guest_identity,
)
from app.domain.places.property_service import (
    PropertyCreate,
    PropertyView,
    create_property,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
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


@pytest.fixture(name="engine_membership")
def fixture_engine() -> Iterator[Engine]:
    """In-memory SQLite engine, schema created from ``Base.metadata``."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(name="session_membership")
def fixture_session(engine_membership: Engine) -> Iterator[Session]:
    """Per-test session — no tenant filter installed.

    The membership service uses :func:`tenant_agnostic` for its
    cross-workspace reads regardless; the unit tests don't need to
    install the filter to exercise the authorization gates because
    the service runs the explicit grant lookup itself.
    """
    factory = sessionmaker(
        bind=engine_membership,
        expire_on_commit=False,
        class_=Session,
    )
    with factory() as s:
        yield s


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _ctx(*, workspace_id: str, slug: str, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


def _make_workspace(session: Session, *, slug: str) -> str:
    """Insert a :class:`Workspace` row + its system ``owners`` group.

    Every workspace in the production schema is bootstrapped with an
    ``owners`` permission group at creation time
    (:func:`app.adapters.db.authz.bootstrap.seed_owners_system_group`).
    The membership service routes every write through
    :func:`app.authz.owners.is_owner_member`, which joins
    ``permission_group_member`` to a ``system=True`` ``slug='owners'``
    row — so the unit fixture must seed the row too, otherwise every
    write path collapses to an authorisation denial regardless of
    the actor.
    """
    workspace_id = new_ulid()
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
    session.add(
        PermissionGroup(
            id=new_ulid(),
            workspace_id=workspace_id,
            slug="owners",
            name="Owners",
            system=True,
            capabilities_json={"all": True},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


def _seed_owner(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
) -> None:
    """Mark ``user_id`` as a member of ``owners@<workspace_id>``.

    The membership-service write paths gate on
    :func:`app.authz.owners.is_owner_member`. Tests that exercise a
    write must seed both the role grant (for the read gate) and the
    owners-group membership (for the write gate).
    """
    owners_group = session.scalars(
        select(PermissionGroup).where(
            PermissionGroup.workspace_id == workspace_id,
            PermissionGroup.slug == "owners",
            PermissionGroup.system.is_(True),
        )
    ).one()
    session.add(
        PermissionGroupMember(
            group_id=owners_group.id,
            user_id=user_id,
            workspace_id=workspace_id,
            added_at=_PINNED,
            added_by_user_id=None,
        )
    )
    session.flush()


def _make_user(session: Session, *, label: str) -> str:
    """Insert a :class:`User` row keyed off ``label`` and return its id."""
    user_id = new_ulid()
    email = f"{label}-{user_id[:6].lower()}@example.test"
    session.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=label,
            locale=None,
            timezone=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    return user_id


def _grant(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
    grant_role: str = "manager",
    owners: bool = True,
) -> None:
    """Seed a workspace-scope :class:`RoleGrant` for ``user_id``.

    By default the helper also enrols the user in
    ``owners@<workspace_id>`` so the actor satisfies both the
    write-side gate (``owners`` group, used by every mutation) and
    the read-side gate (worker-or-higher role grant, used by
    :func:`list_memberships`). Set ``owners=False`` to seed only the
    role grant — useful when the test wants to exercise the
    "manager but not in owners group" denial path.
    """
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_kind="workspace",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    session.flush()
    if owners:
        _seed_owner(session, workspace_id=workspace_id, user_id=user_id)


def _create_property(
    session: Session,
    *,
    owner_ws_id: str,
    owner_ws_slug: str,
    actor_id: str,
    name: str = "Villa Sud",
    clock: FrozenClock,
) -> PropertyView:
    """Bootstrap a property + owner_workspace junction in one flush."""
    body = PropertyCreate.model_validate(
        {
            "name": name,
            "kind": "vacation",
            "address": "12 Chemin des Oliviers, Antibes",
            "address_json": {
                "line1": "12 Chemin des Oliviers",
                "city": "Antibes",
                "country": "FR",
            },
            "country": "FR",
            "timezone": "Europe/Paris",
        }
    )
    return create_property(
        session,
        _ctx(workspace_id=owner_ws_id, slug=owner_ws_slug, actor_id=actor_id),
        body=body,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInvite:
    """``invite_workspace`` — owner-workspace authorisation, row shape."""

    def test_owner_invites_managed_creates_invited_row(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        """Owner invites managed → status='invited', defaults False."""
        owner_ws = _make_workspace(session_membership, slug="owner-a")
        target_ws = _make_workspace(session_membership, slug="agency-b")
        actor_id = _make_user(session_membership, label="actor")
        _grant(
            session_membership,
            workspace_id=owner_ws,
            user_id=actor_id,
        )
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="owner-a",
            actor_id=actor_id,
            clock=frozen_clock,
        )

        view = invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="owner-a", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )

        assert isinstance(view, MembershipRead)
        assert view.property_id == prop.id
        assert view.workspace_id == target_ws
        assert view.membership_role == "managed_workspace"
        assert view.status == "invited"
        assert view.share_guest_identity is False
        # The label is inherited from the owner row so the recipient
        # gets a sensible default.
        assert view.label == prop.name

    def test_invite_writes_audit(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="audit-owner")
        target_ws = _make_workspace(session_membership, slug="audit-target")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="audit-owner",
            actor_id=actor_id,
            clock=frozen_clock,
        )

        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="audit-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="observer_workspace",
            clock=frozen_clock,
        )

        audits = session_membership.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == prop.id,
                AuditLog.action == "invited",
            )
        ).all()
        assert len(audits) == 1
        diff = audits[0].diff
        assert diff["after"]["workspace_id"] == target_ws
        assert diff["after"]["membership_role"] == "observer_workspace"
        assert diff["after"]["status"] == "invited"

    def test_non_owner_invite_attempt_raises(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        """An actor with no grant on the owner workspace is denied."""
        owner_ws = _make_workspace(session_membership, slug="auth-owner")
        target_ws = _make_workspace(session_membership, slug="auth-target")
        owner_actor = _make_user(session_membership, label="owner-actor")
        impostor_actor = _make_user(session_membership, label="impostor")
        _grant(session_membership, workspace_id=owner_ws, user_id=owner_actor)
        # impostor_actor has NO grant on owner_ws — they are a member
        # of target_ws instead, but that's the wrong side of the call.
        _grant(session_membership, workspace_id=target_ws, user_id=impostor_actor)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="auth-owner",
            actor_id=owner_actor,
            clock=frozen_clock,
        )

        with pytest.raises(NotOwnerWorkspaceMember):
            invite_workspace(
                session_membership,
                _ctx(
                    workspace_id=owner_ws,
                    slug="auth-owner",
                    actor_id=impostor_actor,
                ),
                property_id=prop.id,
                target_workspace_id=target_ws,
                role="managed_workspace",
                clock=frozen_clock,
            )

    def test_worker_on_owner_workspace_denied(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        """A worker on the owner workspace (not in ``owners``) is denied invite.

        §22 pins ``property_workspace_invite.create`` to ``owners``
        membership; a worker / manager grant on the owner workspace
        is not enough.
        """
        owner_ws = _make_workspace(session_membership, slug="wkr-owner")
        target_ws = _make_workspace(session_membership, slug="wkr-target")
        owner_actor = _make_user(session_membership, label="owner-actor")
        worker_actor = _make_user(session_membership, label="worker-actor")
        # owner-actor is a real owners-group member, so the bootstrap
        # property creation succeeds.
        _grant(session_membership, workspace_id=owner_ws, user_id=owner_actor)
        # worker-actor holds a worker grant on the owner workspace but
        # is NOT enrolled in owners@owner_ws — the gate must reject.
        _grant(
            session_membership,
            workspace_id=owner_ws,
            user_id=worker_actor,
            grant_role="worker",
            owners=False,
        )
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="wkr-owner",
            actor_id=owner_actor,
            clock=frozen_clock,
        )

        with pytest.raises(NotOwnerWorkspaceMember):
            invite_workspace(
                session_membership,
                _ctx(
                    workspace_id=owner_ws,
                    slug="wkr-owner",
                    actor_id=worker_actor,
                ),
                property_id=prop.id,
                target_workspace_id=target_ws,
                role="managed_workspace",
                clock=frozen_clock,
            )

    def test_invite_to_owner_role_rejected(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        """``invite_workspace`` cannot mint ``owner_workspace``."""
        owner_ws = _make_workspace(session_membership, slug="owner-role-owner")
        target_ws = _make_workspace(session_membership, slug="owner-role-target")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="owner-role-owner",
            actor_id=actor_id,
            clock=frozen_clock,
        )

        with pytest.raises(InvalidMembershipRole):
            invite_workspace(
                session_membership,
                _ctx(workspace_id=owner_ws, slug="owner-role-owner", actor_id=actor_id),
                property_id=prop.id,
                target_workspace_id=target_ws,
                role="owner_workspace",  # type: ignore[arg-type]
                clock=frozen_clock,
            )

    def test_invite_duplicate_workspace_rejected(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        """A second invite to the same workspace is a 409-style error."""
        owner_ws = _make_workspace(session_membership, slug="dup-owner")
        target_ws = _make_workspace(session_membership, slug="dup-target")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="dup-owner",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="dup-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )

        with pytest.raises(MembershipAlreadyExists):
            invite_workspace(
                session_membership,
                _ctx(workspace_id=owner_ws, slug="dup-owner", actor_id=actor_id),
                property_id=prop.id,
                target_workspace_id=target_ws,
                role="observer_workspace",
                clock=frozen_clock,
            )

    def test_invite_unknown_property_raises(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="unk-owner")
        target_ws = _make_workspace(session_membership, slug="unk-target")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)

        with pytest.raises(OwnerWorkspaceMissing):
            invite_workspace(
                session_membership,
                _ctx(workspace_id=owner_ws, slug="unk-owner", actor_id=actor_id),
                property_id="01HWA00000000000000000PRPZ",
                target_workspace_id=target_ws,
                role="managed_workspace",
                clock=frozen_clock,
            )


class TestAccept:
    """``accept_invite`` flips ``invited`` → ``active`` for the recipient."""

    def test_accept_by_member_flips_status(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="acc-owner")
        target_ws = _make_workspace(session_membership, slug="acc-target")
        owner_actor = _make_user(session_membership, label="owner-actor")
        target_actor = _make_user(session_membership, label="target-actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=owner_actor)
        _grant(session_membership, workspace_id=target_ws, user_id=target_actor)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="acc-owner",
            actor_id=owner_actor,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="acc-owner", actor_id=owner_actor),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )

        view = accept_invite(
            session_membership,
            _ctx(workspace_id=target_ws, slug="acc-target", actor_id=target_actor),
            property_id=prop.id,
            accepting_workspace_id=target_ws,
            clock=frozen_clock,
        )
        assert view.status == "active"
        assert view.membership_role == "managed_workspace"

        # Audit row recorded.
        audits = session_membership.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == prop.id,
                AuditLog.action == "accepted",
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].diff["before"]["status"] == "invited"
        assert audits[0].diff["after"]["status"] == "active"

    def test_accept_by_non_member_denied(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="acc-deny-owner")
        target_ws = _make_workspace(session_membership, slug="acc-deny-target")
        owner_actor = _make_user(session_membership, label="owner-actor")
        outsider = _make_user(session_membership, label="outsider")
        _grant(session_membership, workspace_id=owner_ws, user_id=owner_actor)
        # outsider has NO grant on target_ws.
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="acc-deny-owner",
            actor_id=owner_actor,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="acc-deny-owner", actor_id=owner_actor),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )

        with pytest.raises(NotWorkspaceMember):
            accept_invite(
                session_membership,
                _ctx(
                    workspace_id=target_ws,
                    slug="acc-deny-target",
                    actor_id=outsider,
                ),
                property_id=prop.id,
                accepting_workspace_id=target_ws,
                clock=frozen_clock,
            )

    def test_accept_by_worker_on_accepting_ws_denied(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        """A worker grant on the accepting workspace is not enough.

        §22 ``property_workspace_invite.accept`` requires ``owners``
        membership on the accepting workspace; a manager / worker
        grant is insufficient.
        """
        owner_ws = _make_workspace(session_membership, slug="acc-wkr-owner")
        target_ws = _make_workspace(session_membership, slug="acc-wkr-target")
        owner_actor = _make_user(session_membership, label="owner-actor")
        worker_actor = _make_user(session_membership, label="worker-actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=owner_actor)
        # worker_actor holds a worker grant on target_ws but is NOT
        # enrolled in owners@target_ws.
        _grant(
            session_membership,
            workspace_id=target_ws,
            user_id=worker_actor,
            grant_role="worker",
            owners=False,
        )
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="acc-wkr-owner",
            actor_id=owner_actor,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="acc-wkr-owner", actor_id=owner_actor),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )

        with pytest.raises(NotWorkspaceMember):
            accept_invite(
                session_membership,
                _ctx(
                    workspace_id=target_ws,
                    slug="acc-wkr-target",
                    actor_id=worker_actor,
                ),
                property_id=prop.id,
                accepting_workspace_id=target_ws,
                clock=frozen_clock,
            )

    def test_accept_idempotent_on_active_row(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="idem-owner")
        target_ws = _make_workspace(session_membership, slug="idem-target")
        owner_actor = _make_user(session_membership, label="owner-actor")
        target_actor = _make_user(session_membership, label="target-actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=owner_actor)
        _grant(session_membership, workspace_id=target_ws, user_id=target_actor)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="idem-owner",
            actor_id=owner_actor,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="idem-owner", actor_id=owner_actor),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )
        accept_invite(
            session_membership,
            _ctx(workspace_id=target_ws, slug="idem-target", actor_id=target_actor),
            property_id=prop.id,
            accepting_workspace_id=target_ws,
            clock=frozen_clock,
        )

        # Second accept — should silently succeed and NOT write a
        # second audit row.
        view = accept_invite(
            session_membership,
            _ctx(workspace_id=target_ws, slug="idem-target", actor_id=target_actor),
            property_id=prop.id,
            accepting_workspace_id=target_ws,
            clock=frozen_clock,
        )
        assert view.status == "active"

        audits = session_membership.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == prop.id,
                AuditLog.action == "accepted",
            )
        ).all()
        assert len(audits) == 1

    def test_accept_unknown_target_raises(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="unk-acc-owner")
        target_ws = _make_workspace(session_membership, slug="unk-acc-target")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=target_ws, user_id=actor_id)
        # owner ws actor used only to bootstrap the property
        owner_actor = _make_user(session_membership, label="owner-actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=owner_actor)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="unk-acc-owner",
            actor_id=owner_actor,
            clock=frozen_clock,
        )
        # No invite was issued — accepting raises MembershipNotFound.
        with pytest.raises(MembershipNotFound):
            accept_invite(
                session_membership,
                _ctx(
                    workspace_id=target_ws,
                    slug="unk-acc-target",
                    actor_id=actor_id,
                ),
                property_id=prop.id,
                accepting_workspace_id=target_ws,
                clock=frozen_clock,
            )

    def test_accept_owner_row_rejected(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        """Accepting one's own owner row is meaningless — surfaces as not-found."""
        owner_ws = _make_workspace(session_membership, slug="acc-owner-row")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="acc-owner-row",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        with pytest.raises(MembershipNotFound):
            accept_invite(
                session_membership,
                _ctx(workspace_id=owner_ws, slug="acc-owner-row", actor_id=actor_id),
                property_id=prop.id,
                accepting_workspace_id=owner_ws,
                clock=frozen_clock,
            )


class TestRevoke:
    """``revoke_workspace`` removes a non-owner row; refuses the owner row."""

    def test_revoke_removes_non_owner_row(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="rv-owner")
        target_ws = _make_workspace(session_membership, slug="rv-target")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="rv-owner",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="rv-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )

        revoke_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="rv-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=target_ws,
            clock=frozen_clock,
        )

        # The junction row is gone; only the owner row remains.
        rows = session_membership.scalars(
            select(PropertyWorkspace).where(
                PropertyWorkspace.property_id == prop.id,
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].workspace_id == owner_ws

        # Audit row.
        audits = session_membership.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == prop.id,
                AuditLog.action == "revoked",
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].diff["before"]["workspace_id"] == target_ws

    def test_revoke_owner_row_raises(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="rv-owner-self")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="rv-owner-self",
            actor_id=actor_id,
            clock=frozen_clock,
        )

        with pytest.raises(CannotRevokeOwner):
            revoke_workspace(
                session_membership,
                _ctx(workspace_id=owner_ws, slug="rv-owner-self", actor_id=actor_id),
                property_id=prop.id,
                target_workspace_id=owner_ws,
                clock=frozen_clock,
            )

    def test_revoke_unknown_target_raises(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="rv-unk-owner")
        ghost_ws = _make_workspace(session_membership, slug="rv-unk-ghost")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="rv-unk-owner",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        with pytest.raises(MembershipNotFound):
            revoke_workspace(
                session_membership,
                _ctx(workspace_id=owner_ws, slug="rv-unk-owner", actor_id=actor_id),
                property_id=prop.id,
                target_workspace_id=ghost_ws,
                clock=frozen_clock,
            )


class TestUpdateMembershipRole:
    """``update_membership_role`` flips between managed and observer."""

    def test_owner_role_promotion_rejected(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        """Promoting to ``owner_workspace`` requires :func:`transfer_ownership`."""
        owner_ws = _make_workspace(session_membership, slug="ur-promote-owner")
        target_ws = _make_workspace(session_membership, slug="ur-promote-target")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="ur-promote-owner",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="ur-promote-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )

        with pytest.raises(InvalidMembershipRole):
            update_membership_role(
                session_membership,
                _ctx(workspace_id=owner_ws, slug="ur-promote-owner", actor_id=actor_id),
                property_id=prop.id,
                target_workspace_id=target_ws,
                role="owner_workspace",  # type: ignore[arg-type]
                clock=frozen_clock,
            )

    def test_role_changed_managed_to_observer(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="ur-flip-owner")
        target_ws = _make_workspace(session_membership, slug="ur-flip-target")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="ur-flip-owner",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="ur-flip-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )

        view = update_membership_role(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="ur-flip-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="observer_workspace",
            clock=frozen_clock,
        )
        assert view.membership_role == "observer_workspace"

        audits = session_membership.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == prop.id,
                AuditLog.action == "role_changed",
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].diff["before"]["membership_role"] == "managed_workspace"
        assert audits[0].diff["after"]["membership_role"] == "observer_workspace"

    def test_owner_row_role_change_rejected(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="ur-owner-self")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="ur-owner-self",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        with pytest.raises(CannotRevokeOwner):
            update_membership_role(
                session_membership,
                _ctx(workspace_id=owner_ws, slug="ur-owner-self", actor_id=actor_id),
                property_id=prop.id,
                target_workspace_id=owner_ws,
                role="observer_workspace",
                clock=frozen_clock,
            )

    def test_role_change_idempotent_no_audit(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="ur-noop-owner")
        target_ws = _make_workspace(session_membership, slug="ur-noop-target")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="ur-noop-owner",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="ur-noop-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )

        update_membership_role(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="ur-noop-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="managed_workspace",  # same value
            clock=frozen_clock,
        )

        audits = session_membership.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == prop.id,
                AuditLog.action == "role_changed",
            )
        ).all()
        assert len(audits) == 0


class TestTransferOwnership:
    """``transfer_ownership`` re-points the owner row in one TX."""

    def test_transfer_demotes_old_to_observer(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="tr-old-owner")
        new_owner_ws = _make_workspace(session_membership, slug="tr-new-owner")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="tr-old-owner",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="tr-old-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=new_owner_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )

        view = transfer_ownership(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="tr-old-owner", actor_id=actor_id),
            property_id=prop.id,
            new_owner_workspace_id=new_owner_ws,
            demote_to="observer",
            clock=frozen_clock,
        )
        assert view.workspace_id == new_owner_ws
        assert view.membership_role == "owner_workspace"
        assert view.status == "active"

        rows = session_membership.scalars(
            select(PropertyWorkspace).where(
                PropertyWorkspace.property_id == prop.id,
            )
        ).all()
        # Exactly one owner row, and it points at the new workspace.
        owners = [r for r in rows if r.membership_role == "owner_workspace"]
        assert len(owners) == 1
        assert owners[0].workspace_id == new_owner_ws

        # Outgoing demoted to observer.
        observers = [r for r in rows if r.membership_role == "observer_workspace"]
        assert len(observers) == 1
        assert observers[0].workspace_id == owner_ws

        audits = session_membership.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == prop.id,
                AuditLog.action == "ownership_transferred",
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].diff["after"]["incoming_owner"]["workspace_id"] == new_owner_ws
        assert audits[0].diff["after"]["demote_to"] == "observer"

    def test_transfer_revoke_deletes_old(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="tr-rv-old")
        new_owner_ws = _make_workspace(session_membership, slug="tr-rv-new")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="tr-rv-old",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="tr-rv-old", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=new_owner_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )

        transfer_ownership(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="tr-rv-old", actor_id=actor_id),
            property_id=prop.id,
            new_owner_workspace_id=new_owner_ws,
            demote_to="revoke",
            clock=frozen_clock,
        )

        rows = session_membership.scalars(
            select(PropertyWorkspace).where(
                PropertyWorkspace.property_id == prop.id,
            )
        ).all()
        # Only the new owner row remains; the old owner's row was
        # hard-deleted.
        assert len(rows) == 1
        assert rows[0].workspace_id == new_owner_ws
        assert rows[0].membership_role == "owner_workspace"

        audits = session_membership.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == prop.id,
                AuditLog.action == "ownership_transferred",
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].diff["after"]["outgoing_owner"] is None
        assert audits[0].diff["after"]["demote_to"] == "revoke"

    def test_transfer_to_unknown_workspace_raises(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        """Recipient must already be a sibling on the property."""
        owner_ws = _make_workspace(session_membership, slug="tr-unk-owner")
        ghost_ws = _make_workspace(session_membership, slug="tr-unk-ghost")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="tr-unk-owner",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        with pytest.raises(MembershipNotFound):
            transfer_ownership(
                session_membership,
                _ctx(workspace_id=owner_ws, slug="tr-unk-owner", actor_id=actor_id),
                property_id=prop.id,
                new_owner_workspace_id=ghost_ws,
                demote_to="observer",
                clock=frozen_clock,
            )

    def test_transfer_self_noop(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        """``new_owner == current_owner`` is a silent no-op."""
        owner_ws = _make_workspace(session_membership, slug="tr-self")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="tr-self",
            actor_id=actor_id,
            clock=frozen_clock,
        )

        view = transfer_ownership(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="tr-self", actor_id=actor_id),
            property_id=prop.id,
            new_owner_workspace_id=owner_ws,
            demote_to="observer",
            clock=frozen_clock,
        )
        assert view.workspace_id == owner_ws
        assert view.membership_role == "owner_workspace"

        # No audit row written.
        audits = session_membership.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == prop.id,
                AuditLog.action == "ownership_transferred",
            )
        ).all()
        assert len(audits) == 0


class TestShareGuestIdentity:
    """``update_share_guest_identity`` toggles the §15 PII flag."""

    def test_default_false_after_invite(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="sg-owner")
        target_ws = _make_workspace(session_membership, slug="sg-target")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="sg-owner",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        view = invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="sg-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )
        assert view.share_guest_identity is False

    def test_toggle_audits(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="sg-toggle-owner")
        target_ws = _make_workspace(session_membership, slug="sg-toggle-target")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="sg-toggle-owner",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="sg-toggle-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )

        view = update_share_guest_identity(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="sg-toggle-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=target_ws,
            share_guest_identity=True,
            clock=frozen_clock,
        )
        assert view.share_guest_identity is True

        audits = session_membership.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == prop.id,
                AuditLog.action == "share_changed",
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].diff["before"]["share_guest_identity"] is False
        assert audits[0].diff["after"]["share_guest_identity"] is True

    def test_toggle_no_op_silent(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="sg-noop-owner")
        target_ws = _make_workspace(session_membership, slug="sg-noop-target")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="sg-noop-owner",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="sg-noop-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=target_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )
        update_share_guest_identity(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="sg-noop-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=target_ws,
            share_guest_identity=False,  # already False
            clock=frozen_clock,
        )
        audits = session_membership.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == prop.id,
                AuditLog.action == "share_changed",
            )
        ).all()
        assert len(audits) == 0

    def test_owner_row_share_change_rejected(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="sg-owner-self")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="sg-owner-self",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        with pytest.raises(CannotRevokeOwner):
            update_share_guest_identity(
                session_membership,
                _ctx(workspace_id=owner_ws, slug="sg-owner-self", actor_id=actor_id),
                property_id=prop.id,
                target_workspace_id=owner_ws,
                share_guest_identity=True,
                clock=frozen_clock,
            )


class TestListMemberships:
    """``list_memberships`` returns every row scoped to caller's reach."""

    def test_owner_lists_all_rows(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="ls-owner")
        managed_ws = _make_workspace(session_membership, slug="ls-managed")
        observer_ws = _make_workspace(session_membership, slug="ls-observer")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="ls-owner",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="ls-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=managed_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="ls-owner", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=observer_ws,
            role="observer_workspace",
            clock=frozen_clock,
        )

        result = list_memberships(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="ls-owner", actor_id=actor_id),
            property_id=prop.id,
        )
        assert len(result) == 3
        roles = {r.workspace_id: r.membership_role for r in result}
        assert roles[owner_ws] == "owner_workspace"
        assert roles[managed_ws] == "managed_workspace"
        assert roles[observer_ws] == "observer_workspace"

    def test_outsider_collapses_to_not_found(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        """A user with no grant on any of the rows' workspaces gets 404."""
        owner_ws = _make_workspace(session_membership, slug="ls-out-owner")
        unrelated_ws = _make_workspace(session_membership, slug="ls-out-other")
        owner_actor = _make_user(session_membership, label="owner-actor")
        outsider = _make_user(session_membership, label="outsider")
        _grant(session_membership, workspace_id=owner_ws, user_id=owner_actor)
        # outsider has a grant only on an unrelated workspace.
        _grant(session_membership, workspace_id=unrelated_ws, user_id=outsider)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="ls-out-owner",
            actor_id=owner_actor,
            clock=frozen_clock,
        )
        with pytest.raises(MembershipNotFound):
            list_memberships(
                session_membership,
                _ctx(
                    workspace_id=unrelated_ws,
                    slug="ls-out-other",
                    actor_id=outsider,
                ),
                property_id=prop.id,
            )

    def test_unknown_property_raises_not_found(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="ls-unk")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        with pytest.raises(MembershipNotFound):
            list_memberships(
                session_membership,
                _ctx(workspace_id=owner_ws, slug="ls-unk", actor_id=actor_id),
                property_id="01HWA00000000000000000PRPZ",
            )

    def test_managed_workspace_member_can_list(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        """A member of a managed workspace can read the listing."""
        owner_ws = _make_workspace(session_membership, slug="ls-mgd-owner")
        managed_ws = _make_workspace(session_membership, slug="ls-mgd-managed")
        owner_actor = _make_user(session_membership, label="owner-actor")
        managed_actor = _make_user(session_membership, label="managed-actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=owner_actor)
        _grant(
            session_membership,
            workspace_id=managed_ws,
            user_id=managed_actor,
            grant_role="worker",
        )
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="ls-mgd-owner",
            actor_id=owner_actor,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="ls-mgd-owner", actor_id=owner_actor),
            property_id=prop.id,
            target_workspace_id=managed_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )

        result = list_memberships(
            session_membership,
            _ctx(
                workspace_id=managed_ws,
                slug="ls-mgd-managed",
                actor_id=managed_actor,
            ),
            property_id=prop.id,
        )
        assert {r.workspace_id for r in result} == {owner_ws, managed_ws}


class TestExactlyOneOwner:
    """The §02 invariant: exactly one ``owner_workspace`` per property."""

    def test_invariant_after_transfer_observer(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="inv-old")
        new_owner_ws = _make_workspace(session_membership, slug="inv-new")
        third_ws = _make_workspace(session_membership, slug="inv-third")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="inv-old",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        for ws in (new_owner_ws, third_ws):
            invite_workspace(
                session_membership,
                _ctx(workspace_id=owner_ws, slug="inv-old", actor_id=actor_id),
                property_id=prop.id,
                target_workspace_id=ws,
                role="managed_workspace",
                clock=frozen_clock,
            )

        transfer_ownership(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="inv-old", actor_id=actor_id),
            property_id=prop.id,
            new_owner_workspace_id=new_owner_ws,
            demote_to="observer",
            clock=frozen_clock,
        )

        rows = session_membership.scalars(
            select(PropertyWorkspace).where(
                PropertyWorkspace.property_id == prop.id,
            )
        ).all()
        owners = [r for r in rows if r.membership_role == "owner_workspace"]
        assert len(owners) == 1
        assert owners[0].workspace_id == new_owner_ws

    def test_invariant_after_transfer_revoke(
        self, session_membership: Session, frozen_clock: FrozenClock
    ) -> None:
        owner_ws = _make_workspace(session_membership, slug="inv-rv-old")
        new_owner_ws = _make_workspace(session_membership, slug="inv-rv-new")
        actor_id = _make_user(session_membership, label="actor")
        _grant(session_membership, workspace_id=owner_ws, user_id=actor_id)
        prop = _create_property(
            session_membership,
            owner_ws_id=owner_ws,
            owner_ws_slug="inv-rv-old",
            actor_id=actor_id,
            clock=frozen_clock,
        )
        invite_workspace(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="inv-rv-old", actor_id=actor_id),
            property_id=prop.id,
            target_workspace_id=new_owner_ws,
            role="managed_workspace",
            clock=frozen_clock,
        )
        transfer_ownership(
            session_membership,
            _ctx(workspace_id=owner_ws, slug="inv-rv-old", actor_id=actor_id),
            property_id=prop.id,
            new_owner_workspace_id=new_owner_ws,
            demote_to="revoke",
            clock=frozen_clock,
        )
        rows = session_membership.scalars(
            select(PropertyWorkspace).where(
                PropertyWorkspace.property_id == prop.id,
            )
        ).all()
        owners = [r for r in rows if r.membership_role == "owner_workspace"]
        assert len(owners) == 1
        assert owners[0].workspace_id == new_owner_ws
