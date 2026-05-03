"""Deployment-admin settings + capability-registry routes.

Mounts under ``/admin/api/v1`` (§12 "Admin surface"):

* ``GET /settings`` — every ``deployment_setting`` row, resolved
  against the capability-registry default for the same key.
* ``PUT /settings/{key}`` — write a single setting. Owners-only;
  root-only keys (e.g. ``trusted_interfaces``) refuse with the
  canonical typed error envelope.

The route validates the ``key`` against the registry (only known
knobs are writable) and the ``value`` against the registry's
declared coercion. Unknown keys 422 ``unknown_setting``;
root-only keys 422 ``root_only_setting`` so the operator sees
the typed code instead of a silent 404 wall (the existence of
the root-only key is a documented spec invariant — the refusal
path doesn't enumerate tenant data).

The signup-namespaced keys are also writable here as a
super-set; the dedicated :mod:`app.api.admin.signup` router
provides a friendlier batch surface, and both paths converge on
the same ``deployment_setting`` rows.

See ``docs/specs/12-rest-api.md`` §"Admin surface",
``docs/specs/01-architecture.md`` §"Capability registry" and
``docs/specs/15-security-privacy.md`` §"Trusted interfaces".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.capabilities.models import DeploymentSetting
from app.adapters.db.secrets.repositories import SqlAlchemySecretEnvelopeRepository
from app.adapters.llm.openrouter import (
    OPENROUTER_API_KEY_PURPOSE,
    OPENROUTER_API_KEY_SETTING,
    openrouter_api_key_display_stub,
    openrouter_envelope_id_from_pointer,
)
from app.adapters.mail.smtp_config import (
    SMTP_BOUNCE_DOMAIN_SETTING,
    SMTP_FROM_SETTING,
    SMTP_HOST_SETTING,
    SMTP_PASSWORD_PURPOSE,
    SMTP_PASSWORD_SETTING,
    SMTP_PORT_SETTING,
    SMTP_TIMEOUT_SETTING,
    SMTP_USE_TLS_SETTING,
    SMTP_USER_SETTING,
    smtp_envelope_id_from_pointer,
    smtp_password_display_stub,
)
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.adapters.storage.ports import EnvelopeOwner
from app.api.admin._audit import audit_admin
from app.api.admin._owners import ensure_deployment_owner
from app.api.admin.deps import current_deployment_admin_principal
from app.api.deps import db_session
from app.api.transport import admin_sse
from app.capabilities import Capabilities, DeploymentSettings
from app.config import Settings
from app.tenancy import DeploymentContext, tenant_agnostic

__all__ = [
    "DeploymentSettingPayload",
    "DeploymentSettingResponse",
    "DeploymentSettingsResponse",
    "build_admin_settings_router",
]


_Db = Annotated[Session, Depends(db_session)]


# Canonical typed error codes the route surfaces. Each one maps
# to a 422 envelope with ``error = <code>``; the SPA gates the
# Save button off the code so the operator sees a friendly
# inline message instead of a generic "validation failed".
_ERROR_UNKNOWN_KEY: Final[str] = "unknown_setting"
_ERROR_ROOT_ONLY: Final[str] = "root_only_setting"
_ERROR_OWNER_REQUIRED: Final[str] = "owner_required"
_ERROR_VALUE_TYPE: Final[str] = "invalid_setting_value"


@dataclass(frozen=True, slots=True)
class _SettingDef:
    """Static description of one writable deployment_setting key.

    Mirrors the spec's :interface:`AdminDeploymentSetting` shape
    (``mocks/web/src/types/api.ts``):

    * ``key`` — the row PK; matches the corresponding field on
      :class:`DeploymentSettings`.
    * ``kind`` — one of ``bool|int|string|secret`` for the SPA's input
      widget. Mirrors §02 setting-catalog conventions.
    * ``description`` — short operator-facing label.
    * ``coerce`` — callable that narrows the request body's free-form
      JSON value into the storage shape. Raises :class:`ValueError`
      for a bad shape; the route translates that into the canonical
      ``invalid_setting_value`` 422 envelope.
    * ``default`` — factory default used when no row exists. Mirrors
      :class:`DeploymentSettings`'s field default so the GET feed
      always carries a value.
    * ``root_only`` — owners-only writes refuse from a non-owner
      caller with ``root_only_setting`` 422. ``trusted_interfaces``
      is the canonical example named in spec §12.
    """

    key: str
    kind: str
    description: str
    coerce: Callable[[Any], Any]
    default: Any
    root_only: bool = False


def _coerce_bool(value: Any) -> bool:
    """Coerce a JSON value into a strict bool.

    Pydantic's :class:`Field(strict=True)` would do this for a
    typed model — but the PUT body lands as free-form ``Any`` so
    the router can validate the same value against many setting
    types. ``True`` / ``False`` pass through; every other shape
    (string, int, list, dict, ``None``) raises so we never
    silently elevate a truthy value the operator did not type.
    """
    if isinstance(value, bool):
        return value
    raise ValueError("expected a JSON boolean")


def _coerce_int(value: Any) -> int:
    """Coerce a JSON value into a non-negative int.

    ``0`` is allowed (``llm_default_budget_cents_30d=0`` is the
    "hard-disable LLMs" knob; see :func:`app.domain.plans.tight_cap_cents`).
    Negative values are rejected — every int knob in v1 measures
    a non-negative quantity (cents, seconds, counts).
    ``isinstance(value, bool)`` excludes ``True`` / ``False``,
    which Python's :func:`int` would happily promote to 1 / 0.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ValueError("expected a non-negative JSON integer")


