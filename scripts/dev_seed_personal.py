#!/usr/bin/env python3
"""Dev-only personal seed — rehydrate your user + workspace + passkeys.

Companion to :mod:`scripts.dev_login`. After a SQLite reset, ``apply``
re-creates the rows your physical authenticator needs to log into
``https://dev-app.crew.day`` without re-tapping a fresh registration:

1. Bring the dev stack up and confirm it advertises the current app
   domain:

   ``./scripts/dev-stack-up.sh``

   The normal dev stack sets ``CREWDAY_PUBLIC_URL=https://dev-app.crew.day``
   and ``CREWDAY_WEBAUTHN_RP_ID=dev-app.crew.day``. If the app domain
   changes, credentials captured for the old RP ID cannot be migrated;
   delete or ignore the old seed, re-register on the new domain, then
   capture again.
2. If you are recovering from an RP ID/domain change, reset only the
   disposable dev app database first. Do not apply the old seed before
   re-registering: the old passkey rows are scoped to the retired RP ID,
   and seeding the user/workspace rows can block same-email signup.
   If ``capture`` later says the new workspace slug does not exist, this
   is the first thing to check: the signup likely never completed against
   a clean dev DB.

   ``docker compose -f docker-compose.dev.yml down -v``

   ``./scripts/dev-stack-up.sh``
3. Sign up once at ``https://dev-app.crew.day/signup`` (signup is on by
   default; the login page just doesn't link to it). Register your
   passkey through the normal ceremony — it binds to
   ``rp_id="dev-app.crew.day"``.
4. ``python -m scripts.dev_seed_personal capture --email <e> --workspace <slug>``
   writes :data:`SEED_FILE` (``scripts/dev_seed_personal.json``)
   carrying the user, workspace, and every ``passkey_credential`` row.
   Public material only — the private key never leaves your device.
5. After every DB reset: ``python -m scripts.dev_seed_personal apply``.
   Idempotent. (Re)creates the user, workspace + four system groups +
   owners seat + workspace ``manager`` grant + LLM budget ledger,
   grants deployment admin (``RoleGrant scope_kind='deployment'`` +
   ``DeploymentOwner`` row — full superuser on the bare host), then
   inserts the passkey credential rows verbatim.

Hard-gated like ``dev_login``: ``CREWDAY_DEV_AUTH=1`` +
``CREWDAY_PROFILE=dev`` + sqlite-only.

Captured rows record the live ``sign_count`` for transparency, but
``apply`` writes ``0`` so the first post-seed assertion bypasses the
clone-detection branch in :func:`app.auth.passkey.login_finish`
(``old_sign_count > 0`` gate); subsequent assertions advance
monotonically.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import click
from sqlalchemy import select
from sqlalchemy.orm import Session as SqlaSession

from app.adapters.db.assets.bootstrap import seed_asset_type_catalog
from app.adapters.db.assets.models import (
    Asset,
    AssetAction,
    AssetDocument,
    AssetType,
    FileExtraction,
)
from app.adapters.db.authz.bootstrap import (
    seed_owners_system_group,
    seed_system_permission_groups,
)
from app.adapters.db.authz.models import (
    DeploymentOwner,
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.capabilities.models import DeploymentSetting
from app.adapters.db.identity.models import (
    PasskeyCredential,
    User,
    canonicalise_email,
)
from app.adapters.db.places.models import Area, Property, PropertyWorkspace
from app.adapters.db.session import make_uow
from app.adapters.db.workspace.bootstrap import seed_starter_work_roles
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.auth.signup import FALLBACK_CAP_CENTS
from app.auth.webauthn import base64url_to_bytes, bytes_to_base64url
from app.config import get_settings
from app.domain.llm.budget import new_ledger_row
from app.domain.plans import seed_free_tier_10pct, tight_cap_cents
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import SystemClock
from app.util.tokens import short_token
from app.util.ulid import new_ulid

__all__ = [
    "SEED_FILE",
    "apply_seed",
    "capture_seed",
    "main",
]


_DEV_AUTH_ENV_VAR: Final[str] = "CREWDAY_DEV_AUTH"

# Repo-relative location of the personal seed payload. Sibling of this
# file so a single ``git add scripts/dev_seed_personal.json`` covers
# the whole story.
SEED_FILE: Final[Path] = Path(__file__).resolve().parent / "dev_seed_personal.json"


@dataclass(frozen=True, slots=True)
class _SmokePropertySpec:
    name: str
    kind: str
    city: str
    country: str
    locale: str
    currency: str
    timezone: str
    areas: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SmokeAssetSpec:
    name: str
    property_name: str
    area: str
    type_key: str
    make: str
    model: str
    serial: str
    condition: str
    status: str
    installed_on: date
    purchased_on: date
    price_cents: int
    currency: str
    vendor: str
    warranty: date
    guest_visible: bool
    notes: str
    guest_instructions: str | None = None


@dataclass(frozen=True, slots=True)
class _SmokeDocumentSpec:
    title: str
    filename: str
    kind: str
    asset_name: str
    expires_on: date | None
    amount_cents: int | None
    amount_currency: str | None
    extraction_status: str
    extractor: str | None
    body_text: str | None
    notes: str | None = None


_SMOKE_PROPERTY_SPECS: Final[tuple[_SmokePropertySpec, ...]] = (
    _SmokePropertySpec(
        name="Smoke Villa",
        kind="str",
        city="Nice",
        country="FR",
        locale="fr-FR",
        currency="EUR",
        timezone="Europe/Paris",
        areas=("Kitchen", "Pool plant", "Utility closet", "Garden"),
    ),
    _SmokePropertySpec(
        name="Smoke Apartment",
        kind="vacation",
        city="Dubai",
        country="AE",
        locale="en-AE",
        currency="AED",
        timezone="Asia/Dubai",
        areas=("Kitchen", "Laundry", "Entry"),
    ),
)

_SMOKE_ASSET_SPECS: Final[tuple[_SmokeAssetSpec, ...]] = (
    _SmokeAssetSpec(
        name="Pool pump #2",
        property_name="Smoke Villa",
        area="Pool plant",
        type_key="pool_pump",
        make="Pentair",
        model="SuperFlo VS",
        serial="PP2-SMOKE-001",
        condition="fair",
        status="active",
        installed_on=date(2021, 4, 12),
        purchased_on=date(2021, 3, 30),
        price_cents=148900,
        currency="EUR",
        vendor="Azur Piscine",
        warranty=date(2026, 6, 30),
        guest_visible=False,
        notes="Seed asset for filter, QR-sheet, and maintenance due smoke tests.",
    ),
    _SmokeAssetSpec(
        name="Kitchen refrigerator",
        property_name="Smoke Villa",
        area="Kitchen",
        type_key="refrigerator",
        make="Liebherr",
        model="CNsdc 5703",
        serial="FRIDGE-SMOKE-014",
        condition="good",
        status="active",
        installed_on=date(2023, 2, 18),
        purchased_on=date(2023, 2, 10),
        price_cents=219900,
        currency="EUR",
        vendor="Darty Pro",
        warranty=date(2028, 2, 10),
        guest_visible=True,
        guest_instructions=(
            "If the alarm sounds, close the door firmly and notify the host."
        ),
        notes="Guest-visible equipment card coverage.",
    ),
    _SmokeAssetSpec(
        name="Entry smoke detector",
        property_name="Smoke Apartment",
        area="Entry",
        type_key="smoke_detector",
        make="Nest",
        model="Protect 2nd Gen",
        serial="SD-SMOKE-777",
        condition="new",
        status="active",
        installed_on=date(2025, 11, 6),
        purchased_on=date(2025, 10, 28),
        price_cents=47900,
        currency="AED",
        vendor="ACE",
        warranty=date(2027, 10, 28),
        guest_visible=True,
        guest_instructions="Press the center button only during a false alarm.",
        notes="Safety-category filter coverage.",
    ),
    _SmokeAssetSpec(
        name="Laundry washer",
        property_name="Smoke Apartment",
        area="Laundry",
        type_key="washing_machine",
        make="Bosch",
        model="Series 6",
        serial="WASH-SMOKE-302",
        condition="needs_replacement",
        status="in_repair",
        installed_on=date(2016, 8, 9),
        purchased_on=date(2016, 8, 1),
        price_cents=259900,
        currency="AED",
        vendor="Jumbo",
        warranty=date(2019, 8, 1),
        guest_visible=False,
        notes="Repair-state and replacement-state row coverage.",
    ),
    _SmokeAssetSpec(
        name="Garden generator",
        property_name="Smoke Villa",
        area="Garden",
        type_key="generator",
        make="Honda",
        model="EU70is",
        serial="GEN-SMOKE-088",
        condition="poor",
        status="decommissioned",
        installed_on=date(2014, 5, 20),
        purchased_on=date(2014, 5, 5),
        price_cents=399900,
        currency="EUR",
        vendor="Pro Tools Riviera",
        warranty=date(2017, 5, 5),
        guest_visible=False,
        notes="Archived-ish status coverage without soft-deleting the row.",
    ),
)

_SMOKE_DOCUMENT_SPECS: Final[tuple[_SmokeDocumentSpec, ...]] = (
    _SmokeDocumentSpec(
        title="Pool pump warranty",
        filename="pool-pump-warranty-smoke.pdf",
        kind="warranty",
        asset_name="Pool pump #2",
        expires_on=date(2026, 6, 30),
        amount_cents=148900,
        amount_currency="EUR",
        extraction_status="succeeded",
        extractor="pdf",
        body_text=(
            "Warranty certificate for Smoke Villa pool pump #2. Covers motor "
            "and controller faults through 2026-06-30."
        ),
        notes="Shows an expiring warranty document.",
    ),
    _SmokeDocumentSpec(
        title="Refrigerator manual",
        filename="liebherr-refrigerator-manual.txt",
        kind="manual",
        asset_name="Kitchen refrigerator",
        expires_on=None,
        amount_cents=None,
        amount_currency=None,
        extraction_status="succeeded",
        extractor="passthrough",
        body_text=(
            "Quick reference: hold alarm for three seconds, clean coils every "
            "six months, and check door seals after each turnover."
        ),
    ),
    _SmokeDocumentSpec(
        title="Smoke detector certificate",
        filename="entry-smoke-detector-certificate.pdf",
        kind="certificate",
        asset_name="Entry smoke detector",
        expires_on=date(2027, 10, 28),
        amount_cents=47900,
        amount_currency="AED",
        extraction_status="empty",
        extractor="pdf",
        body_text="",
    ),
    _SmokeDocumentSpec(
        title="Washer repair invoice",
        filename="washer-repair-invoice-smoke.pdf",
        kind="invoice",
        asset_name="Laundry washer",
        expires_on=None,
        amount_cents=62500,
        amount_currency="AED",
        extraction_status="failed",
        extractor="pdf",
        body_text=None,
        notes="Failure-state document for retry UI coverage.",
    ),
)


# ---------------------------------------------------------------------------
# Gate checks — copy of dev_login._check_gates so each script reads as a
# self-contained dev affordance.
# ---------------------------------------------------------------------------


class _GateError(RuntimeError):
    """Refused — one of the hard gates failed."""


def _check_gates() -> None:
    raw = os.environ.get(_DEV_AUTH_ENV_VAR, "0").lower()
    if raw not in {"1", "yes", "true"}:
        raise _GateError(
            f"{_DEV_AUTH_ENV_VAR} is not set to 1/yes/true (got {raw!r}); "
            "dev-seed-personal is hard-gated off."
        )
    settings = get_settings()
    if settings.profile != "dev":
        raise _GateError(
            f"CREWDAY_PROFILE={settings.profile!r} — dev-seed-personal "
            "requires profile=dev."
        )
    scheme = settings.database_url.split(":", 1)[0].lower()
    if not scheme.startswith("sqlite"):
        raise _GateError(
            f"database_url scheme {scheme!r} is not SQLite; "
            "dev-seed-personal refuses to mint rows against a non-SQLite DB."
        )


def _fail_gate(exc: _GateError) -> int:
    print(f"error: dev-seed-personal refused to run: {exc}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Row helpers — idempotent lookups (mirror dev_login.py shape).
# ---------------------------------------------------------------------------


def _find_user(session: SqlaSession, email_lower: str) -> User | None:
    with tenant_agnostic():
        return session.scalars(
            select(User).where(User.email_lower == email_lower)
        ).one_or_none()


def _find_workspace(session: SqlaSession, slug: str) -> Workspace | None:
    with tenant_agnostic():
        return session.scalars(
            select(Workspace).where(Workspace.slug == slug)
        ).one_or_none()


def _ensure_user_workspace(
    session: SqlaSession, *, user_id: str, workspace_id: str, now: datetime
) -> None:
    with tenant_agnostic():
        existing = session.scalars(
            select(UserWorkspace)
            .where(UserWorkspace.user_id == user_id)
            .where(UserWorkspace.workspace_id == workspace_id)
        ).one_or_none()
        if existing is not None:
            return
        session.add(
            UserWorkspace(
                user_id=user_id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=now,
            )
        )
        session.flush()


def _ensure_role_grant(
    session: SqlaSession,
    *,
    user_id: str,
    workspace_id: str,
    grant_role: str,
    now: datetime,
) -> None:
    with tenant_agnostic():
        existing = session.scalars(
            select(RoleGrant)
            .where(RoleGrant.user_id == user_id)
            .where(RoleGrant.workspace_id == workspace_id)
            .where(RoleGrant.grant_role == grant_role)
            .where(RoleGrant.scope_property_id.is_(None))
        ).one_or_none()
        if existing is not None:
            return
        session.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user_id,
                grant_role=grant_role,
                scope_property_id=None,
                created_at=now,
                created_by_user_id=None,
            )
        )
        session.flush()


def _upsert_deployment_setting(
    session: SqlaSession,
    *,
    key: str,
    value: Any,
    now: datetime,
) -> None:
    """Set ``deployment_setting[key] = value`` (insert or overwrite)."""
    with tenant_agnostic():
        row = session.get(DeploymentSetting, key)
        if row is None:
            session.add(
                DeploymentSetting(
                    key=key,
                    value=value,
                    updated_at=now,
                    updated_by="dev_seed_personal",
                )
            )
        else:
            row.value = value
            row.updated_at = now
            row.updated_by = "dev_seed_personal"
        session.flush()


def _ensure_deployment_admin(
    session: SqlaSession,
    *,
    user_id: str,
    now: datetime,
) -> None:
    """Grant full deployment-level admin: ``RoleGrant`` + ``DeploymentOwner``.

    Mirrors :func:`app.admin.init._seed_first_deployment_owner` shape:
    one live ``RoleGrant`` row with ``scope_kind='deployment'`` /
    ``workspace_id=NULL`` (gates the admin surface) + one
    ``DeploymentOwner`` row (membership in ``owners@deployment``,
    which carries governance authority).
    """
    with tenant_agnostic():
        existing_grant = session.scalar(
            select(RoleGrant.id)
            .where(RoleGrant.scope_kind == "deployment")
            .where(RoleGrant.user_id == user_id)
            .where(RoleGrant.revoked_at.is_(None))
            .limit(1)
        )
        if existing_grant is None:
            session.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=None,
                    user_id=user_id,
                    grant_role="manager",
                    scope_kind="deployment",
                    created_at=now,
                    created_by_user_id=None,
                )
            )
        existing_owner = session.get(DeploymentOwner, user_id)
        if existing_owner is None:
            session.add(
                DeploymentOwner(
                    user_id=user_id,
                    added_at=now,
                    added_by_user_id=None,
                )
            )
        session.flush()


def _ensure_owners_membership(
    session: SqlaSession,
    *,
    user_id: str,
    workspace_id: str,
    now: datetime,
) -> None:
    with tenant_agnostic():
        owners_group = session.scalars(
            select(PermissionGroup)
            .where(PermissionGroup.workspace_id == workspace_id)
            .where(PermissionGroup.slug == "owners")
        ).one_or_none()
        if owners_group is None:
            return
        existing = session.scalars(
            select(PermissionGroupMember)
            .where(PermissionGroupMember.group_id == owners_group.id)
            .where(PermissionGroupMember.user_id == user_id)
        ).one_or_none()
        if existing is not None:
            return
        session.add(
            PermissionGroupMember(
                group_id=owners_group.id,
                user_id=user_id,
                workspace_id=workspace_id,
                added_at=now,
                added_by_user_id=None,
            )
        )
        session.flush()


def _create_user(
    session: SqlaSession,
    *,
    email_lower: str,
    display_name: str,
    timezone: str | None,
    now: datetime,
) -> str:
    user_id = new_ulid()
    with tenant_agnostic():
        session.add(
            User(
                id=user_id,
                email=email_lower,
                email_lower=email_lower,
                display_name=display_name,
                timezone=timezone,
                created_at=now,
            )
        )
        session.flush()
    return user_id


def _create_workspace(
    session: SqlaSession,
    *,
    slug: str,
    name: str,
    owner_user_id: str,
    now: datetime,
) -> str:
    """Insert workspace + budget ledger + system permission groups.

    Mirrors :func:`scripts.dev_login._resolve_or_create_workspace` for
    the missing-workspace branch but without the existing-row early
    return — caller checks first. We don't go through
    :func:`provision_workspace_and_owner_seat` because that helper also
    inserts the :class:`User`; here we may already have one (a captured
    user id reused, or the user-existed branch landed first).
    """
    workspace_id = new_ulid()
    cap_cents = tight_cap_cents(FALLBACK_CAP_CENTS)
    with tenant_agnostic():
        session.add(
            Workspace(
                id=workspace_id,
                slug=slug,
                name=name,
                plan="free",
                quota_json=seed_free_tier_10pct(),
                created_at=now,
            )
        )
        session.flush()
        session.add(
            new_ledger_row(
                workspace_id=workspace_id,
                cap_cents=cap_cents,
                now=now,
            )
        )
        session.flush()
        seed_ctx = WorkspaceContext(
            workspace_id=workspace_id,
            workspace_slug=slug,
            actor_id=owner_user_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id=new_ulid(),
        )
        seed_owners_system_group(
            session,
            seed_ctx,
            workspace_id=workspace_id,
            owner_user_id=owner_user_id,
        )
        seed_system_permission_groups(
            session,
            workspace_id=workspace_id,
        )
        seed_starter_work_roles(session, workspace_id=workspace_id, now=now)
    return workspace_id


def _ensure_passkey(
    session: SqlaSession,
    *,
    user_id: str,
    credential_id: bytes,
    public_key: bytes,
    transports: str | None,
    backup_eligible: bool,
    aaguid: str | None,
    label: str | None,
    now: datetime,
) -> bool:
    """Insert one ``passkey_credential`` row if missing. Returns True on insert.

    Sign-count is forced to ``0`` so the first post-seed assertion
    skips the clone-detection branch in
    :func:`app.auth.passkey.login_finish`. Any later assertion bumps
    monotonically from whatever the authenticator returns.
    """
    with tenant_agnostic():
        existing = session.get(PasskeyCredential, credential_id)
        if existing is not None:
            return False
        session.add(
            PasskeyCredential(
                id=credential_id,
                user_id=user_id,
                public_key=public_key,
                sign_count=0,
                transports=transports,
                backup_eligible=backup_eligible,
                aaguid=aaguid,
                label=label,
                created_at=now,
                last_used_at=None,
            )
        )
        session.flush()
    return True


def _seed_smoke_workspace_content(
    session: SqlaSession,
    *,
    workspace_id: str,
    actor_id: str,
    now: datetime,
) -> dict[str, int]:
    ctx = WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="smoke",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )
    seed_asset_type_catalog(session, ctx)
    property_ids = _ensure_smoke_properties(session, workspace_id=workspace_id, now=now)
    area_ids = _smoke_area_ids(session, property_ids)
    type_ids = _smoke_asset_type_ids(session, workspace_id)
    asset_ids = _ensure_smoke_assets(
        session,
        workspace_id=workspace_id,
        property_ids=property_ids,
        area_ids=area_ids,
        type_ids=type_ids,
        now=now,
    )
    action_count = _ensure_smoke_actions(
        session,
        workspace_id=workspace_id,
        asset_ids=asset_ids,
        actor_id=actor_id,
        now=now,
    )
    document_count = _ensure_smoke_documents(
        session,
        workspace_id=workspace_id,
        asset_ids=asset_ids,
        now=now,
    )
    return {
        "properties": len(property_ids),
        "assets": len(asset_ids),
        "actions": action_count,
        "documents": document_count,
    }


def _ensure_smoke_properties(
    session: SqlaSession,
    *,
    workspace_id: str,
    now: datetime,
) -> dict[str, str]:
    out: dict[str, str] = {}
    with tenant_agnostic():
        for spec in _SMOKE_PROPERTY_SPECS:
            existing = session.scalar(
                select(Property)
                .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
                .where(PropertyWorkspace.workspace_id == workspace_id)
                .where(Property.name == spec.name)
                .where(Property.deleted_at.is_(None))
                .limit(1)
            )
            if existing is None:
                property_id = new_ulid()
                existing = Property(
                    id=property_id,
                    name=spec.name,
                    kind=spec.kind,
                    address=f"{spec.name}, {spec.city}",
                    address_json={"city": spec.city, "country": spec.country},
                    country=spec.country,
                    locale=spec.locale,
                    default_currency=spec.currency,
                    timezone=spec.timezone,
                    tags_json=["smoke", "seed"],
                    welcome_defaults_json={},
                    settings_override_json={"assets.show_guest_assets": True},
                    property_notes_md="Seeded by dev_seed_personal for smoke testing.",
                    created_at=now,
                    updated_at=now,
                    deleted_at=None,
                )
                session.add(existing)
                session.flush()
                session.add(
                    PropertyWorkspace(
                        property_id=property_id,
                        workspace_id=workspace_id,
                        label=spec.name,
                        membership_role="owner_workspace",
                        share_guest_identity=True,
                        auto_shift_from_occurrence=False,
                        status="active",
                        created_at=now,
                    )
                )
            out[spec.name] = existing.id
            _ensure_smoke_areas(
                session, property_id=existing.id, labels=spec.areas, now=now
            )
        session.flush()
    return out


def _ensure_smoke_areas(
    session: SqlaSession,
    *,
    property_id: str,
    labels: tuple[str, ...],
    now: datetime,
) -> None:
    existing = set(
        session.scalars(
            select(Area.label)
            .where(Area.property_id == property_id)
            .where(Area.deleted_at.is_(None))
        ).all()
    )
    for ordering, label in enumerate(labels):
        if label in existing:
            continue
        kind = "outdoor" if label in {"Garden", "Pool plant"} else "indoor_room"
        session.add(
            Area(
                id=new_ulid(),
                property_id=property_id,
                unit_id=None,
                name=label,
                label=label,
                kind=kind,
                icon=None,
                ordering=ordering,
                parent_area_id=None,
                notes_md="",
                created_at=now,
                updated_at=now,
                deleted_at=None,
            )
        )


def _smoke_area_ids(
    session: SqlaSession,
    property_ids: dict[str, str],
) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    with tenant_agnostic():
        for property_name, property_id in property_ids.items():
            rows = session.execute(
                select(Area.id, Area.label).where(
                    Area.property_id == property_id,
                    Area.deleted_at.is_(None),
                )
            ).all()
            for area_id, label in rows:
                out[(property_name, label)] = area_id
    return out


def _smoke_asset_type_ids(session: SqlaSession, workspace_id: str) -> dict[str, str]:
    with tenant_agnostic():
        rows = session.execute(
            select(AssetType.key, AssetType.id).where(
                AssetType.workspace_id == workspace_id,
                AssetType.deleted_at.is_(None),
            )
        ).all()
    return {key: asset_type_id for key, asset_type_id in rows}


def _ensure_smoke_assets(
    session: SqlaSession,
    *,
    workspace_id: str,
    property_ids: dict[str, str],
    area_ids: dict[tuple[str, str], str],
    type_ids: dict[str, str],
    now: datetime,
) -> dict[str, str]:
    out: dict[str, str] = {}
    with tenant_agnostic():
        for spec in _SMOKE_ASSET_SPECS:
            existing = session.scalar(
                select(Asset)
                .where(Asset.workspace_id == workspace_id)
                .where(Asset.name == spec.name)
                .where(Asset.deleted_at.is_(None))
                .limit(1)
            )
            if existing is None:
                asset_id = new_ulid()
                existing = Asset(
                    id=asset_id,
                    workspace_id=workspace_id,
                    property_id=property_ids[spec.property_name],
                    area_id=area_ids.get((spec.property_name, spec.area)),
                    asset_type_id=type_ids.get(spec.type_key),
                    name=spec.name,
                    make=spec.make,
                    model=spec.model,
                    serial_number=spec.serial,
                    condition=spec.condition,
                    status=spec.status,
                    installed_on=spec.installed_on,
                    purchased_on=spec.purchased_on,
                    purchase_price_cents=spec.price_cents,
                    purchase_currency=spec.currency,
                    purchase_vendor=spec.vendor,
                    warranty_expires_on=spec.warranty,
                    expected_lifespan_years=None,
                    estimated_replacement_on=None,
                    cover_photo_file_id=None,
                    qr_token=short_token(workspace_id, asset_id),
                    guest_visible=spec.guest_visible,
                    guest_instructions_md=spec.guest_instructions,
                    notes_md=spec.notes,
                    settings_override_json=None,
                    created_at=now,
                    updated_at=now,
                    deleted_at=None,
                )
                session.add(existing)
                session.flush()
            out[spec.name] = existing.id
    return out


def _ensure_smoke_actions(
    session: SqlaSession,
    *,
    workspace_id: str,
    asset_ids: dict[str, str],
    actor_id: str,
    now: datetime,
) -> int:
    specs = (
        ("Pool pump #2", "inspect", "Inspect seal", 180, now - timedelta(days=210)),
        (
            "Kitchen refrigerator",
            "service",
            "Clean coils",
            180,
            now - timedelta(days=35),
        ),
        (
            "Laundry washer",
            "repair",
            "Drain pump repair",
            None,
            now - timedelta(days=4),
        ),
    )
    count = 0
    with tenant_agnostic():
        for asset_name, kind, label, interval_days, performed_at in specs:
            asset_id = asset_ids[asset_name]
            existing = session.scalar(
                select(AssetAction.id)
                .where(AssetAction.workspace_id == workspace_id)
                .where(AssetAction.asset_id == asset_id)
                .where(AssetAction.label == label)
                .where(AssetAction.deleted_at.is_(None))
                .limit(1)
            )
            if existing is None:
                session.add(
                    AssetAction(
                        id=new_ulid(),
                        workspace_id=workspace_id,
                        asset_id=asset_id,
                        key=f"smoke_{kind}_{asset_name.lower().replace(' ', '_')}",
                        kind=kind,
                        label=label,
                        description_md=None,
                        task_template_id=None,
                        schedule_id=None,
                        interval_days=interval_days,
                        estimated_duration_minutes=30,
                        inventory_effects_json=None,
                        last_performed_at=performed_at,
                        last_performed_task_id=None,
                        performed_by=actor_id,
                        notes_md="Seeded smoke maintenance record.",
                        meter_reading=None,
                        evidence_blob_hash=None,
                        created_at=performed_at,
                        updated_at=performed_at,
                        deleted_at=None,
                    )
                )
            count += 1
    return count


def _ensure_smoke_documents(
    session: SqlaSession,
    *,
    workspace_id: str,
    asset_ids: dict[str, str],
    now: datetime,
) -> int:
    count = 0
    with tenant_agnostic():
        for spec in _SMOKE_DOCUMENT_SPECS:
            asset_id = asset_ids[spec.asset_name]
            existing = session.scalar(
                select(AssetDocument)
                .where(AssetDocument.workspace_id == workspace_id)
                .where(AssetDocument.asset_id == asset_id)
                .where(AssetDocument.title == spec.title)
                .where(AssetDocument.deleted_at.is_(None))
                .limit(1)
            )
            if existing is None:
                existing = AssetDocument(
                    id=new_ulid(),
                    workspace_id=workspace_id,
                    file_id=None,
                    blob_hash=_smoke_blob_hash(spec.filename),
                    filename=spec.filename,
                    asset_id=asset_id,
                    property_id=None,
                    kind=spec.kind,
                    title=spec.title,
                    notes_md=spec.notes,
                    expires_on=spec.expires_on,
                    amount_cents=spec.amount_cents,
                    amount_currency=spec.amount_currency,
                    created_at=now,
                    updated_at=now,
                    deleted_at=None,
                )
                session.add(existing)
                session.flush()
            _ensure_smoke_extraction(
                session,
                workspace_id=workspace_id,
                document=existing,
                spec=spec,
                now=now,
            )
            count += 1
    return count


def _ensure_smoke_extraction(
    session: SqlaSession,
    *,
    workspace_id: str,
    document: AssetDocument,
    spec: _SmokeDocumentSpec,
    now: datetime,
) -> None:
    row = session.get(FileExtraction, document.id)
    body_text = spec.body_text
    extracted_at = now if spec.extraction_status in {"empty", "succeeded"} else None
    last_error = (
        "Seeded smoke extraction failure: sample PDF text layer was unreadable."
        if spec.extraction_status == "failed"
        else None
    )
    if body_text:
        pages_json: list[dict[str, int]] | None = [
            {"page": 1, "char_start": 0, "char_end": len(body_text)}
        ]
        token_count = len(body_text.split())
    elif spec.extraction_status == "empty":
        pages_json = []
        token_count = 0
    else:
        pages_json = None
        token_count = None
    attempts = 1 if spec.extraction_status in {"failed", "succeeded"} else 0
    if row is None:
        session.add(
            FileExtraction(
                id=document.id,
                workspace_id=workspace_id,
                extraction_status=spec.extraction_status,
                extractor=spec.extractor,
                body_text=body_text,
                pages_json=pages_json,
                token_count=token_count,
                has_secret_marker=False,
                attempts=attempts,
                last_error=last_error,
                extracted_at=extracted_at,
                created_at=now,
                updated_at=now,
            )
        )
        return
    row.workspace_id = workspace_id
    row.extraction_status = spec.extraction_status
    row.extractor = spec.extractor
    row.body_text = body_text
    row.pages_json = pages_json
    row.token_count = token_count
    row.has_secret_marker = False
    row.attempts = attempts
    row.last_error = last_error
    row.extracted_at = extracted_at
    row.updated_at = now


def _smoke_blob_hash(filename: str) -> str:
    import hashlib

    return hashlib.sha256(f"crewday-smoke-document:{filename}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Apply — load JSON, seed rows.
# ---------------------------------------------------------------------------


def apply_seed(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the parsed seed payload. Idempotent.

    Returns a small summary dict the CLI prints.
    """
    owner = payload["owner"]
    workspace = payload["workspace"]
    email_lower = canonicalise_email(owner["email"])
    display_name = owner.get("display_name") or email_lower.split("@", 1)[0]
    timezone = owner.get("timezone")
    workspace_slug = workspace["slug"]
    workspace_name = workspace.get("name") or workspace_slug
    grant_role = workspace.get("role", "manager")
    if grant_role == "owner":
        # Schema enum dropped the legacy ``owner`` value; the governance
        # bit lives on the ``owners`` permission group. Map for parity
        # with dev_login.py.
        grant_role = "manager"
    is_owner = grant_role == "manager"
    deployment_admin = bool(owner.get("deployment_admin", True))
    # Default deployment settings flipped on every apply: dev signup
    # works without a CAPTCHA gate (the dev stack has no Turnstile
    # secret wired in, and the only consumer is *you* signing up to
    # re-register your authenticator). Override / extend via the
    # ``deployment_settings`` block on the seed JSON.
    settings_overrides: dict[str, Any] = {"captcha_required": False}
    settings_overrides.update(payload.get("deployment_settings") or {})

    summary: dict[str, Any] = {
        "user_created": False,
        "workspace_created": False,
        "passkeys_inserted": 0,
        "passkeys_skipped": 0,
        "smoke_content": {},
        "deployment_admin": deployment_admin,
        "deployment_settings_applied": sorted(settings_overrides),
    }

    with make_uow() as uow_session:
        assert isinstance(uow_session, SqlaSession)
        session = uow_session
        now = SystemClock().now()

        existing_user = _find_user(session, email_lower)
        if existing_user is None:
            user_id = _create_user(
                session,
                email_lower=email_lower,
                display_name=display_name,
                timezone=timezone,
                now=now,
            )
            summary["user_created"] = True
        else:
            user_id = existing_user.id

        existing_workspace = _find_workspace(session, workspace_slug)
        if existing_workspace is None:
            workspace_id = _create_workspace(
                session,
                slug=workspace_slug,
                name=workspace_name,
                owner_user_id=user_id,
                now=now,
            )
            summary["workspace_created"] = True
        else:
            workspace_id = existing_workspace.id

        seed_starter_work_roles(session, workspace_id=workspace_id, now=now)
        _ensure_user_workspace(
            session, user_id=user_id, workspace_id=workspace_id, now=now
        )
        _ensure_role_grant(
            session,
            user_id=user_id,
            workspace_id=workspace_id,
            grant_role=grant_role,
            now=now,
        )
        if is_owner:
            _ensure_owners_membership(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                now=now,
            )
        if deployment_admin:
            _ensure_deployment_admin(session, user_id=user_id, now=now)

        for key, value in settings_overrides.items():
            _upsert_deployment_setting(session, key=key, value=value, now=now)

        if workspace_slug == "smoke":
            summary["smoke_content"] = _seed_smoke_workspace_content(
                session,
                workspace_id=workspace_id,
                actor_id=user_id,
                now=now,
            )

        for entry in owner.get("passkeys", ()):
            inserted = _ensure_passkey(
                session,
                user_id=user_id,
                credential_id=base64url_to_bytes(entry["credential_id_b64"]),
                public_key=base64url_to_bytes(entry["public_key_b64"]),
                transports=entry.get("transports"),
                backup_eligible=bool(entry.get("backup_eligible", False)),
                aaguid=entry.get("aaguid"),
                label=entry.get("label"),
                now=now,
            )
            if inserted:
                summary["passkeys_inserted"] += 1
            else:
                summary["passkeys_skipped"] += 1

        summary["user_id"] = user_id
        summary["workspace_id"] = workspace_id
        summary["workspace_slug"] = workspace_slug

    return summary


