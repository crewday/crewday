"""Unit tests for :mod:`app.authz.places_visibility` (cd-atvn).

Pins the role_grant fan-out shape that both the workers' narrowed
properties roster (``places.py``) and the manager-roster +
dashboard fan-out (``employees.py`` / ``dashboard.py``) consume:

* Workspace-wide grant (``scope_property_id IS NULL``) → every live
  workspace property.
* Property-pinned grant → just that property if it's live in the
  workspace; retired or sibling-workspace targets collapse out.
* Mixed grants → union of the above.
* No grants → empty set / missing key.
* Soft-retired grants (``revoked_at IS NOT NULL``) → never widen the
  view (cd-x1xh).
* ``PropertyWorkspace.status='invited'`` → not yet in-force, must
  not surface (cd-hsk).

The two surfaces of the helper get matching coverage:

* :func:`visible_property_ids_for_user` — single-actor ``set[str]``
  shape consumed by ``places.py``.
* :func:`visible_property_ids_by_user` — batched per-user shape
  consumed by ``employees.py`` + ``dashboard.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.authz.places_visibility import (
    visible_property_ids_by_user,
    visible_property_ids_for_user,
)
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine with every ORM table created."""
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def _seed_workspace(session: Session, *, slug: str = "vis-ws") -> str:
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name="Visibility WS",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


def _seed_user(session: Session, *, tag: str) -> str:
    user_id = new_ulid()
    email = f"{tag}@example.com"
    session.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=tag.capitalize(),
            created_at=_PINNED,
        )
    )
    session.flush()
    return user_id


def _seed_property(
    session: Session,
    *,
    workspace_id: str,
    name: str,
    deleted: bool = False,
    junction_status: str = "active",
) -> str:
    address_json: dict[str, Any] = {
        "line1": f"{name} 1",
        "line2": None,
        "city": "Nice",
        "state_province": None,
        "postal_code": None,
        "country": "FR",
    }
    property_id = new_ulid()
    session.add(
        Property(
            id=property_id,
            name=name,
            kind="vacation",
            address=f"{name} 1, Nice, FR",
            address_json=address_json,
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
            deleted_at=_PINNED if deleted else None,
        )
    )
    session.flush()
    session.add(
        PropertyWorkspace(
            property_id=property_id,
            workspace_id=workspace_id,
            label=name,
            membership_role="owner_workspace",
            status=junction_status,
            created_at=_PINNED,
        )
    )
    session.flush()
    return property_id


