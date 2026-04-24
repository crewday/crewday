"""Workspace-scoped admin surface aggregator.

This router is the reserved seat for the workspace-scoped admin
tree (§15 "Self-serve abuse mitigations", §01 "admin"). It aggregates
owner/manager-only read-only views that don't fit any single bounded
context — abuse-signal surfacing on :mod:`.signups` first, with
additional admin dashboards (workspace health, security posture)
landing in downstream Beads tasks.

Mounted by :mod:`app.api.factory` via :data:`app.api.v1.CONTEXT_ROUTERS`
under the workspace prefix, so every route lands at
``/w/<slug>/api/v1/admin/...``. The tenancy middleware resolves the
active :class:`~app.tenancy.WorkspaceContext` from the ``<slug>``
segment before any handler runs — admin endpoints therefore always
operate on a concrete workspace, never on the bare host.

**Not the deployment-scoped admin tree.** :mod:`app.api.admin`
(``/admin/api/v1/*``) is a separate, deployment-operator surface
gated on ``(scope_kind='deployment', grant_role='admin')``. The two
trees never overlap: the deployment admin mounts LLM provider
config, cross-workspace usage, and deployment-wide audit; the
workspace admin mounts per-workspace abuse/security surfaces that a
workspace owner or manager needs to inspect without leaving their
tenant.

**OpenAPI / CLI namespace.** The URL segment is ``/admin/`` (spec
§15 "Self-serve abuse mitigations" names ``/admin/signups``
verbatim, so we match that URL without rewriting), but the OpenAPI
tag, ``operation_id`` prefix, and ``x-cli.group`` use
``workspace_admin``. Per §13 ``crewday admin`` is **host-CLI-only**
(no HTTP), and the deployment-scoped HTTP admin lives under CLI
group ``deploy`` — using ``admin`` as an ``x-cli.group`` for an
HTTP route would therefore collide with a reserved host-only
namespace. ``workspace_admin`` (with a dash-separated CLI form,
``workspace-admin``) is the unambiguous third seat and does not
collide with either reserved group.

Authorisation is scope-kind ``'workspace'`` with ``action_key =
'audit_log.view'`` — the closest existing catalog entry (§05). The
spec text for ``/admin/signups`` says "abuse signals written to
audit log and surfaced on the operator-only ``/admin/signups``
page", so ``audit_log.view`` is the semantic fit: same read-only,
same owner/manager default, same ``root_protected_deny`` posture.
A future spec edit may introduce a dedicated ``admin.view`` bit
(tracked as cd-z5rd); in the meantime every route in this tree
uses ``audit_log.view`` so permission rules written today keep
working when the bit is renamed.

See ``docs/specs/15-security-privacy.md`` §"Self-serve abuse
mitigations", ``docs/specs/12-rest-api.md`` §"Base URL", and
``docs/specs/13-cli.md`` §"crewday admin vs crewday deploy".
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.admin.signups import router as signups_router

# Tag is ``workspace_admin`` — see module docstring for why we deliberately
# avoid ``admin`` (collides with the deployment-admin tree's tag in
# :mod:`app.api.admin` and with the reserved host-CLI-only ``admin`` group
# in §13). Aggregator + sub-router both declare the tag so a direct mount
# and a :func:`fastapi.FastAPI.include_router` re-mount both produce the
# canonical tag.
router = APIRouter(tags=["workspace_admin"])
router.include_router(signups_router)

__all__ = ["router"]
