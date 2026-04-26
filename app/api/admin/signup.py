"""Deployment-admin self-serve signup settings.

Mounts under ``/admin/api/v1`` (§12 "Admin surface"):

* ``GET /signup/settings`` — current ``signup_enabled`` flag,
  per-throttle overrides, and the deployment's
  disposable-domain list path.
* ``PUT /signup/settings`` — patch any of the above keys.

The values are stored on the ``deployment_setting`` table (§02
"Conventions" §"Deployment-wide settings"); the capability
registry (:mod:`app.capabilities`) is the runtime consumer. The
PUT route writes the row and refreshes the cached registry on
:attr:`app.state.capabilities` so the new value takes effect for
the next signup attempt without a restart.

See ``docs/specs/12-rest-api.md`` §"Admin surface",
``docs/specs/15-security-privacy.md`` §"Self-serve abuse
mitigations" and ``docs/specs/01-architecture.md`` §"Capability
registry".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.adapters.db.capabilities.models import DeploymentSetting
from app.api.admin._audit import audit_admin
from app.api.admin.deps import current_deployment_admin_principal
from app.api.deps import db_session
from app.capabilities import Capabilities
from app.tenancy import DeploymentContext, tenant_agnostic

__all__ = [
    "SignupSettingsPayload",
    "SignupSettingsResponse",
    "build_admin_signup_router",
]


_Db = Annotated[Session, Depends(db_session)]


# ``deployment_setting`` keys this router owns. Centralising the
# names means the GET projection, the PUT validator, and the
# audit-row diff all reference the same string literals — a typo
# breaks at import, not at runtime.
_KEY_SIGNUP_ENABLED: Final[str] = "signup_enabled"
_KEY_THROTTLE_OVERRIDES: Final[str] = "signup_throttle_overrides"
_KEY_DISPOSABLE_DOMAINS_PATH: Final[str] = "signup_disposable_domains_path"


# Default value for ``signup_disposable_domains_path`` when no
# override row exists. Matches :mod:`app.abuse.data.disposable_domains`'s
# bundled file location — the curated list shipped with the
# binary; an operator pointing at a custom file (overridable
# disposable list) writes a new ``deployment_setting`` row.
_DEFAULT_DISPOSABLE_DOMAINS_PATH: Final[str] = "app/abuse/data/disposable_domains.txt"


class SignupSettingsResponse(BaseModel):
    """Body of ``GET /admin/api/v1/signup/settings``.

    Mirrors the SPA's :interface:`AdminSignupSettings` shape from
    ``mocks/web/src/types/api.ts``. Today's response collapses
    every optional throttle key into the
    ``signup_throttle_overrides`` map — the SPA's signup page
    renders the map verbatim. The wire shape stays narrow (one
    field per setting) so the cd-jlms slice does not commit to
    a richer typed throttle envelope before §15's pre-verified
    upload caps land their own routes.
    """

    signup_enabled: bool
    signup_throttle_overrides: dict[str, int]
    signup_disposable_domains_path: str


class SignupSettingsPayload(BaseModel):
    """Request body of ``PUT /admin/api/v1/signup/settings``.

    Every field is optional — the SPA can patch one knob without
    re-sending the others. Pydantic v2's
    :class:`Field(default=None)` keeps the JSON spec from
    requiring a present-but-null value; a payload that omits a
    field leaves the corresponding ``deployment_setting`` row
    untouched.
    """

    signup_enabled: bool | None = Field(default=None)
    signup_throttle_overrides: dict[str, int] | None = Field(default=None)
    signup_disposable_domains_path: str | None = Field(default=None, max_length=512)


def _setting(session: Session, *, key: str) -> DeploymentSetting | None:
    """Tenant-agnostic ``session.get`` for a :class:`DeploymentSetting` row."""
    with tenant_agnostic():
        return session.get(DeploymentSetting, key)


def _read_setting(session: Session, *, key: str, default: Any) -> Any:
    """Resolve a single ``deployment_setting`` value with a default."""
    row = _setting(session, key=key)
    if row is None:
        return default
    return row.value


def _write_setting(
    session: Session,
    *,
    key: str,
    value: Any,
    actor_user_id: str,
    now: datetime,
) -> Any:
    """Upsert a single :class:`DeploymentSetting` row.

    Returns the previous value (or the ``default``-equivalent
    sentinel when the row didn't exist before) so the audit-row
    diff can capture the before / after pair without a second
    SELECT. ``actor_user_id`` is stamped on
    :attr:`DeploymentSetting.updated_by` for the operator-history
    column the admin SPA renders alongside the value.
    """
    row = _setting(session, key=key)
    previous: Any
    if row is None:
        previous = None
        with tenant_agnostic():
            session.add(
                DeploymentSetting(
                    key=key, value=value, updated_at=now, updated_by=actor_user_id
                )
            )
    else:
        previous = row.value
        row.value = value
        row.updated_at = now
        row.updated_by = actor_user_id
    return previous


def _normalise_throttle_overrides(value: Any) -> dict[str, int]:
    """Coerce a stored throttle blob into the wire shape.

    The setting's stored shape is a flat ``{name: int}`` mapping;
    defensive normalisation handles a half-shaped row that
    somehow landed a non-int value. Unknown shapes collapse to
    an empty dict — same fail-soft posture
    :class:`Capabilities.refresh_settings` takes.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for name, count in value.items():
        if (
            isinstance(name, str)
            and isinstance(count, int)
            and not isinstance(count, bool)
        ):
            out[name] = count
    return out


def _refresh_capabilities(request: Request, session: Session) -> None:
    """Refresh :attr:`app.state.capabilities` after a settings write.

    The capability registry caches the mutable subset on app
    state so the per-request hot path reads it without a DB
    round-trip. After a PUT mutation we re-read the rows so the
    in-memory cache matches the DB; if the app factory hasn't
    seeded the cache (unit-test path), we silently skip — the
    next ``probe`` call will hydrate it from the freshly-written
    rows.
    """
    capabilities: Capabilities | None = getattr(request.app.state, "capabilities", None)
    if capabilities is None:
        return
    capabilities.refresh_settings(session)


def build_admin_signup_router() -> APIRouter:
    """Return the router carrying the signup-settings admin routes."""
    router = APIRouter(tags=["admin"])

    @router.get(
        "/signup/settings",
        response_model=SignupSettingsResponse,
        operation_id="admin.signup.settings.read",
        summary="Read self-serve signup settings",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "signup-settings",
                "summary": "Read self-serve signup settings",
                "mutates": False,
            },
        },
    )
    def read_signup_settings(
        _ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
    ) -> SignupSettingsResponse:
        """Return the current signup settings projection.

        Reads the three signup-namespaced keys directly off
        ``deployment_setting``. Falls back to the
        :class:`DeploymentSettings` defaults so a never-written
        deployment surfaces the factory values rather than
        ``None`` / ``[]``.
        """
        return SignupSettingsResponse(
            signup_enabled=bool(
                _read_setting(session, key=_KEY_SIGNUP_ENABLED, default=True)
            ),
            signup_throttle_overrides=_normalise_throttle_overrides(
                _read_setting(session, key=_KEY_THROTTLE_OVERRIDES, default={})
            ),
            signup_disposable_domains_path=str(
                _read_setting(
                    session,
                    key=_KEY_DISPOSABLE_DOMAINS_PATH,
                    default=_DEFAULT_DISPOSABLE_DOMAINS_PATH,
                )
            ),
        )

    @router.put(
        "/signup/settings",
        response_model=SignupSettingsResponse,
        operation_id="admin.signup.settings.update",
        summary="Update self-serve signup settings",
        status_code=status.HTTP_200_OK,
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "signup-settings-update",
                "summary": "Update self-serve signup settings",
                "mutates": True,
            },
        },
    )
    def update_signup_settings(
        payload: SignupSettingsPayload,
        ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        request: Request,
    ) -> SignupSettingsResponse:
        """Patch any combination of signup settings.

        Only the fields present in ``payload`` are written; absent
        fields leave their existing rows untouched. The audit row
        carries a ``diff`` whose keys are exactly the fields that
        changed (a present-but-equal field still re-writes the row
        because :attr:`DeploymentSetting.updated_at` advances —
        the diff omits the equal-value entries so the audit feed
        stays signal-rich).

        After the writes land, :func:`_refresh_capabilities`
        re-reads the rows into :attr:`app.state.capabilities` so
        the next signup attempt sees the new value without a
        process restart.
        """
        now = datetime.now(UTC)
        diff: dict[str, dict[str, Any]] = {}
        with tenant_agnostic():
            if payload.signup_enabled is not None:
                previous = _write_setting(
                    session,
                    key=_KEY_SIGNUP_ENABLED,
                    value=payload.signup_enabled,
                    actor_user_id=ctx.user_id,
                    now=now,
                )
                if previous != payload.signup_enabled:
                    diff[_KEY_SIGNUP_ENABLED] = {
                        "before": previous,
                        "after": payload.signup_enabled,
                    }
            if payload.signup_throttle_overrides is not None:
                normalised = _normalise_throttle_overrides(
                    payload.signup_throttle_overrides
                )
                previous_throttle = _normalise_throttle_overrides(
                    _read_setting(session, key=_KEY_THROTTLE_OVERRIDES, default={})
                )
                _write_setting(
                    session,
                    key=_KEY_THROTTLE_OVERRIDES,
                    value=normalised,
                    actor_user_id=ctx.user_id,
                    now=now,
                )
                if previous_throttle != normalised:
                    diff[_KEY_THROTTLE_OVERRIDES] = {
                        "before": previous_throttle,
                        "after": normalised,
                    }
            if payload.signup_disposable_domains_path is not None:
                previous_path = _read_setting(
                    session,
                    key=_KEY_DISPOSABLE_DOMAINS_PATH,
                    default=_DEFAULT_DISPOSABLE_DOMAINS_PATH,
                )
                _write_setting(
                    session,
                    key=_KEY_DISPOSABLE_DOMAINS_PATH,
                    value=payload.signup_disposable_domains_path,
                    actor_user_id=ctx.user_id,
                    now=now,
                )
                if previous_path != payload.signup_disposable_domains_path:
                    diff[_KEY_DISPOSABLE_DOMAINS_PATH] = {
                        "before": previous_path,
                        "after": payload.signup_disposable_domains_path,
                    }
            if diff:
                audit_admin(
                    session,
                    ctx=ctx,
                    request=request,
                    entity_kind="deployment_setting",
                    # ``signup`` is a synthetic entity id covering the
                    # three signup-namespaced rows the route mutates as
                    # a single envelope. Spec §02 "audit_log" pairs
                    # ``entity_kind`` + ``entity_id`` for retrieval; the
                    # synthetic id keeps the audit-feed filter for
                    # "every signup-settings change" simple.
                    entity_id="signup",
                    action="signup_settings.updated",
                    diff=diff,
                )
            session.flush()
        _refresh_capabilities(request, session)
        return SignupSettingsResponse(
            signup_enabled=bool(
                _read_setting(session, key=_KEY_SIGNUP_ENABLED, default=True)
            ),
            signup_throttle_overrides=_normalise_throttle_overrides(
                _read_setting(session, key=_KEY_THROTTLE_OVERRIDES, default={})
            ),
            signup_disposable_domains_path=str(
                _read_setting(
                    session,
                    key=_KEY_DISPOSABLE_DOMAINS_PATH,
                    default=_DEFAULT_DISPOSABLE_DOMAINS_PATH,
                )
            ),
        )

    return router
