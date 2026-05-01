"""Deployment-admin signup / abuse-signal surface (placeholder).

Mounts ``GET /signups`` under the deployment-admin tree so the
downstream signup-audit feature (cd-ovt4) has a stable URL to fill in.
The final surface lists abuse-relevant signup events the spec
enumerates under §15 "Self-serve abuse mitigations":

* burst-rate trips on ``POST /api/v1/signup/start``;
* same source IP provisioning across distinct email addresses;
* repeat provisioning from one email;
* quota near-breach events on unverified workspaces.

Today every signup row lives in ``audit_log`` (§15 "Audit log") —
the operator-only ``/admin/signups`` page is the projection over
that log across the deployment.
Because cd-g1ay is the scaffold step, this router returns the
canonical §12 collection envelope with an empty ``data`` list and
``has_more = false`` — a forward-compatible shape that cd-ovt4 will
fill without a response-schema bump.

Authorisation: deployment-admin principal, consistent with every
``/admin/api/v1/*`` route. Workspace owners and managers do not see
this surface; post-workspace quota signals are still deployment
signals because they are part of the signup abuse programme.

**OpenAPI / CLI naming.** The operation uses the deployment-admin
``admin`` tag and ``admin.signups.list`` operation id, matching the
rest of :mod:`app.api.admin`.

See ``docs/specs/15-security-privacy.md`` §"Self-serve abuse
mitigations", ``docs/specs/12-rest-api.md`` §"Pagination",
``docs/specs/13-cli.md`` §"crewday admin vs crewday deploy".
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.admin.deps import current_deployment_admin_principal
from app.tenancy import DeploymentContext

__all__ = [
    "SignupAuditEntry",
    "SignupAuditKind",
    "SignupsListResponse",
    "build_admin_signups_router",
]


_AdminCtx = Annotated[DeploymentContext, Depends(current_deployment_admin_principal)]


# Spec §12 "Pagination": ``limit`` defaults to 50 and caps at 500. We
# mirror those bounds verbatim so once the real query lands the knob
# already behaves the way the rest of v1 does.
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500

SignupAuditKind = Literal[
    "burst_rate",
    "distinct_emails_one_ip",
    "repeat_email",
    "quota_near_breach",
]


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


def build_admin_signups_router() -> APIRouter:
    """Return the router carrying the deployment signup-abuse feed."""
    router = APIRouter(tags=["admin"])

    @router.get(
        "/signups",
        response_model=SignupsListResponse,
        operation_id="admin.signups.list",
        summary="List deployment signup abuse signals",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "signups-list",
                "summary": "List deployment signup abuse signals",
                "mutates": False,
            },
        },
    )
    def get_signups(
        _ctx: _AdminCtx,
        kind: Annotated[SignupAuditKind | None, Query()] = None,
        cursor: Annotated[str | None, Query(max_length=256)] = None,
        limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    ) -> SignupsListResponse:
        """Return the deployment signup abuse-signal feed (placeholder).

        The real implementation (cd-ovt4) will read ``audit_log`` rows
        whose ``action`` prefix matches the signup event catalogue,
        apply the cursor, and project each row into
        :class:`SignupAuditEntry`. Until then the endpoint returns an
        empty page so the deployment-admin SPA can wire against the
        live URL and the admin surface gate is exercised in CI.
        """
        _ = kind, cursor, limit
        return SignupsListResponse(data=[], next_cursor=None, has_more=False)

    return router