def _seed_grant(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
    grant_role: str = "worker",
    scope_property_id: str | None = None,
    revoked: bool = False,
) -> str:
    grant_id = new_ulid()
    session.add(
        RoleGrant(
            id=grant_id,
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_property_id=scope_property_id,
            revoked_at=_PINNED if revoked else None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    session.flush()
    return grant_id


class TestVisiblePropertyIdsForUser:
    """Single-actor projection — the ``places.py`` shape."""

    def test_workspace_wide_grant_fans_out_to_every_live_property(
        self, factory: sessionmaker[Session]
    ) -> None:
        with factory() as s:
            workspace_id = _seed_workspace(s)
            user_id = _seed_user(s, tag="ws-grant")
            p1 = _seed_property(s, workspace_id=workspace_id, name="Alpha")
            p2 = _seed_property(s, workspace_id=workspace_id, name="Bravo")
            _seed_grant(s, workspace_id=workspace_id, user_id=user_id)
            s.commit()
            assert visible_property_ids_for_user(
                s, workspace_id=workspace_id, user_id=user_id
            ) == {p1, p2}

    def test_property_pinned_grant_narrows_to_one(
        self, factory: sessionmaker[Session]
    ) -> None:
        with factory() as s:
            workspace_id = _seed_workspace(s)
            user_id = _seed_user(s, tag="pinned")
            p1 = _seed_property(s, workspace_id=workspace_id, name="Alpha")
            _seed_property(s, workspace_id=workspace_id, name="Bravo")
            _seed_grant(
                s,
                workspace_id=workspace_id,
                user_id=user_id,
                scope_property_id=p1,
            )
            s.commit()
            assert visible_property_ids_for_user(
                s, workspace_id=workspace_id, user_id=user_id
            ) == {p1}

    def test_mixed_workspace_and_pinned_to_deleted_property(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Workspace-wide grant wins; the pinned-but-retired target
        is silently dropped from the live universe.
        """
        with factory() as s:
            workspace_id = _seed_workspace(s)
            user_id = _seed_user(s, tag="mixed")
            p_live = _seed_property(s, workspace_id=workspace_id, name="Live")
            p_dead = _seed_property(
                s, workspace_id=workspace_id, name="Retired", deleted=True
            )
            _seed_grant(s, workspace_id=workspace_id, user_id=user_id)
            _seed_grant(
                s,
                workspace_id=workspace_id,
                user_id=user_id,
                scope_property_id=p_dead,
            )
            s.commit()
            assert visible_property_ids_for_user(
                s, workspace_id=workspace_id, user_id=user_id
            ) == {p_live}

    def test_pinned_grant_to_retired_property_collapses(
        self, factory: sessionmaker[Session]
    ) -> None:
        """The only grant the user has points at a soft-deleted
        property — the result is the empty set, not the dangling id.
        """
        with factory() as s:
            workspace_id = _seed_workspace(s)
            user_id = _seed_user(s, tag="dead-pin")
            p_dead = _seed_property(
                s, workspace_id=workspace_id, name="Retired", deleted=True
            )
            _seed_property(s, workspace_id=workspace_id, name="Other")
            _seed_grant(
                s,
                workspace_id=workspace_id,
                user_id=user_id,
                scope_property_id=p_dead,
            )
            s.commit()
            assert (
                visible_property_ids_for_user(
                    s, workspace_id=workspace_id, user_id=user_id
                )
                == set()
            )

    def test_no_grants_returns_empty(self, factory: sessionmaker[Session]) -> None:
        with factory() as s:
            workspace_id = _seed_workspace(s)
            user_id = _seed_user(s, tag="ungranted")
            _seed_property(s, workspace_id=workspace_id, name="Alpha")
            s.commit()
            assert (
                visible_property_ids_for_user(
                    s, workspace_id=workspace_id, user_id=user_id
                )
                == set()
            )

    def test_revoked_grant_does_not_widen_view(
        self, factory: sessionmaker[Session]
    ) -> None:
        """cd-x1xh: a soft-retired grant must not surface a property id."""
        with factory() as s:
            workspace_id = _seed_workspace(s)
            user_id = _seed_user(s, tag="revoked")
            p1 = _seed_property(s, workspace_id=workspace_id, name="Alpha")
            _seed_grant(
                s,
                workspace_id=workspace_id,
                user_id=user_id,
                scope_property_id=p1,
                revoked=True,
            )
            s.commit()
            assert (
                visible_property_ids_for_user(
                    s, workspace_id=workspace_id, user_id=user_id
                )
                == set()
            )

    def test_invited_junction_row_excluded(
        self, factory: sessionmaker[Session]
    ) -> None:
        """cd-hsk: ``status='invited'`` means the workspace has not yet
        accepted the share; the property must not surface on a
        workspace-wide grant fan-out.
        """
        with factory() as s:
            workspace_id = _seed_workspace(s)
            user_id = _seed_user(s, tag="invited")
            p_active = _seed_property(s, workspace_id=workspace_id, name="Active")
            _seed_property(
                s,
                workspace_id=workspace_id,
                name="Pending",
                junction_status="invited",
            )
            _seed_grant(s, workspace_id=workspace_id, user_id=user_id)
            s.commit()
            assert visible_property_ids_for_user(
                s, workspace_id=workspace_id, user_id=user_id
            ) == {p_active}

    def test_sibling_workspace_grant_does_not_leak(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A pinned grant on this workspace whose target is bound to a
        *different* workspace's junction must collapse to empty —
        ``PropertyWorkspace`` filters by ``workspace_id`` so the
        property is not in this workspace's live universe.
        """
        with factory() as s:
            workspace_id = _seed_workspace(s, slug="primary")
            other_ws = _seed_workspace(s, slug="sibling")
            user_id = _seed_user(s, tag="cross")
            sibling_only = _seed_property(s, workspace_id=other_ws, name="Sibling")
            _seed_grant(
                s,
                workspace_id=workspace_id,
                user_id=user_id,
                scope_property_id=sibling_only,
            )
            s.commit()
            assert (
                visible_property_ids_for_user(
                    s, workspace_id=workspace_id, user_id=user_id
                )
                == set()
            )


class TestVisiblePropertyIdsByUser:
    """Batched projection — the ``employees.py`` / ``dashboard.py`` shape."""

    def test_empty_user_ids_short_circuits(
        self, factory: sessionmaker[Session]
    ) -> None:
        with factory() as s:
            workspace_id = _seed_workspace(s)
            s.commit()
            assert (
                visible_property_ids_by_user(s, workspace_id=workspace_id, user_ids=[])
                == {}
            )

    def test_per_user_shape_sorted_lists(self, factory: sessionmaker[Session]) -> None:
        """Two users on the same workspace get distinct projections;
        the per-user lists are sorted (matches the existing roster
        contract that ``properties_api`` and the SPA expect).
        """
        with factory() as s:
            workspace_id = _seed_workspace(s)
            ws_user = _seed_user(s, tag="ws")
            pinned_user = _seed_user(s, tag="pinned")
            _seed_user(s, tag="ungranted")
            p1 = _seed_property(s, workspace_id=workspace_id, name="Alpha")
            p2 = _seed_property(s, workspace_id=workspace_id, name="Bravo")
            _seed_grant(s, workspace_id=workspace_id, user_id=ws_user)
            _seed_grant(
                s,
                workspace_id=workspace_id,
                user_id=pinned_user,
                scope_property_id=p2,
            )
            s.commit()
            out = visible_property_ids_by_user(
                s,
                workspace_id=workspace_id,
                user_ids=[ws_user, pinned_user],
            )
            assert out[ws_user] == sorted([p1, p2])
            assert out[pinned_user] == [p2]

    def test_users_without_live_grants_omitted(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A user passed in ``user_ids`` who has no live grants is
        absent from the result; callers default to ``[]`` via
        ``out.get(user_id, [])``.
        """
        with factory() as s:
            workspace_id = _seed_workspace(s)
            granted = _seed_user(s, tag="granted")
            ungranted = _seed_user(s, tag="ungranted")
            p1 = _seed_property(s, workspace_id=workspace_id, name="Alpha")
            _seed_grant(
                s,
                workspace_id=workspace_id,
                user_id=granted,
                scope_property_id=p1,
            )
            s.commit()
            out = visible_property_ids_by_user(
                s,
                workspace_id=workspace_id,
                user_ids=[granted, ungranted],
            )
            assert out == {granted: [p1]}

    def test_revoked_grant_filtered_in_batch(
        self, factory: sessionmaker[Session]
    ) -> None:
        """cd-x1xh: a soft-retired grant in the batch must not widen
        the user's roster entry. With only a revoked grant, the user
        drops out of the result map entirely.
        """
        with factory() as s:
            workspace_id = _seed_workspace(s)
            user_id = _seed_user(s, tag="revoked")
            _seed_property(s, workspace_id=workspace_id, name="Alpha")
            _seed_grant(
                s,
                workspace_id=workspace_id,
                user_id=user_id,
                revoked=True,
            )
            s.commit()
            assert (
                visible_property_ids_by_user(
                    s, workspace_id=workspace_id, user_ids=[user_id]
                )
                == {}
            )

    def test_invited_junction_row_excluded_in_batch(
        self, factory: sessionmaker[Session]
    ) -> None:
        """cd-hsk: ``status='invited'`` rows must drop out of the
        batched manager-roster fan-out too, not just the single-actor
        shape — both surfaces consume :func:`_live_workspace_property_ids`
        but the test pins the contract on each public entry point so
        a regression on one path can't sneak through unnoticed.
        """
        with factory() as s:
            workspace_id = _seed_workspace(s)
            user_id = _seed_user(s, tag="invited-batch")
            p_active = _seed_property(s, workspace_id=workspace_id, name="Active")
            _seed_property(
                s,
                workspace_id=workspace_id,
                name="Pending",
                junction_status="invited",
            )
            _seed_grant(s, workspace_id=workspace_id, user_id=user_id)
            s.commit()
            assert visible_property_ids_by_user(
                s, workspace_id=workspace_id, user_ids=[user_id]
            ) == {user_id: [p_active]}
