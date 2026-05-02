"""Pydantic schema tests for ``POST /users/invite`` body (cd-4o61).

Pure schema-level coverage: the new ``work_engagement`` +
``user_work_roles`` sub-payloads parse, default to ``None`` /
absent when omitted, and reject unknown keys (``extra="forbid"``).
The HTTP-level wiring + downstream persistence live in
``tests/integration/identity/test_membership.py`` and the new
cd-4o61 invite-accept tests; this file owns the request-shape
contract so a wire-shape regression fails fast at unit time.

See ``docs/specs/03-auth-and-tokens.md`` §"Additional users
(invite → click-to-accept)" and ``docs/specs/12-rest-api.md``
§"Users".
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1.users import (
    GrantInput,
    InviteRequest,
    UserWorkRoleInput,
    WorkEngagementInput,
)


class TestWorkEngagementInput:
    """Body shape mirrors the wire description in §03."""

    def test_payroll_only_kind_parses(self) -> None:
        payload = WorkEngagementInput.model_validate({"engagement_kind": "payroll"})
        assert payload.engagement_kind == "payroll"
        assert payload.supplier_org_id is None

    def test_agency_supplied_with_supplier_parses(self) -> None:
        payload = WorkEngagementInput.model_validate(
            {
                "engagement_kind": "agency_supplied",
                "supplier_org_id": "01HWA00000000000000000ORG1",
            }
        )
        assert payload.supplier_org_id == "01HWA00000000000000000ORG1"

    def test_kind_required(self) -> None:
        with pytest.raises(ValidationError):
            WorkEngagementInput.model_validate({})

    def test_extra_keys_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WorkEngagementInput.model_validate(
                {"engagement_kind": "payroll", "rate": 12.5}
            )


class TestUserWorkRoleInput:
    """Each entry is just a ``work_role_id``."""

    def test_work_role_id_parses(self) -> None:
        payload = UserWorkRoleInput.model_validate(
            {"work_role_id": "01HWA00000000000000000WRA1"}
        )
        assert payload.work_role_id == "01HWA00000000000000000WRA1"

    def test_work_role_id_required(self) -> None:
        with pytest.raises(ValidationError):
            UserWorkRoleInput.model_validate({})

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            UserWorkRoleInput.model_validate({"work_role_id": ""})

    def test_extra_keys_rejected(self) -> None:
        with pytest.raises(ValidationError):
            UserWorkRoleInput.model_validate(
                {
                    "work_role_id": "01HWA00000000000000000WRA1",
                    "started_on": "2026-05-02",
                }
            )


class TestInviteRequest:
    """Top-level body wires the new sub-payloads as optional."""

    _WS_ID = "01HWA00000000000000000WS01"

    def _base_grants(self) -> list[dict[str, str]]:
        return [
            {
                "scope_kind": "workspace",
                "scope_id": self._WS_ID,
                "grant_role": "worker",
            }
        ]

    def test_minimal_body_omits_new_fields(self) -> None:
        body = InviteRequest.model_validate(
            {
                "email": "alice@example.com",
                "display_name": "Alice",
                "grants": self._base_grants(),
            }
        )
        assert body.work_engagement is None
        assert body.user_work_roles is None

    def test_body_with_engagement_only(self) -> None:
        body = InviteRequest.model_validate(
            {
                "email": "alice@example.com",
                "display_name": "Alice",
                "grants": self._base_grants(),
                "work_engagement": {"engagement_kind": "contractor"},
            }
        )
        assert body.work_engagement is not None
        assert body.work_engagement.engagement_kind == "contractor"
        assert body.user_work_roles is None

    def test_body_with_user_work_roles_only(self) -> None:
        body = InviteRequest.model_validate(
            {
                "email": "alice@example.com",
                "display_name": "Alice",
                "grants": self._base_grants(),
                "user_work_roles": [
                    {"work_role_id": "01HWA00000000000000000WRA1"},
                    {"work_role_id": "01HWA00000000000000000WRA2"},
                ],
            }
        )
        assert body.user_work_roles is not None
        assert [r.work_role_id for r in body.user_work_roles] == [
            "01HWA00000000000000000WRA1",
            "01HWA00000000000000000WRA2",
        ]

    def test_body_with_both_payloads(self) -> None:
        body = InviteRequest.model_validate(
            {
                "email": "alice@example.com",
                "display_name": "Alice",
                "grants": self._base_grants(),
                "work_engagement": {
                    "engagement_kind": "agency_supplied",
                    "supplier_org_id": "01HWA00000000000000000ORG1",
                },
                "user_work_roles": [{"work_role_id": "01HWA00000000000000000WRA1"}],
            }
        )
        assert body.work_engagement is not None
        assert body.work_engagement.supplier_org_id == "01HWA00000000000000000ORG1"
        assert body.user_work_roles is not None
        assert len(body.user_work_roles) == 1


class TestGrantInputScopeKinds:
    """``GrantInput`` parses the three accepted scope kinds (cd-dagg).

    The wire shape carries ``scope_property_id`` and ``binding_org_id``
    as optional fields. The domain service does the cross-tenant
    validation; this class only proves the body parses without
    raising.
    """

    _WS_ID = "01HWA00000000000000000WS01"
    _PROPERTY_ID = "01HWA0000000000000000P001"
    _ORG_ID = "01HWA00000000000000000ORG1"

    def test_workspace_scope_with_binding_org_id(self) -> None:
        grant = GrantInput.model_validate(
            {
                "scope_kind": "workspace",
                "scope_id": self._WS_ID,
                "grant_role": "client",
                "binding_org_id": self._ORG_ID,
            }
        )
        assert grant.binding_org_id == self._ORG_ID
        assert grant.scope_property_id is None

    def test_property_scope_with_scope_property_id(self) -> None:
        grant = GrantInput.model_validate(
            {
                "scope_kind": "property",
                "scope_id": self._WS_ID,
                "grant_role": "worker",
                "scope_property_id": self._PROPERTY_ID,
            }
        )
        assert grant.scope_kind == "property"
        assert grant.scope_property_id == self._PROPERTY_ID

    def test_organization_scope_parses(self) -> None:
        grant = GrantInput.model_validate(
            {
                "scope_kind": "organization",
                "scope_id": self._ORG_ID,
                "grant_role": "client",
            }
        )
        assert grant.scope_kind == "organization"
        assert grant.scope_id == self._ORG_ID
