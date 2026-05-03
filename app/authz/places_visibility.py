"""Property visibility projection from a user's role grants (cd-atvn).

Both the workers' narrowed properties roster (``GET /properties``,
``app/api/v1/places.py``) and the manager roster's per-employee
property fan-out (``GET /employees`` and the dashboard aggregate,
``app/api/v1/employees.py`` / ``app/api/v1/dashboard.py``) ask the
same question: **for ``(workspace, user)``, which property ids does
the user appear on?**

The answer is a join over :class:`RoleGrant` (the source of property
scoping) and ``PropertyWorkspace x Property`` (the live-property
universe in the workspace):

* A workspace-wide grant (``scope_property_id IS NULL``) fans out
  across every live property bound to the workspace
  (``Property.deleted_at IS NULL`` and ``PropertyWorkspace.status =
  'active'``).
* A property-pinned grant (``scope_property_id`` set) narrows to that
  single property if it's still live in the workspace; a grant
  pointing at a retired, sibling-workspace, or not-yet-accepted
  (``invited``) property collapses out.
* Soft-retired grants (``revoked_at IS NOT NULL``) never widen the
  view — only live grants count (cd-x1xh).
* No matching grants → empty set.

The two prior in-line copies (``places.py::_visible_property_ids_for_worker``
and ``employees.py::_load_property_ids_by_user``) shared a reason to
change (every evolution of property-grant scoping rules touches both).
This helper is the single seam they consume.

**Behaviour delta on consolidation.** The prior copies filtered only
``Property.deleted_at IS NULL`` and skipped ``PropertyWorkspace.status``.
Every other reader of the junction in the repo (places /
billing / instructions / payroll repositories) gates on
``status = 'active'``; the cd-hsk invite lifecycle says ``invited``
rows are not yet in-force. Tightening here aligns the worker /
manager roster fan-out with the rest of the read-side and lands
the gate that should have been there from cd-hsk. Existing tests
seed rows with the default ``status='active'`` so behaviour is
unchanged on the green paths.

Per the package docstring, modules under :mod:`app.authz` MAY import
from :mod:`app.adapters` directly — this helper is a thin DB shim
and lives here so the property-visibility decision has one home,
even though the call sites are HTTP handlers.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.places.models import Property, PropertyWorkspace

__all__ = ["visible_property_ids_by_user", "visible_property_ids_for_user"]


def _live_workspace_property_ids(session: Session, *, workspace_id: str) -> set[str]:
    """Return the live property ids bound to ``workspace_id``.

    Joins :class:`PropertyWorkspace` to :class:`Property` so the gate
    enforces ``Property.deleted_at IS NULL`` — retired properties never
    leak into the result. Also filters ``PropertyWorkspace.status =
    'active'`` so an ``invited`` (not-yet-accepted) sibling row stays
    out of the worker's view (§02 "Villa belongs to many workspaces"
    — only ``active`` rows are in-force).
    """
    stmt = (
        select(PropertyWorkspace.property_id)
        .join(Property, Property.id == PropertyWorkspace.property_id)
        .where(
            PropertyWorkspace.workspace_id == workspace_id,
            PropertyWorkspace.status == "active",
            Property.deleted_at.is_(None),
        )
    )
    return set(session.scalars(stmt).all())


def visible_property_ids_for_user(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
) -> set[str]:
    """Return the property ids ``user_id`` may see in ``workspace_id``.

    Walks live :class:`RoleGrant` rows for the user in the workspace
    and projects them onto the workspace's live properties:

    * Workspace-wide grant → every live workspace property.
    * Property-pinned grant → the pinned property if it's live in the
      workspace; otherwise the grant collapses out.
    * No live grants → empty set.

    Soft-retired grants (``revoked_at IS NOT NULL``) are filtered out
    at the source (cd-x1xh). Property gating uses the same
    ``PropertyWorkspace x Property`` join the manager roster uses,
    so both call sites agree on what "live" means.
    """
    grants_stmt = select(RoleGrant.scope_property_id).where(
        RoleGrant.workspace_id == workspace_id,
        RoleGrant.user_id == user_id,
        # cd-x1xh: live grants only — soft-retired grants must not
        # widen the caller's visible-property fan-out.
        RoleGrant.revoked_at.is_(None),
    )
    scope_property_ids = list(session.scalars(grants_stmt).all())
    if not scope_property_ids:
        return set()

    live = _live_workspace_property_ids(session, workspace_id=workspace_id)
    if not live:
        return set()

    has_workspace_grant = any(pid is None for pid in scope_property_ids)
    if has_workspace_grant:
        return set(live)

    visible: set[str] = set()
    for pid in scope_property_ids:
        if pid is not None and pid in live:
            visible.add(pid)
    return visible


def visible_property_ids_by_user(
    session: Session,
    *,
    workspace_id: str,
    user_ids: list[str],
) -> dict[str, list[str]]:
    """Batched fan-out of :func:`visible_property_ids_for_user`.

    Returns ``{user_id: sorted([property_id, ...])}`` — the manager
    roster's per-employee shape (``app/api/v1/employees.py`` and
    ``app/api/v1/dashboard.py``). Single ``role_grant`` SELECT for the
    whole batch + one ``PropertyWorkspace x Property`` SELECT for the
    workspace's live ids; the in-memory walk is bounded by the user
    count x per-user grant fan-out (typically a handful).

    Users with no live grants are omitted from the result; callers
    default to ``[]`` via ``out.get(user_id, [])``.
    """
    if not user_ids:
        return {}

    grants_stmt = select(RoleGrant.user_id, RoleGrant.scope_property_id).where(
        RoleGrant.workspace_id == workspace_id,
        RoleGrant.user_id.in_(user_ids),
        # cd-x1xh: live grants only — a soft-retired grant must
        # not widen a user's property visibility on the roster.
        RoleGrant.revoked_at.is_(None),
    )
    grants_by_user: dict[str, list[str | None]] = defaultdict(list)
    for grant_user_id, scope_property_id in session.execute(grants_stmt).all():
        grants_by_user[grant_user_id].append(scope_property_id)

    if not grants_by_user:
        return {}

    live = _live_workspace_property_ids(session, workspace_id=workspace_id)

    out: dict[str, list[str]] = {}
    for grant_user_id, scope_property_ids in grants_by_user.items():
        bucket: set[str] = set()
        has_workspace_grant = any(pid is None for pid in scope_property_ids)
        if has_workspace_grant:
            bucket.update(live)
        for pid in scope_property_ids:
            if pid is not None and pid in live:
                bucket.add(pid)
        out[grant_user_id] = sorted(bucket)
    return out
