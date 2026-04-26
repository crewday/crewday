"""Deployment-owners check for the admin tree.

A handful of admin mutations are gated on membership in the
deployment ``owners`` permission group (§12 "Admin surface"):

* ``POST /admin/api/v1/workspaces/{id}/archive`` — owners-only;
* ``PUT /admin/api/v1/settings/{key}`` for root-protected keys
  (e.g. ``trusted_interfaces``);
* ``POST /admin/api/v1/admins/groups/owners/members`` (add) and
  ``POST /admin/api/v1/admins/groups/owners/members/{user_id}/revoke``
  — root-only mutations on the owners-group itself.

The deployment owners permission group is **not yet seeded** —
cd-zkr is the task that lands its bootstrap. Until then the
question "is this caller in ``owners@deployment``?" must always
return ``False`` so the gate fails closed: an unauthorised
mutation is far less harmful than a silent elevation. Once cd-zkr
ships the owners group, :func:`is_deployment_owner` reads the
real ``permission_group_member`` row instead and the gates start
admitting.

The gate **never raises 403**. Spec §12 "Admin surface": "the
surface does not advertise its own existence to tenants" — an
authenticated admin who is not an owner sees the same canonical
``not_found`` envelope as a complete stranger, so the SPA cannot
infer "you'd be allowed to do this if you were an owner". The
helper raises the same :class:`HTTPException` shape the admin
auth dep uses (404 + ``{"error": "not_found"}``) so downstream
exception handling is uniform.

See ``docs/specs/12-rest-api.md`` §"Admin surface" and
``docs/specs/05-employees-and-roles.md`` §"Permissions: surface,
groups, and action catalog" §"Deployment groups".
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.tenancy import DeploymentContext

__all__ = ["ensure_deployment_owner", "is_deployment_owner"]


def is_deployment_owner(
    session: Session,
    *,
    user_id: str,
) -> bool:
    """Return ``True`` iff ``user_id`` belongs to ``owners@deployment``.

    The deployment owners permission group does not exist yet
    (cd-zkr will seed it). Until then the helper hard-returns
    ``False`` so every owner-only gate fails closed — an owner
    mutation today returns 404, matching the spec's surface-
    invisibility contract. When cd-zkr lands the seed, this
    function flips to a single SELECT against
    ``permission_group_member`` joined to ``permission_group``
    on ``slug='owners'`` AND ``workspace_id IS NULL`` (the
    deployment partition).

    The signature already takes ``session`` and ``user_id`` so
    the call sites land today without churn — the cd-zkr swap is
    an internal-only change.
    """
    return False


def ensure_deployment_owner(session: Session, *, ctx: DeploymentContext) -> None:
    """Raise the canonical 404 envelope when ``ctx`` is not an owner.

    Pair with :func:`current_deployment_admin_principal` on a
    route that needs the ``owners@deployment`` gate:

    .. code-block:: python

        @router.post("/workspaces/{id}/archive")
        def archive(
            ctx: Annotated[DeploymentContext, Depends(...)],
            session: _Db,
        ) -> WorkspaceArchiveResponse:
            ensure_deployment_owner(session, ctx=ctx)
            ...

    The 404 envelope mirrors :func:`app.api.admin.deps._not_found`
    so the SPA / CLI cannot tell the "no admin grant at all" miss
    apart from the "admin but not an owner" miss — both flow through
    one canonical ``{"error": "not_found"}`` response. Spec §12:
    "the surface does not advertise its own existence to tenants".
    """
    if not is_deployment_owner(session, user_id=ctx.user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found"},
        )