def _coerce_positive_int(value: Any) -> int:
    number = _coerce_int(value)
    if number > 0:
        return number
    raise ValueError("expected a positive JSON integer")


def _coerce_tcp_port(value: Any) -> int:
    number = _coerce_int(value)
    if 1 <= number <= 65535:
        return number
    raise ValueError("expected a TCP port from 1 to 65535")


def _coerce_marketplace_fee_bps(value: Any) -> int:
    number = _coerce_int(value)
    if 0 <= number <= 10000:
        return number
    raise ValueError("expected basis points from 0 to 10000")


def _coerce_platform_fee_currency_policy(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a string")
    if value == "match_source":
        return value
    if (
        value.startswith("fixed_")
        and len(value) == len("fixed_USD")
        and value.removeprefix("fixed_").isalpha()
        and value.removeprefix("fixed_").isupper()
    ):
        return value
    raise ValueError("expected 'match_source' or fixed_<ISO>")


def _coerce_str_int_dict(value: Any) -> dict[str, int]:
    """Coerce a JSON value into a ``{str: int}`` mapping.

    Used for ``signup_throttle_overrides``. Keys must be strings
    and values must be non-negative ints; anything else raises.
    """
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    out: dict[str, int] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise ValueError("override keys must be strings")
        if (
            not isinstance(raw_value, int)
            or isinstance(raw_value, bool)
            or raw_value < 0
        ):
            raise ValueError("override values must be non-negative integers")
        out[raw_key] = raw_value
    return out


def _coerce_secret_str(value: Any) -> str:
    """Coerce a JSON value into a non-blank secret string."""
    if isinstance(value, str) and value.strip():
        return value
    raise ValueError("expected a non-blank JSON string")


# ``DeploymentSettings`` is a slotted dataclass — class-level
# field access reads an attribute descriptor, not the default
# value. Pull the defaults out of the dataclass field metadata
# so the registry stays a single source of truth without
# tripping mypy's ``__slots__`` conflict diagnostic.
_DEPLOYMENT_DEFAULTS: Final[dict[str, Any]] = {
    field.name: field.default for field in fields(DeploymentSettings)
}


# Registry of writable setting definitions. Mirrors the
# :class:`DeploymentSettings` field set so a new operator-mutable
# knob lights up by appending one entry here. Registry-only secret
# pointers that are not capability flags also live here so the admin
# route remains the single deployment-setting writer.
_REGISTRY: Final[tuple[_SettingDef, ...]] = (
    _SettingDef(
        key="signup_enabled",
        kind="bool",
        description="Master switch for self-serve signup.",
        coerce=_coerce_bool,
        default=_DEPLOYMENT_DEFAULTS["signup_enabled"],
    ),
    _SettingDef(
        key="signup_throttle_overrides",
        kind="json",
        description="Override the per-IP / per-email signup throttles.",
        coerce=_coerce_str_int_dict,
        # ``signup_throttle_overrides`` uses ``field(default_factory=dict)``
        # so the dataclass field's ``.default`` is the MISSING sentinel —
        # spell the empty dict explicitly here for the registry default.
        default={},
    ),
    _SettingDef(
        key="require_passkey_attestation",
        kind="bool",
        description=(
            "Require passkey attestation during registration; off by default "
            "to let consumer authenticators land freely."
        ),
        coerce=_coerce_bool,
        default=_DEPLOYMENT_DEFAULTS["require_passkey_attestation"],
    ),
    _SettingDef(
        key="llm_default_budget_cents_30d",
        kind="int",
        description="Default rolling 30-day LLM spend cap per workspace, in cents.",
        coerce=_coerce_int,
        default=_DEPLOYMENT_DEFAULTS["llm_default_budget_cents_30d"],
    ),
    _SettingDef(
        key="captcha_required",
        kind="bool",
        description="Require Turnstile CAPTCHA on the self-serve signup form.",
        coerce=_coerce_bool,
        default=_DEPLOYMENT_DEFAULTS["captcha_required"],
    ),
    _SettingDef(
        key="marketplace_enabled",
        kind="bool",
        description="Enable the deferred marketplace discovery surface.",
        coerce=_coerce_bool,
        default=_DEPLOYMENT_DEFAULTS["marketplace_enabled"],
    ),
    _SettingDef(
        key="platform_fee_default_bps",
        kind="int",
        description="Default platform fee basis points snapshotted onto matches.",
        coerce=_coerce_marketplace_fee_bps,
        default=_DEPLOYMENT_DEFAULTS["platform_fee_default_bps"],
    ),
    _SettingDef(
        key="platform_fee_currency_policy",
        kind="string",
        description="Marketplace platform fee currency policy.",
        coerce=_coerce_platform_fee_currency_policy,
        default=_DEPLOYMENT_DEFAULTS["platform_fee_currency_policy"],
    ),
    _SettingDef(
        key=OPENROUTER_API_KEY_SETTING,
        kind="secret",
        description="OpenRouter API key encrypted into secret_envelope.",
        coerce=_coerce_secret_str,
        default={"display_stub": ""},
    ),
    _SettingDef(
        key=SMTP_HOST_SETTING,
        kind="string",
        description="SMTP relay host.",
        coerce=_coerce_secret_str,
        default="",
    ),
    _SettingDef(
        key=SMTP_PORT_SETTING,
        kind="int",
        description="SMTP relay port.",
        coerce=_coerce_tcp_port,
        default=587,
    ),
    _SettingDef(
        key=SMTP_USER_SETTING,
        kind="string",
        description="SMTP login username.",
        coerce=_coerce_secret_str,
        default="",
    ),
    _SettingDef(
        key=SMTP_PASSWORD_SETTING,
        kind="secret",
        description="SMTP password encrypted into secret_envelope.",
        coerce=_coerce_secret_str,
        default={"display_stub": ""},
    ),
    _SettingDef(
        key=SMTP_FROM_SETTING,
        kind="string",
        description="SMTP From address.",
        coerce=_coerce_secret_str,
        default="",
    ),
    _SettingDef(
        key=SMTP_USE_TLS_SETTING,
        kind="bool",
        description="Use TLS for SMTP delivery.",
        coerce=_coerce_bool,
        default=True,
    ),
    _SettingDef(
        key=SMTP_BOUNCE_DOMAIN_SETTING,
        kind="string",
        description="SMTP bounce domain override.",
        coerce=_coerce_secret_str,
        default="",
    ),
    _SettingDef(
        key=SMTP_TIMEOUT_SETTING,
        kind="int",
        description="SMTP socket timeout, in seconds.",
        coerce=_coerce_positive_int,
        default=10,
    ),
    # ``trusted_interfaces`` is the canonical ``root_only`` example
    # (§12 "PUT /settings/{key}"). The actual value is read off
    # :attr:`Settings.trusted_interfaces` at boot — there is no
    # ``deployment_setting`` row for it, and writing one through the
    # admin tree is forbidden. The catalog entry exists so the GET
    # feed can advertise the key (with ``root_only=True``) and the
    # PUT validator returns ``root_only_setting`` instead of
    # ``unknown_setting`` when an operator targets it.
    _SettingDef(
        key="trusted_interfaces",
        kind="json",
        description=(
            "Comma-separated globs of network interfaces the bind-guard "
            "treats as trusted. Configured via CREWDAY_TRUSTED_INTERFACES; "
            "writes through the admin surface refuse."
        ),
        coerce=lambda _: (_ for _ in ()).throw(  # pragma: no cover - unreachable
            ValueError("root-only setting")
        ),
        default=[],
        root_only=True,
    ),
)


_REGISTRY_INDEX: Final[dict[str, _SettingDef]] = {
    entry.key: entry for entry in _REGISTRY
}


class DeploymentSettingResponse(BaseModel):
    """One row of ``GET /admin/api/v1/settings``.

    Mirrors :interface:`AdminDeploymentSetting` in
    ``mocks/web/src/types/api.ts``: the resolved value, its kind,
    operator-facing description, root-only flag, and the
    last-write metadata. ``updated_at`` / ``updated_by`` are
    ``""`` for keys that never had a row (still on factory
    default) so the SPA's table cell can render without a
    nullish-coalesce.
    """

    key: str
    value: Any
    kind: str
    description: str
    root_only: bool
    updated_at: str
    updated_by: str


class DeploymentSettingsResponse(BaseModel):
    """Body of ``GET /admin/api/v1/settings``.

    Returned as ``{settings: [...]}`` for forward compat with a
    later cursor envelope; the cd-jlms slice ships every key in
    one page (the registry is small and bounded).
    """

    settings: list[DeploymentSettingResponse]


class DeploymentSettingPayload(BaseModel):
    """Request body of ``PUT /admin/api/v1/settings/{key}``.

    Only ``value`` lives in the body; the key sits on the URL
    so an operator cannot accidentally retarget the write.
    Pydantic accepts any JSON value here — narrow validation
    happens in the per-key coerce function.
    """

    value: Any = Field(...)


def _existing_row(session: Session, *, key: str) -> DeploymentSetting | None:
    """Tenant-agnostic ``session.get`` for one ``deployment_setting`` row."""
    with tenant_agnostic():
        return session.get(DeploymentSetting, key)


def _format_updated_at(row: DeploymentSetting | None) -> str:
    """ISO-8601 UTC for ``row.updated_at``; ``""`` when no row."""
    if row is None:
        return ""
    moment = row.updated_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat()


def _refresh_capabilities(request: Request, session: Session) -> None:
    """Hot-reload the in-memory capability registry after a write.

    Mirrors :func:`app.api.admin.signup._refresh_capabilities` —
    duplicated rather than imported because the two routers do
    not depend on each other and a shared helper would couple
    them. Promote when a third caller appears.
    """
    capabilities: Capabilities | None = getattr(request.app.state, "capabilities", None)
    if capabilities is None:
        return
    capabilities.refresh_settings(session)


def _resolve_value(definition: _SettingDef, row: DeploymentSetting | None) -> Any:
    """Return the row's stored value, falling back to the default."""
    if definition.key == OPENROUTER_API_KEY_SETTING:
        if row is None:
            return {"display_stub": ""}
        return {"display_stub": openrouter_api_key_display_stub()}
    if definition.key == SMTP_PASSWORD_SETTING:
        if row is None:
            return {"display_stub": ""}
        return {"display_stub": smtp_password_display_stub()}
    if row is None:
        return definition.default
    return row.value


def _audit_value(definition: _SettingDef, value: Any) -> Any:
    """Return a log-safe representation of a stored setting value."""
    if definition.key == OPENROUTER_API_KEY_SETTING:
        if value is None:
            return None
        return {"display_stub": openrouter_api_key_display_stub()}
    if definition.key == SMTP_PASSWORD_SETTING:
        if value is None:
            return None
        return {"display_stub": smtp_password_display_stub()}
    return value


def _settings_from_request(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if isinstance(settings, Settings):
        return settings
    raise RuntimeError("app.state.settings is not configured")


def _stored_value_for_write(
    *,
    definition: _SettingDef,
    value: Any,
    session: Session,
    request: Request,
) -> Any:
    """Return the DB value for a coerced admin setting write."""
    if definition.key not in {OPENROUTER_API_KEY_SETTING, SMTP_PASSWORD_SETTING}:
        return value
    if not isinstance(value, str):  # pragma: no cover - coerce narrows first
        raise ValueError("expected a non-blank JSON string")
    settings = _settings_from_request(request)
    if settings.root_key is None:
        setting_label = (
            "OpenRouter API key"
            if definition.key == OPENROUTER_API_KEY_SETTING
            else "SMTP password"
        )
        raise _problem(
            _ERROR_VALUE_TYPE,
            message=f"CREWDAY_ROOT_KEY is required to store the {setting_label}",
        )
    purpose = (
        OPENROUTER_API_KEY_PURPOSE
        if definition.key == OPENROUTER_API_KEY_SETTING
        else SMTP_PASSWORD_PURPOSE
    )
    envelope = Aes256GcmEnvelope(
        settings.root_key,
        repository=SqlAlchemySecretEnvelopeRepository(session),
    )
    pointer = envelope.encrypt(
        value.encode("utf-8"),
        purpose=purpose,
        owner=EnvelopeOwner(kind="deployment_setting", id=definition.key),
    )
    if definition.key == OPENROUTER_API_KEY_SETTING:
        return openrouter_envelope_id_from_pointer(pointer)
    return smtp_envelope_id_from_pointer(pointer)


def _problem(error: str, *, message: str) -> HTTPException:
    """Build the canonical 422 typed-error envelope.

    The ``detail`` dict lifts ``error`` and ``message`` into the
    top-level problem+json body (see
    :func:`app.api.errors._handle_http_exception`'s dict-detail
    spread), so the SPA reads ``body.error`` directly.
    """
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"error": error, "message": message},
    )


