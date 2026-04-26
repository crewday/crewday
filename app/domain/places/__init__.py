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

See docs/specs/04-properties-and-stays.md and
docs/specs/05-employees-and-roles.md §"Property work role assignment".
"""

from app.domain.places.ports import (
    AssignmentIntegrityError,
    DuplicateActiveAssignment,
    PropertyWorkRoleAssignmentRepository,
    PropertyWorkRoleAssignmentRow,
)

__all__ = [
    "AssignmentIntegrityError",
    "DuplicateActiveAssignment",
    "PropertyWorkRoleAssignmentRepository",
    "PropertyWorkRoleAssignmentRow",
]
