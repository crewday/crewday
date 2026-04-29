"""Places context — properties, units, areas, closures.

Public surface:

* :class:`~app.domain.places.ports.PropertyWorkRoleAssignmentRepository` —
  repository port for the per-property pinning seam (cd-kezq); see
  :mod:`app.domain.places.property_work_role_assignments` for the
  consumer.
* :class:`~app.domain.places.ports.PropertyWorkRoleAssignmentRow` —
  immutable row projection returned by the repo above.
* :class:`~app.domain.places.ports.DuplicateActiveAssignment` /
  :class:`~app.domain.places.ports.AssignmentIntegrityError` — typed
  exceptions the SA adapter raises on the partial-UNIQUE +
  FK-miss flush-time integrity flavours respectively.
* :mod:`app.domain.places.membership_service` (cd-hsk) — the
  ``property_workspace`` junction service: invite / accept / revoke /
  re-role / transfer / list. Exported through this package so
  callers in sibling contexts import via the public surface rather
  than reaching into the module path.

See docs/specs/04-properties-and-stays.md and
docs/specs/05-employees-and-roles.md §"Property work role assignment".
"""

from app.domain.places.area_service import (
    AreaCreate,
    AreaKind,
    AreaNestingTooDeep,
    AreaNotFound,
    AreaReorderItem,
    AreaUpdate,
    AreaView,
    create_area,
    delete_area,
    get_area,
    list_areas,
    move_area,
    reorder_areas,
    update_area,
)
from app.domain.places.closure_service import (
    ClosureClashes,
    ClosureNotFound,
    ClosureReason,
    ClosureScheduleClash,
    ClosureStayClash,
    PropertyClosureCreate,
    PropertyClosureUpdate,
    PropertyClosureView,
    create_closure,
    delete_closure,
    detect_clashes,
    list_closures,
    update_closure,
)
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
from app.domain.places.ports import (
    AssignmentIntegrityError,
    DuplicateActiveAssignment,
    PropertyWorkRoleAssignmentRepository,
    PropertyWorkRoleAssignmentRow,
)

__all__ = [
    "AreaCreate",
    "AreaKind",
    "AreaNestingTooDeep",
    "AreaNotFound",
    "AreaReorderItem",
    "AreaUpdate",
    "AreaView",
    "AssignmentIntegrityError",
    "CannotRevokeOwner",
    "ClosureClashes",
    "ClosureNotFound",
    "ClosureReason",
    "ClosureScheduleClash",
    "ClosureStayClash",
    "DuplicateActiveAssignment",
    "InvalidMembershipRole",
    "InvalidMembershipStatus",
    "MembershipAlreadyExists",
    "MembershipNotFound",
    "MembershipRead",
    "MembershipRole",
    "MembershipStatus",
    "NotOwnerWorkspaceMember",
    "NotWorkspaceMember",
    "OwnerWorkspaceMissing",
    "PropertyClosureCreate",
    "PropertyClosureUpdate",
    "PropertyClosureView",
    "PropertyWorkRoleAssignmentRepository",
    "PropertyWorkRoleAssignmentRow",
    "TransferDemoteAction",
    "accept_invite",
    "create_area",
    "create_closure",
    "delete_area",
    "delete_closure",
    "detect_clashes",
    "get_area",
    "invite_workspace",
    "list_areas",
    "list_closures",
    "list_memberships",
    "move_area",
    "reorder_areas",
    "revoke_workspace",
    "transfer_ownership",
    "update_area",
    "update_closure",
    "update_membership_role",
    "update_share_guest_identity",
]