# ---------------------------------------------------------------------------
# Capture — read live rows, build JSON.
# ---------------------------------------------------------------------------


def capture_seed(*, email: str, workspace_slug: str) -> dict[str, Any]:
    """Read the live user + workspace + passkeys, return a serialisable payload."""
    email_lower = canonicalise_email(email)
    with make_uow() as uow_session:
        assert isinstance(uow_session, SqlaSession)
        session = uow_session

        user = _find_user(session, email_lower)
        if user is None:
            raise click.ClickException(
                f"no user with email {email!r} — sign up via the SPA first."
            )
        workspace = _find_workspace(session, workspace_slug)
        if workspace is None:
            raise click.ClickException(f"no workspace with slug {workspace_slug!r}.")

        with tenant_agnostic():
            credentials = session.scalars(
                select(PasskeyCredential)
                .where(PasskeyCredential.user_id == user.id)
                .order_by(PasskeyCredential.created_at)
            ).all()
            grant = session.scalars(
                select(RoleGrant)
                .where(RoleGrant.user_id == user.id)
                .where(RoleGrant.workspace_id == workspace.id)
                .where(RoleGrant.scope_property_id.is_(None))
            ).first()
            deployment_admin = (
                session.scalar(
                    select(RoleGrant.id)
                    .where(RoleGrant.scope_kind == "deployment")
                    .where(RoleGrant.user_id == user.id)
                    .where(RoleGrant.revoked_at.is_(None))
                    .limit(1)
                )
                is not None
            )

        passkeys: list[dict[str, Any]] = []
        for cred in credentials:
            passkeys.append(
                {
                    "credential_id_b64": bytes_to_base64url(cred.id),
                    "public_key_b64": bytes_to_base64url(cred.public_key),
                    "sign_count": cred.sign_count,
                    "transports": cred.transports,
                    "backup_eligible": cred.backup_eligible,
                    "aaguid": cred.aaguid,
                    "label": cred.label,
                }
            )

        return {
            "owner": {
                "email": user.email,
                "display_name": user.display_name,
                "timezone": user.timezone,
                "deployment_admin": deployment_admin,
                "passkeys": passkeys,
            },
            "workspace": {
                "slug": workspace.slug,
                "name": workspace.name,
                "role": grant.grant_role if grant is not None else "manager",
            },
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group(
    help=(
        "Dev-only: rehydrate your personal user + workspace + passkeys after "
        "a SQLite reset, or capture the current state into the seed JSON."
    )
)
def main() -> None:
    """Top-level group; subcommands enforce gates themselves."""


@main.command(
    "apply",
    help=(
        "Read the seed JSON and (idempotently) seed user + workspace + "
        "passkey rows. Defaults to scripts/dev_seed_personal.json."
    ),
)
@click.option(
    "--file",
    "seed_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=str(SEED_FILE),
    help="Seed file path (default: scripts/dev_seed_personal.json).",
)
def apply_cmd(seed_path: Path) -> None:
    try:
        _check_gates()
    except _GateError as exc:
        sys.exit(_fail_gate(exc))

    if not seed_path.exists():
        click.echo(
            f"error: seed file not found at {seed_path}. "
            "Run `dev_seed_personal capture` first.",
            err=True,
        )
        sys.exit(2)

    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    summary = apply_seed(payload)
    click.echo(json.dumps(summary, indent=2, sort_keys=True))


@main.command(
    "capture",
    help=(
        "Read the live user + workspace + passkey rows and write the seed "
        "JSON. Use after registering your passkey through the SPA."
    ),
)
@click.option("--email", required=True, help="Owner email address.")
@click.option(
    "--workspace",
    "workspace_slug",
    required=True,
    help="Workspace slug to capture.",
)
@click.option(
    "--file",
    "seed_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=str(SEED_FILE),
    help="Output path (default: scripts/dev_seed_personal.json).",
)
def capture_cmd(email: str, workspace_slug: str, seed_path: Path) -> None:
    try:
        _check_gates()
    except _GateError as exc:
        sys.exit(_fail_gate(exc))

    payload = capture_seed(email=email, workspace_slug=workspace_slug)
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    seed_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    n_passkeys = len(payload["owner"]["passkeys"])
    click.echo(
        f"wrote {seed_path} ({n_passkeys} passkey"
        f"{'s' if n_passkeys != 1 else ''} captured)"
    )


if __name__ == "__main__":
    main()
