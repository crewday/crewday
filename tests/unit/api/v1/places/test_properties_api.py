"""HTTP-level tests for ``/properties`` (cd-lzh1).

Exercises the workspace properties roster endpoint:

* manager / owner can list the roster (200, valid shape);
* worker is rejected with 403 ``permission_denied``;
* cross-workspace bleed-through is impossible;
* the projection joins :class:`Property`, :class:`PropertyWorkspace`,
  and :class:`Area` correctly (areas nested per property,
  soft-deleted rows excluded);
* the response is a bare ``Property[]`` array (no envelope) and
  honours every field declared in
  ``app/web/src/types/property.ts``;
* the OpenAPI document carries both ``places`` and ``properties``
  tags + the canonical ``properties.list`` operation id.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.places.models import Area, Property, PropertyWorkspace
from app.api.v1.places import _COLOR_PALETTE, _color_for, build_properties_router
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", build_properties_router())], factory, ctx)


def _seed_property(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    name: str = "Villa Test",
    city: str = "Antibes",
    timezone: str = "Europe/Paris",
    kind: str = "vacation",
    country: str = "FR",
    locale: str | None = "fr-FR",
    address: str = "1 Test Street",
    client_org_id: str | None = None,
    owner_user_id: str | None = None,
    deleted: bool = False,
    created_at: datetime = _PINNED,
) -> str:
    """Insert a :class:`Property` row + the workspace junction.

    Mirrors the test seed used by the employees API tests but exposes
    every field the SPA's :class:`Property` projects so each assertion
    can pin its own fixture without churning a shared row.
    """
    address_json: dict[str, Any] = {
        "line1": address,
        "line2": None,
        "city": city,
        "state_province": None,
        "postal_code": None,
        "country": country,
    }
    with factory() as s:
        prop = Property(
            id=new_ulid(),
            name=name,
            kind=kind,
            address=address,
            address_json=address_json,
            country=country,
            locale=locale,
            default_currency=None,
            timezone=timezone,
            lat=None,
            lon=None,
            client_org_id=client_org_id,
            owner_user_id=owner_user_id,
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=created_at,
            updated_at=created_at,
            deleted_at=_PINNED if deleted else None,
        )
        s.add(prop)
        s.flush()
        s.add(
            PropertyWorkspace(
                property_id=prop.id,
                workspace_id=workspace_id,
                label=name,
                membership_role="owner_workspace",
                created_at=_PINNED,
            )
        )
        s.commit()
        return prop.id


def _seed_area(
    factory: sessionmaker[Session],
    *,
    property_id: str,
    label: str,
    ordering: int = 0,
    icon: str | None = None,
) -> str:
    with factory() as s:
        row = Area(
            id=new_ulid(),
            property_id=property_id,
            label=label,
            icon=icon,
            ordering=ordering,
            created_at=_PINNED,
        )
        s.add(row)
        s.commit()
        return row.id


# ---------------------------------------------------------------------------
# AuthZ
# ---------------------------------------------------------------------------


class TestAuthZ:
    def test_owner_can_list(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Owner / manager surface holds the gate by default-allow."""
        ctx, factory, ws_id = owner_ctx
        _seed_property(factory, workspace_id=ws_id, name="Villa Sud")
        client = _client(ctx, factory)
        resp = client.get("/properties")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["name"] == "Villa Sud"

    def test_manager_can_list(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A pure manager (no owners-group membership) passes the gate.

        ``properties.read``'s ``default_allow`` covers both ``owners``
        and ``managers``. To exercise the manager branch we seed a
        fresh user with ONLY a ``RoleGrant.grant_role='manager'`` row
        — no owners-group membership — so the resolver must fall
        through to the derived ``managers`` group check
        (:func:`app.authz.membership.is_member_of`). A regression
        that drops ``managers`` from ``default_allow`` would surface
        as a 403 here.
        """
        from app.adapters.db.authz.models import RoleGrant
        from tests.factories.identity import build_workspace_context

        ctx, factory, ws_id = owner_ctx
        with factory() as s:
            mgr_user = bootstrap_user(
                s, email="mgr@example.com", display_name="Manager"
            )
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=mgr_user.id,
                    grant_role="manager",
                    scope_property_id=None,
                    created_at=_PINNED,
                    created_by_user_id=None,
                )
            )
            s.commit()
            mgr_user_id = mgr_user.id
        manager_ctx = build_workspace_context(
            workspace_id=ctx.workspace_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=mgr_user_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
        )
        _seed_property(factory, workspace_id=ws_id, name="Villa Sud")
        client = _client(manager_ctx, factory)
        resp = client.get("/properties")
        assert resp.status_code == 200, resp.text

    def test_worker_403(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Workers do not hold ``properties.read`` — must be 403."""
        ctx, factory, ws_id, _ = worker_ctx
        _seed_property(factory, workspace_id=ws_id, name="Villa Sud")
        client = _client(ctx, factory)
        resp = client.get("/properties")
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "permission_denied"


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


class TestTenancy:
    def test_cross_workspace_blocked(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A property linked only to workspace B never bleeds into A's roster."""
        ctx, factory, ws_a_id = owner_ctx
        # Sibling workspace + owner.
        with factory() as s:
            sibling_owner = bootstrap_user(
                s, email="other@example.com", display_name="Other Owner"
            )
            ws_b = bootstrap_workspace(
                s,
                slug="ws-sibling",
                name="Sibling WS",
                owner_user_id=sibling_owner.id,
            )
            s.commit()
            ws_b_id = ws_b.id
        _seed_property(factory, workspace_id=ws_a_id, name="Villa A")
        sibling_id = _seed_property(factory, workspace_id=ws_b_id, name="Villa B")

        client = _client(ctx, factory)
        body = client.get("/properties").json()
        ids = {row["id"] for row in body}
        assert sibling_id not in ids
        # Sanity: the workspace-A property is still listed.
        assert {row["name"] for row in body} == {"Villa A"}


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


class TestShape:
    def test_returns_bare_array_not_envelope(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Critical contract: SPA expects ``Property[]``, not ``{data, ...}``.

        cd-lzh1 records the bare-array decision (matching the cd-g6nf
        ``/employees`` precedent); a future refactor that introduces
        the standard ``{data, next_cursor, has_more}`` envelope MUST
        also migrate every ``fetchJson<Property[]>`` call site in the
        SPA in lockstep. This assertion is the sentinel that catches a
        one-sided change.
        """
        ctx, factory, ws_id = owner_ctx
        _seed_property(factory, workspace_id=ws_id, name="Villa Sud")
        client = _client(ctx, factory)
        body = client.get("/properties").json()
        assert isinstance(body, list), (
            "GET /properties must return a JSON array — see cd-lzh1"
        )
        for row in body:
            assert isinstance(row, dict)
            assert "data" not in row, "envelope sentinel leaked into row"

    def test_shape_matches_spa_type(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Every key in app/web/src/types/property.ts must round-trip."""
        ctx, factory, ws_id = owner_ctx
        _seed_property(factory, workspace_id=ws_id, name="Villa Sud")
        client = _client(ctx, factory)
        body = client.get("/properties").json()
        row = body[0]
        # SPA-required field set — keep this assertion in lockstep with
        # ``app/web/src/types/property.ts``. A mismatch surfaces as a
        # TypeError in the SPA before render, so the contract is cheap
        # to enforce here.
        expected = {
            "id",
            "name",
            "city",
            "timezone",
            "color",
            "kind",
            "areas",
            "evidence_policy",
            "country",
            "locale",
            "settings_override",
            "client_org_id",
            "owner_user_id",
        }
        assert set(row.keys()) == expected
        # Spot-check the projected types so a regression that flips a
        # string to an int (or drops a default to ``null``) trips a
        # clean assertion failure rather than confusing the SPA.
        assert isinstance(row["id"], str)
        assert isinstance(row["name"], str)
        assert isinstance(row["city"], str)
        assert isinstance(row["timezone"], str)
        assert row["color"] in {"moss", "sky", "rust"}
        assert row["kind"] in {"str", "vacation", "residence", "mixed"}
        assert isinstance(row["areas"], list)
        assert row["evidence_policy"] in {"inherit", "require", "optional", "forbid"}
        assert isinstance(row["country"], str)
        assert isinstance(row["locale"], str)
        assert isinstance(row["settings_override"], dict)
        # ``client_org_id`` / ``owner_user_id`` are nullable strings —
        # the seed sets them to ``None`` so the wire shape is JSON null.
        assert row["client_org_id"] is None
        assert row["owner_user_id"] is None


# ---------------------------------------------------------------------------
# Areas join
# ---------------------------------------------------------------------------


class TestAreas:
    def test_areas_nested_per_property(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Each property carries its own areas, ordered by ``ordering``.

        Two properties with overlapping area labels must NOT cross-
        pollinate — area rows are scoped by ``property_id``. Within a
        property the SPA's ``Property.areas`` is rendered in the
        seeded ``ordering`` order; ties break alphabetically by label.
        """
        ctx, factory, ws_id = owner_ctx
        # Property A — three areas with explicit ordering.
        prop_a = _seed_property(factory, workspace_id=ws_id, name="Villa A")
        _seed_area(factory, property_id=prop_a, label="Kitchen", ordering=2)
        _seed_area(factory, property_id=prop_a, label="Pool", ordering=1)
        _seed_area(factory, property_id=prop_a, label="Garden", ordering=3)
        # Property B — disjoint set, same workspace.
        prop_b = _seed_property(factory, workspace_id=ws_id, name="Villa B")
        _seed_area(factory, property_id=prop_b, label="Studio", ordering=0)

        client = _client(ctx, factory)
        body = client.get("/properties").json()
        rows = {row["id"]: row for row in body}
        # Ordering ascending, alphabetical tiebreak: Pool(1) → Kitchen(2) → Garden(3).
        assert rows[prop_a]["areas"] == ["Pool", "Kitchen", "Garden"]
        assert rows[prop_b]["areas"] == ["Studio"]

    def test_property_with_no_areas_returns_empty_list(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A property without any :class:`Area` rows projects ``areas=[]``.

        The SPA's ``Property.areas`` is typed as ``string[]`` (not
        ``string[] | null``) so a missing list MUST surface as an
        empty array, not ``null``.
        """
        ctx, factory, ws_id = owner_ctx
        _seed_property(factory, workspace_id=ws_id, name="Villa Bare")
        client = _client(ctx, factory)
        row = client.get("/properties").json()[0]
        assert row["areas"] == []


# ---------------------------------------------------------------------------
# Soft delete
# ---------------------------------------------------------------------------


class TestSoftDelete:
    def test_soft_deleted_property_excluded(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A property with ``deleted_at IS NOT NULL`` never surfaces.

        Soft-delete is the cd-8u5 retire flow; the read-side guard
        keeps the roster clean of retired rows even if the junction
        row survives.
        """
        ctx, factory, ws_id = owner_ctx
        live = _seed_property(factory, workspace_id=ws_id, name="Live Villa")
        # Seed a second property and soft-delete it directly.
        gone = _seed_property(factory, workspace_id=ws_id, name="Retired Villa")
        with factory() as s, tenant_agnostic():
            row = s.get(Property, gone)
            assert row is not None
            row.deleted_at = _PINNED
            s.commit()

        client = _client(ctx, factory)
        body = client.get("/properties").json()
        ids = {r["id"] for r in body}
        assert live in ids
        assert gone not in ids


# ---------------------------------------------------------------------------
# OpenAPI surface
# ---------------------------------------------------------------------------


class TestOpenAPI:
    def test_openapi_carries_tags(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """The list operation tags as both ``places`` and ``properties``.

        ``places`` clusters the operation under the §01 places context;
        ``properties`` keeps the per-resource tag the SPA codegen
        groups operations by. The operation id is the canonical
        ``properties.list`` per spec §12 "OpenAPI".
        """
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        schema = client.get("/openapi.json").json()
        list_op = schema["paths"]["/properties"]["get"]
        assert "places" in list_op["tags"]
        assert "properties" in list_op["tags"]
        assert list_op["operationId"] == "properties.list"


# ---------------------------------------------------------------------------
# Empty roster
# ---------------------------------------------------------------------------


class TestEmptyRoster:
    def test_no_properties_returns_empty_array(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """The flat-array contract requires a true ``[]`` when nothing matches.

        :func:`bootstrap_workspace` does NOT seed any property rows, so
        the baseline owner ctx already exercises this branch — the
        assertion pins it explicitly so a regression that returns
        ``{}`` or ``{"data": []}`` is caught loudly.
        """
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get("/properties")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []


# ---------------------------------------------------------------------------
# Color stability
# ---------------------------------------------------------------------------


class TestColorStability:
    """The accent color must be stable across reloads + process restarts.

    cd-lzh1 picks the SPA-facing :data:`PropertyColor` from a
    deterministic SHA-256 of the property id (built-in :func:`hash`
    would shuffle across restarts because it is salted per-process).
    A regression that swaps SHA-256 for :func:`hash`, or that mutates
    the palette ordering, would surface as a flaky color in the
    manager UI — pinning two assertions catches both classes of
    breakage.
    """

    def test_same_id_yields_same_color_across_calls(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Two requests against the same row return the same color."""
        ctx, factory, ws_id = owner_ctx
        _seed_property(factory, workspace_id=ws_id, name="Stable Villa")
        client = _client(ctx, factory)
        first = client.get("/properties").json()[0]["color"]
        second = client.get("/properties").json()[0]["color"]
        assert first == second

    def test_color_matches_sha256_palette_index(self) -> None:
        """The mapping is ``palette[sha256(id)[0] % len(palette)]``.

        Pinning the recipe — not just the determinism — protects the
        contract: a future "let's just use ``hash``" optimisation
        would silently shift every property's accent on restart and
        break the manager's spatial memory. A direct hash recompute
        against the documented palette catches the swap.
        """
        for raw_id in (
            "01HZ8K2X9C3M7P5R6T8W0Y1Z2A",  # ULID-shaped sample
            "abc",
            "",
            "🦊",  # non-ASCII still hashes fine
        ):
            digest = hashlib.sha256(raw_id.encode("utf-8")).digest()
            assert _color_for(raw_id) == _COLOR_PALETTE[digest[0] % len(_COLOR_PALETTE)]
