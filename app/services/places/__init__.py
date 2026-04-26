"""Public surface of the places context.

Re-exports the property domain service + membership service so callers
in other bounded contexts (stays, tasks, API handlers) import from
here rather than reaching directly into :mod:`app.domain.places`.
Keeps the domain module free to restructure its internals.

See ``docs/specs/04-properties-and-stays.md`` §"Property" /
§"Multi-belonging", ``docs/specs/02-domain-model.md``
§"property_workspace", and ``docs/specs/01-architecture.md``
§"Contexts & boundaries".
"""

from __future__ import annotations

from app.domain.places.membership_service import (
    CannotRevokeOwner,
    InvalidMembershipRole,
    InvalidMembershipStatus,
    MembershipAlreadyExists,
    MembershipNotFound,
    MembershipRead,
    MembershipRole,
    MembershipStatus,
    NotOwnerWorkspaceMember,
    NotWorkspaceMember,
    OwnerWorkspaceMissing,
    TransferDemoteAction,
    accept_invite,
    invite_workspace,
    list_memberships,
    revoke_workspace,
    transfer_ownership,
    update_membership_role,
    update_share_guest_identity,
)
from app.domain.places.property_service import (
    AddressCountryMismatch,
    AddressPayload,
    PropertyCreate,
    PropertyKind,
    PropertyNotFound,
    PropertyUpdate,
    PropertyView,
    create_property,
    get_property,
    list_properties,
    soft_delete_property,
    update_property,
)
from app.domain.places.unit_service import (
    LastUnitProtected,
    UnitCreate,
    UnitNameTaken,
    UnitNotFound,
    UnitUpdate,
    UnitView,
    create_unit,
    get_unit,
    list_units,
    soft_delete_unit,
    update_unit,
)

__all__ = [
    "AddressCountryMismatch",
    "AddressPayload",
    "CannotRevokeOwner",
    "InvalidMembershipRole",
    "InvalidMembershipStatus",
    "LastUnitProtected",
    "MembershipAlreadyExists",
    "MembershipNotFound",
    "MembershipRead",
    "MembershipRole",
    "MembershipStatus",
    "NotOwnerWorkspaceMember",
    "NotWorkspaceMember",
    "OwnerWorkspaceMissing",
    "PropertyCreate",
    "PropertyKind",
    "PropertyNotFound",
    "PropertyUpdate",
    "PropertyView",
    "TransferDemoteAction",
    "UnitCreate",
    "UnitNameTaken",
    "UnitNotFound",
    "UnitUpdate",
    "UnitView",
    "accept_invite",
    "create_property",
    "create_unit",
    "get_property",
    "get_unit",
    "invite_workspace",
    "list_memberships",
    "list_properties",
    "list_units",
    "revoke_workspace",
    "soft_delete_property",
    "soft_delete_unit",
    "transfer_ownership",
    "update_membership_role",
    "update_property",
    "update_share_guest_identity",
    "update_unit",
]
