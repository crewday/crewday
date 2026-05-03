"""Deployment-admin lookup.

The admin surface (``/admin/api/v1/...`` per §12 "Admin") authorises
its callers via *any active* ``role_grant`` row with
``scope_kind = 'deployment'``. This module is the single source of
truth for that question; every admin auth dependency
(:mod:`app.api.v1.deps` once cd-xgmu lands) calls
:func:`is_deployment_admin` rather than re-issuing the SELECT.

Mirrors the shape of :func:`app.authz.owners.is_owner_member` /
:func:`app.authz.owners.is_owner_on_any_workspace` — a single SELECT,
no caching at this layer (the request-scoped admin context cache is
the caller's job), explicit ``user_id`` rather than threading a
context.

**Tenant-agnostic.** The ``role_grant`` table is registered as
workspace-scoped (the workspace partition is what the ORM tenant
filter pins), so a SELECT that targets the deployment partition
(``workspace_id IS NULL``) must opt out of the filter. Without
:func:`tenant_agnostic` the filter would either inject
``workspace_id = :ctx`` (excluding deployment rows by definition) or
fail closed with :class:`TenantFilterMissing` at the bare host —
either outcome is wrong for an admin lookup that runs before any
tenancy resolves. The opt-out is justified: the deployment-grant
universe is by spec ``workspace_id IS NULL`` only.

See ``docs/specs/12-rest-api.md`` §"Admin surface",
``docs/specs/02-domain-model.md`` §"role_grants", and
``docs/specs/05-employees-and-roles.md`` §"Permissions: surface,
groups, and action catalog" §"Deployment groups".
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.tenancy import tenant_agnostic

__all__ = ["is_deployment_admin"]


def is_deployment_admin(session: Session, *, user_id: str) -> bool:
    """Return ``True`` iff ``user_id`` holds any active deployment grant.

    A user is a deployment admin iff there is at least one
    ``role_grant`` row with ``scope_kind='deployment'`` and
    ``user_id=:user_id``. The grant role itself is not consulted here
    — the spec carries the deployment-admin shell on every grant role
    in the deployment partition (§12 "Admin surface" pairs the
    ``role_grant`` row with deployment permission groups for
    fine-grained authority; the row alone authorises *access* to the
    admin surface).

    "Active" in v1 means "exists" — :class:`RoleGrant` does not yet
    carry a ``revoked_at`` column (the v1 slice docstring spells this
    out; cd-79r adds it). Revocation today is a hard delete, so the
    existence check is exactly the right shape; once ``revoked_at``
    Filters to ``revoked_at IS NULL`` so a soft-retired deployment
    admin grant no longer authorises the bare-host admin surface
    (cd-x1xh).

    **Tenant-agnostic.** ``role_grant`` is a workspace-scoped table;
    the deployment partition (``workspace_id IS NULL``) is invisible
    to the ORM tenant filter by design. The lookup runs inside
    :func:`tenant_agnostic` to bypass the filter — bare-host admin
    auth has no workspace ctx to pin against. The opt-out is
    justified by the spec: deployment grants are exactly the rows
    with no workspace.

    **Single SELECT.** No caching; the caller (admin auth dep, the
    ``/auth/me`` resolver) memoizes per request when relevant.
    """
    stmt = (
        select(RoleGrant.id)
        .where(
            RoleGrant.scope_kind == "deployment",
            RoleGrant.user_id == user_id,
            RoleGrant.revoked_at.is_(None),
        )
        .limit(1)
    )
    with tenant_agnostic():
        return session.scalars(stmt).first() is not None
