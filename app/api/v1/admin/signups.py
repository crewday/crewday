"""Workspace-admin signup / abuse-signal surface (placeholder).

Mounts ``GET /admin/signups`` under the workspace tree so the
downstream signup-audit feature (cd-ovt4) has a stable URL to fill in.
The final surface lists abuse-relevant signup events the spec
enumerates under §15 "Self-serve abuse mitigations":

* burst-rate trips on ``POST /api/v1/signup/start``;
* same source IP provisioning across distinct email addresses;
* repeat provisioning from one email;
* quota near-breach events on unverified workspaces.

Today every signup row lives in ``audit_log`` (§15 "Audit log") —
the operator-only ``/admin/signups`` page is the projection over
that log, narrowed to the workspace via the tenancy middleware.
Because cd-g1ay is the scaffold step, this router returns the
canonical §12 collection envelope with an empty ``data`` list and
``has_more = false`` — a forward-compatible shape that cd-ovt4 will
fill without a response-schema bump.

Authorisation: ``audit_log.view`` on ``scope_kind='workspace'``
(§05 action catalog). Default-allow is ``owners, managers`` with
``root_protected_deny=True``, which matches the task's
"owner/manager only" requirement verbatim. A dedicated
``admin.view`` bit may land later (cd-z5rd); picking the closest
existing key today keeps the permission-rule wire format stable
so rules written against the placeholder keep working through the
rename.

**OpenAPI / CLI naming.** The URL segment is ``/admin/signups``
(spec §15 names that URL verbatim) but the ``operation_id``,
OpenAPI tag, and ``x-cli.group`` use ``workspace_admin`` /
``workspace-admin`` to avoid colliding with the reserved
host-CLI-only ``crewday admin`` group (§13) and the
deployment-admin tree (:mod:`app.api.admin`). See
:mod:`app.api.v1.admin` module docstring for the full rationale.

See ``docs/specs/15-security-privacy.md`` §"Self-serve abuse
mitigations", ``docs/specs/12-rest-api.md`` §"Pagination",
``docs/specs/13-cli.md`` §"crewday admin vs crewday deploy".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import current_workspace_context
from app.authz import Permission
from app.tenancy import WorkspaceContext

__all__ = [
    "SignupAuditEntry",
    "SignupsListResponse",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]


# Spec §12 "Pagination": ``limit`` defaults to 50 and caps at 500. We
# mirror those bounds verbatim so once the real query lands the knob
# already behaves the way the rest of v1 does.
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500


class SignupAuditEntry(BaseModel):
    """One abuse-signal row surfaced on ``/admin/signups``.

    Mirrors the §15 catalogue of signup events: each row carries
    the ``kind`` (``burst_rate`` / ``distinct_emails_one_ip`` /
    ``repeat_email`` / ``quota_near_breach`` etc.), an opaque
    ``event_id`` for the audit row, an ``occurred_at`` timestamp,
    a hashed ``ip_hash`` (no plaintext IPs per §15 "Logging and
    redaction") and a free-form ``detail`` bag the UI projects
    without interpretation.

    The model is defined today — with no emitting call site — so
    cd-ovt4 has a stable wire contract to populate. Every field is
    optional on the placeholder shape because no real row exists;
    cd-ovt4 will tighten the Pydantic model as the emitter lands.
    """

    event_id: str
    kind: str
    occurred_at: str
    ip_hash: str | None = None
    email_hash: str | None = None
    detail: dict[str, object] = Field(default_factory=dict)


class SignupsListResponse(BaseModel):
    """Collection envelope for ``GET /admin/signups``.

    Shape matches the §12 "Pagination" canonical envelope —
    ``{data, next_cursor, has_more}`` — so the downstream task that
    wires the real query against ``audit_log`` doesn't have to
    re-shape the response. Returning the envelope shape today also
    means the SPA can start rendering the page (empty state, loading
    skeleton, filter chrome) before cd-ovt4 lands.
    """

    data: list[SignupAuditEntry]
    next_cursor: str | None = None
    has_more: bool = False


# No explicit ``tags`` on this sub-router — the aggregator in
# :mod:`app.api.v1.admin` already tags every child route with
# ``workspace_admin``. Re-declaring the tag here would emit the route
# under ``["workspace_admin", "workspace_admin"]`` in the OpenAPI schema,
# a cosmetic bug the :func:`app.api.factory._build_custom_openapi`
# generator cannot dedupe (it collapses tag *definitions*, not
# per-operation tag lists).
router = APIRouter()


@router.get(
    "/signups",
    response_model=SignupsListResponse,
    operation_id="workspace_admin.signups.list",
    summary="List workspace signup abuse signals",
    dependencies=[Depends(Permission("audit_log.view", scope_kind="workspace"))],
    openapi_extra={
        # §13 CLI surface. ``crewday admin`` is the HOST-CLI-only group
        # (no HTTP) and ``crewday deploy`` is the deployment HTTP admin;
        # this workspace-scoped HTTP admin surface needs its own CLI
        # group that collides with neither reserved name, so we use
        # ``workspace-admin``. ``mutates=False`` is informational: this
        # is a read path and §11's per-user agent approval mode treats
        # it accordingly (silent execution in chat).
        "x-cli": {
            "group": "workspace-admin",
            "verb": "signups-list",
            "summary": "List workspace signup abuse signals",
            "mutates": False,
        },
    },
)
def get_signups(
    ctx: _Ctx,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
) -> SignupsListResponse:
    """Return the workspace's signup abuse-signal feed (placeholder).

    The real implementation (cd-ovt4) will read ``audit_log`` rows
    whose ``action`` prefix matches the signup event catalogue,
    narrow to ``ctx.workspace_id`` (tenancy middleware already bound
    the context), apply the cursor, and project each row into
    :class:`SignupAuditEntry`. Until then the endpoint returns an
    empty page so the SPA can wire against the live URL and the
    permission gate is exercised in CI.

    ``cursor`` + ``limit`` are accepted on the query string today so
    clients (SPA, CLI, agent) can start threading them through —
    validation kicks in on bad values, and the empty response is
    correct for every input.
    """
    # Explicitly discard the parameters so tools reading the module
    # don't flag them as unused; once cd-ovt4 wires the query they
    # drive the cursor walk. ``ctx`` is likewise unused today because
    # the empty page is workspace-agnostic — the tenancy middleware
    # has already done the authorisation scoping by the time we run.
    _ = ctx, cursor, limit
    return SignupsListResponse(data=[], next_cursor=None, has_more=False)