def build_admin_settings_router() -> APIRouter:
    """Return the router carrying the deployment-settings admin routes."""
    router = APIRouter(tags=["admin"])

    @router.get(
        "/settings",
        response_model=DeploymentSettingsResponse,
        operation_id="admin.settings.list",
        summary="List every deployment setting + its current resolved value",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "settings-list",
                "summary": "List deployment settings",
                "mutates": False,
            },
        },
    )
    def list_settings(
        _ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
    ) -> DeploymentSettingsResponse:
        """Return one row per registered key, resolved against defaults.

        Reads every ``deployment_setting`` row in one round-trip,
        joins it against the in-memory :data:`_REGISTRY` so the
        operator sees the same shape (kind, description,
        root-only flag, last-write metadata) for every key —
        whether or not a row exists.
        """
        with tenant_agnostic():
            rows = {
                row.key: row for row in session.scalars(select(DeploymentSetting)).all()
            }
        items: list[DeploymentSettingResponse] = []
        for definition in _REGISTRY:
            row = rows.get(definition.key)
            items.append(
                DeploymentSettingResponse(
                    key=definition.key,
                    value=_resolve_value(definition, row),
                    kind=definition.kind,
                    description=definition.description,
                    root_only=definition.root_only,
                    updated_at=_format_updated_at(row),
                    updated_by=row.updated_by if row and row.updated_by else "",
                )
            )
        return DeploymentSettingsResponse(settings=items)

    @router.put(
        "/settings/{key}",
        response_model=DeploymentSettingResponse,
        operation_id="admin.settings.update",
        summary="Write a single deployment setting",
        status_code=status.HTTP_200_OK,
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "settings-update",
                "summary": "Write a single deployment setting",
                "mutates": True,
            },
        },
    )
    def update_setting(
        key: str,
        payload: DeploymentSettingPayload,
        ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        request: Request,
    ) -> DeploymentSettingResponse:
        """Write one setting; refuse for unknown / root-only keys.

        Validation order matches the spec's principle "do not
        elevate before authorising":

        1. Reject unknown keys with ``unknown_setting`` 422.
        2. Reject root-only keys with ``root_only_setting`` 422
           (the spec's "owners-only; root-only keys refuse"
           wording wraps both the gate and the typed code).
        3. Owner-gate the write via
           :func:`ensure_deployment_owner`; non-owner admins receive
           the same 404 wall as non-admin callers.
        4. Coerce the body's value through the registry's typed
           converter; bad shapes 422 ``invalid_setting_value``.
        5. Upsert the ``deployment_setting`` row, write the
           audit, refresh the in-memory capability registry.
        """
        definition = _REGISTRY_INDEX.get(key)
        if definition is None:
            raise _problem(_ERROR_UNKNOWN_KEY, message=f"unknown setting key: {key!r}")
        if definition.root_only:
            raise _problem(
                _ERROR_ROOT_ONLY,
                message=(
                    f"setting {key!r} is root-only and must be configured "
                    "outside the admin surface"
                ),
            )
        # Every non-root-only setting still requires deployment-owner
        # authority for v1. A later per-key matrix can narrow this
        # gate without changing the wire shape.
        ensure_deployment_owner(session, ctx=ctx)
        try:
            value = definition.coerce(payload.value)
        except ValueError as exc:
            raise _problem(_ERROR_VALUE_TYPE, message=str(exc)) from exc

        now = datetime.now(UTC)
        with tenant_agnostic():
            row = _existing_row(session, key=key)
            previous: Any
            if row is None:
                previous = None
                stored_value = _stored_value_for_write(
                    definition=definition,
                    value=value,
                    session=session,
                    request=request,
                )
                row = DeploymentSetting(
                    key=key,
                    value=stored_value,
                    updated_at=now,
                    updated_by=ctx.user_id,
                )
                session.add(row)
            else:
                previous = row.value
                row.value = _stored_value_for_write(
                    definition=definition,
                    value=value,
                    session=session,
                    request=request,
                )
                row.updated_at = now
                row.updated_by = ctx.user_id
            audit_admin(
                session,
                ctx=ctx,
                request=request,
                entity_kind="deployment_setting",
                entity_id=key,
                action="deployment_setting.updated",
                diff={
                    "value": {
                        "before": _audit_value(definition, previous),
                        "after": _audit_value(definition, row.value),
                    }
                },
            )
            session.flush()
        _refresh_capabilities(request, session)
        admin_sse.publish_admin_event(
            kind="admin.settings.updated",
            ctx=ctx,
            request=request,
            payload={"key": key},
        )
        return DeploymentSettingResponse(
            key=key,
            value=_resolve_value(definition, row),
            kind=definition.kind,
            description=definition.description,
            root_only=definition.root_only,
            updated_at=_format_updated_at(row),
            updated_by=row.updated_by or "",
        )

    return router


# Re-exported for the test suite: pinning the literal here keeps
# the spec-pinned typed codes in one place so the route + the
# tests + the SPA stay in lockstep.
ERROR_UNKNOWN_KEY: Final[str] = _ERROR_UNKNOWN_KEY
ERROR_ROOT_ONLY: Final[str] = _ERROR_ROOT_ONLY
ERROR_OWNER_REQUIRED: Final[str] = _ERROR_OWNER_REQUIRED
ERROR_VALUE_TYPE: Final[str] = _ERROR_VALUE_TYPE
