"""Host-only first-boot and workspace bootstrap services.

These functions back ``crewday admin`` commands. They run in-process
against the deployment database, never through the HTTP admin surface.
"""

from __future__ import annotations

from dataclasses import MISSING, dataclass, fields
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.adapters.db.assets.bootstrap import seed_asset_type_catalog
from app.adapters.db.authz.bootstrap import (
    seed_owners_system_group,
    seed_system_permission_groups,
)
from app.adapters.db.authz.models import PermissionGroup
from app.adapters.db.capabilities.models import DeploymentSetting
from app.adapters.db.identity.models import (
    Invite,
    MagicLinkNonce,
    User,
    canonicalise_email,
)
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.audit import write_deployment_audit
from app.auth._hashing import hash_with_pepper
from app.auth._throttle import Throttle
from app.auth.keys import derive_subkey
from app.auth.magic_link import PendingMagicLink, request_link
from app.capabilities import Capabilities, DeploymentSettings
from app.config import Settings
from app.domain.llm.budget import new_ledger_row
from app.domain.plans import FREE_TIER_DEFAULTS, seed_free_tier_10pct, tight_cap_cents
from app.fixtures.llm import seed_default_registry
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.tenancy.slug import normalise_slug
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "ADMIN_DEMO_REFUSAL",
    "AdminInitResult",
    "AdminInviteResult",
    "WorkspaceBootstrapResult",
    "admin_init",
    "invite_user",
    "is_admin_initialized",
    "workspace_bootstrap",
]


ADMIN_DEMO_REFUSAL: Final[str] = "admin commands not available in demo"
_SYSTEM_ACTOR_ID: Final[str] = "00000000000000000000000000"
_INIT_MARKER_KEY: Final[str] = "admin_init_completed"
_ROOT_KEY_GENERATED_MARKER_KEY: Final[str] = "admin_init_root_key_generated"
_MAGIC_LINK_BASE_URL: Final[str] = "http://127.0.0.1:8000"
_INVITE_TTL: Final[timedelta] = timedelta(hours=24)
_VALID_INVITE_ROLES: Final[frozenset[str]] = frozenset(
    {"owner", "manager", "worker", "client"}
)


@dataclass(frozen=True, slots=True)
class AdminInitResult:
    initialized: bool
    generated_root_key: str | None
    settings_seeded: int
    llm_provider_model_id: str


@dataclass(frozen=True, slots=True)
class AdminInviteResult:
    invite_id: str
    user_id: str
    url: str
    role: str
    workspace_id: str
    workspace_slug: str


@dataclass(frozen=True, slots=True)
class WorkspaceBootstrapResult:
    workspace_id: str
    user_id: str
    url: str


def _now(clock: Clock | None) -> datetime:
    return (clock if clock is not None else SystemClock()).now()


def _require_not_demo(settings: Settings) -> None:
    if settings.demo_mode:
        raise RuntimeError(ADMIN_DEMO_REFUSAL)


def _require_root_key(settings: Settings) -> SecretStr:
    root_key = settings.root_key
    if root_key is None or not root_key.get_secret_value():
        raise RuntimeError("CREWDAY_ROOT_KEY is required for admin magic links")
    return root_key


def _public_url(settings: Settings) -> str:
    return (settings.public_url or _MAGIC_LINK_BASE_URL).rstrip("/")


def _setting_default(field_name: str) -> Any:
    field = next(f for f in fields(DeploymentSettings) if f.name == field_name)
    if field.default is not MISSING:
        return field.default
    default_factory = field.default_factory
    if default_factory is not MISSING:
        return default_factory()
    raise RuntimeError(f"deployment setting {field_name!r} has no default")


def _seed_deployment_settings(
    session: Session,
    *,
    now: datetime,
    updated_by: str,
) -> int:
    seeded = 0
    with tenant_agnostic():
        for field in fields(DeploymentSettings):
            if session.get(DeploymentSetting, field.name) is not None:
                continue
            session.add(
                DeploymentSetting(
                    key=field.name,
                    value=_setting_default(field.name),
                    updated_at=now,
                    updated_by=updated_by,
                )
            )
            seeded += 1
        session.flush()
    return seeded


def _deployment_audit(
    session: Session,
    *,
    entity_kind: str,
    entity_id: str,
    action: str,
    diff: dict[str, Any] | None = None,
    clock: Clock | None = None,
) -> None:
    payload = {"via": "cli"}
    if diff:
        payload.update(diff)
    write_deployment_audit(
        session,
        actor_id=_SYSTEM_ACTOR_ID,
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        correlation_id=new_ulid(clock=clock),
        entity_kind=entity_kind,
        entity_id=entity_id,
        action=action,
        diff=payload,
        via="cli",
        clock=clock,
    )


def _system_workspace_ctx(
    *,
    workspace_id: str,
    workspace_slug: str,
    clock: Clock | None = None,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=_SYSTEM_ACTOR_ID,
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(clock=clock),
        principal_kind="system",
    )


def _email_hash(email_lower: str, *, settings: Settings) -> str:
    pepper = derive_subkey(_require_root_key(settings), purpose="magic-link")
    return hash_with_pepper(email_lower, pepper)


def _ensure_user(
    session: Session,
    *,
    email_lower: str,
    display_name: str,
    now: datetime,
    clock: Clock | None = None,
) -> tuple[User, bool]:
    with tenant_agnostic():
        existing = session.scalar(select(User).where(User.email_lower == email_lower))
        if existing is not None:
            return existing, False
        user = User(
            id=new_ulid(clock=clock),
            email=email_lower,
            email_lower=email_lower,
            display_name=display_name,
            timezone="UTC",
            created_at=now,
        )
        session.add(user)
        session.flush()
        return user, True


def _pending_invite(
    session: Session,
    *,
    workspace_id: str,
    email_lower: str,
) -> Invite | None:
    with tenant_agnostic():
        return session.scalar(
            select(Invite).where(
                Invite.workspace_id == workspace_id,
                Invite.pending_email_lower == email_lower,
                Invite.state == "pending",
            )
        )


def _delete_pending_invite_nonces(session: Session, *, invite_id: str) -> None:
    with tenant_agnostic():
        session.execute(
            delete(MagicLinkNonce)
            .where(MagicLinkNonce.subject_id == invite_id)
            .where(MagicLinkNonce.purpose == "grant_invite")
            .where(MagicLinkNonce.consumed_at.is_(None))
            .execution_options(synchronize_session=False)
        )
        session.flush()


def _mint_link(
    session: Session,
    *,
    email_lower: str,
    purpose: Literal["grant_invite"],
    subject_id: str,
    settings: Settings,
    base_url: str,
    now: datetime,
    ttl: timedelta,
    clock: Clock | None = None,
) -> PendingMagicLink:
    pending = request_link(
        session,
        email=email_lower,
        purpose=purpose,
        ip="cli",
        mailer=None,
        base_url=base_url,
        now=now,
        ttl=ttl,
        throttle=Throttle(),
        settings=settings,
        clock=clock,
        subject_id=subject_id,
        send_email=False,
        via="cli",
    )
    if pending is None:
        raise RuntimeError(f"failed to mint {purpose} magic link")
    return pending


def admin_init(
    session: Session,
    *,
    settings: Settings,
    generated_root_key: str | None = None,
    clock: Clock | None = None,
) -> AdminInitResult:
    """Seed deployment defaults for first boot.

    The root key itself is never persisted. When the CLI had to
    generate one, this function records only a boolean marker so a
    later idempotent run does not print a second disposable secret.
    """
    _require_not_demo(settings)
    now = _now(clock)
    with tenant_agnostic():
        completed = session.get(DeploymentSetting, _INIT_MARKER_KEY)
        if completed is not None:
            return AdminInitResult(
                initialized=False,
                generated_root_key=None,
                settings_seeded=0,
                llm_provider_model_id="",
            )
    seeded = _seed_deployment_settings(session, now=now, updated_by="system")
    with tenant_agnostic():
        provider_model = seed_default_registry(session, clock=clock)
        if generated_root_key is not None:
            session.add(
                DeploymentSetting(
                    key=_ROOT_KEY_GENERATED_MARKER_KEY,
                    value=True,
                    updated_at=now,
                    updated_by="system",
                )
            )
        session.add(
            DeploymentSetting(
                key=_INIT_MARKER_KEY,
                value=True,
                updated_at=now,
                updated_by="system",
            )
        )
        _deployment_audit(
            session,
            entity_kind="deployment",
            entity_id="default",
            action="admin.init",
            diff={
                "settings_seeded": seeded,
                "root_key_generated": generated_root_key is not None,
                "llm_provider_model_id": provider_model.id,
            },
            clock=clock,
        )
        session.flush()
    return AdminInitResult(
        initialized=True,
        generated_root_key=generated_root_key,
        settings_seeded=seeded,
        llm_provider_model_id=provider_model.id,
    )


def is_admin_initialized(session: Session) -> bool:
    """Return whether ``crewday admin init`` has already completed."""
    with tenant_agnostic():
        return session.get(DeploymentSetting, _INIT_MARKER_KEY) is not None


def invite_user(
    session: Session,
    *,
    settings: Settings,
    email: str,
    workspace_slug: str,
    role: str = "worker",
    clock: Clock | None = None,
) -> AdminInviteResult:
    """Create or refresh a pending invite and return its magic link URL."""
    _require_not_demo(settings)
    _require_root_key(settings)
    if role not in _VALID_INVITE_ROLES:
        raise ValueError(f"role must be one of {sorted(_VALID_INVITE_ROLES)}")
    now = _now(clock)
    email_lower = canonicalise_email(email)
    if not email_lower or "@" not in email_lower:
        raise ValueError("email must be a valid address")
    with tenant_agnostic():
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == workspace_slug)
        )
    if workspace is None:
        raise LookupError(f"workspace {workspace_slug!r} not found")

    display_name = email_lower.split("@", 1)[0] or email_lower
    user, user_created = _ensure_user(
        session,
        email_lower=email_lower,
        display_name=display_name,
        now=now,
        clock=clock,
    )
    grant_role = "manager" if role == "owner" else role
    grants = [
        {
            "scope_kind": "workspace",
            "scope_id": workspace.id,
            "grant_role": grant_role,
            "scope_property_id": None,
        }
    ]
    group_memberships: list[dict[str, str]] = []
    if role == "owner":
        with tenant_agnostic():
            owners = session.scalar(
                select(PermissionGroup).where(
                    PermissionGroup.workspace_id == workspace.id,
                    PermissionGroup.slug == "owners",
                )
            )
        if owners is None:
            raise LookupError(
                f"workspace {workspace.slug!r} has no owners system group"
            )
        group_memberships.append({"group_id": owners.id})

    email_hash = _email_hash(email_lower, settings=settings)
    existing = _pending_invite(
        session, workspace_id=workspace.id, email_lower=email_lower
    )
    if existing is None:
        invite_id = new_ulid(clock=clock)
        invite = Invite(
            id=invite_id,
            workspace_id=workspace.id,
            user_id=user.id,
            pending_email=email_lower,
            pending_email_lower=email_lower,
            email_hash=email_hash,
            display_name=display_name,
            state="pending",
            grants_json=grants,
            group_memberships_json=group_memberships,
            invited_by_user_id=None,
            created_at=now,
            expires_at=now + _INVITE_TTL,
            accepted_at=None,
            revoked_at=None,
        )
        session.add(invite)
    else:
        invite_id = existing.id
        existing.user_id = user.id
        existing.pending_email = email_lower
        existing.pending_email_lower = email_lower
        existing.email_hash = email_hash
        existing.display_name = display_name
        existing.grants_json = grants
        existing.group_memberships_json = group_memberships
        existing.expires_at = now + _INVITE_TTL
        _delete_pending_invite_nonces(session, invite_id=invite_id)
    session.flush()

    pending = _mint_link(
        session,
        email_lower=email_lower,
        purpose="grant_invite",
        subject_id=invite_id,
        settings=settings,
        base_url=_public_url(settings),
        now=now,
        ttl=_INVITE_TTL,
        clock=clock,
    )
    _deployment_audit(
        session,
        entity_kind="invite",
        entity_id=invite_id,
        action="admin.user_invited",
        diff={
            "workspace_id": workspace.id,
            "workspace_slug": workspace.slug,
            "role": role,
            "email_hash": email_hash,
            "user_id": user.id,
            "user_created": user_created,
        },
        clock=clock,
    )
    session.flush()
    return AdminInviteResult(
        invite_id=invite_id,
        user_id=user.id,
        url=pending.url,
        role=role,
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
    )


def workspace_bootstrap(
    session: Session,
    *,
    settings: Settings,
    slug: str,
    name: str,
    owner_email: str,
    capabilities: Capabilities | None = None,
    clock: Clock | None = None,
) -> WorkspaceBootstrapResult:
    """Create a workspace, seed its owner, and mint the owner's link."""
    _require_not_demo(settings)
    _require_root_key(settings)
    now = _now(clock)
    slug = normalise_slug(slug)
    owner_email_lower = canonicalise_email(owner_email)
    if not owner_email_lower or "@" not in owner_email_lower:
        raise ValueError("owner email must be a valid address")
    with tenant_agnostic():
        existing = session.scalar(select(Workspace).where(Workspace.slug == slug))
    if existing is not None:
        raise ValueError(f"workspace slug {slug!r} already exists")

    owner, user_created = _ensure_user(
        session,
        email_lower=owner_email_lower,
        display_name=owner_email_lower.split("@", 1)[0] or owner_email_lower,
        now=now,
        clock=clock,
    )
    workspace_id = new_ulid(clock=clock)
    full_cap_cents = (
        capabilities.settings.llm_default_budget_cents_30d
        if capabilities is not None
        else FREE_TIER_DEFAULTS["llm_budget_cents_30d"]
    )
    with tenant_agnostic():
        workspace = Workspace(
            id=workspace_id,
            slug=slug,
            name=name,
            plan="free",
            quota_json=seed_free_tier_10pct(),
            settings_json={},
            created_at=now,
            updated_at=now,
        )
        session.add(workspace)
        session.flush()
        session.add(
            new_ledger_row(
                workspace_id=workspace_id,
                cap_cents=tight_cap_cents(full_cap_cents),
                now=now,
                clock=clock,
            )
        )
        session.add(
            UserWorkspace(
                user_id=owner.id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=now,
            )
        )
        session.flush()
        ctx = _system_workspace_ctx(
            workspace_id=workspace_id,
            workspace_slug=slug,
            clock=clock,
        )
        seed_owners_system_group(
            session,
            ctx,
            workspace_id=workspace_id,
            owner_user_id=owner.id,
            via="cli",
            clock=clock,
        )
        seed_system_permission_groups(session, workspace_id=workspace_id, clock=clock)
        seed_asset_type_catalog(session, ctx, clock=clock)
        invite_id = new_ulid(clock=clock)
        session.add(
            Invite(
                id=invite_id,
                workspace_id=workspace_id,
                user_id=owner.id,
                pending_email=owner_email_lower,
                pending_email_lower=owner_email_lower,
                email_hash=_email_hash(owner_email_lower, settings=settings),
                display_name=owner.display_name,
                state="pending",
                grants_json=[],
                group_memberships_json=[],
                invited_by_user_id=None,
                created_at=now,
                expires_at=now + _INVITE_TTL,
                accepted_at=None,
                revoked_at=None,
            )
        )
        _deployment_audit(
            session,
            entity_kind="workspace",
            entity_id=workspace_id,
            action="admin.workspace_bootstrapped",
            diff={
                "workspace_slug": slug,
                "owner_user_id": owner.id,
                "owner_user_created": user_created,
                "via": "cli",
            },
            clock=clock,
        )
        pending = _mint_link(
            session,
            email_lower=owner_email_lower,
            purpose="grant_invite",
            subject_id=invite_id,
            settings=settings,
            base_url=_public_url(settings),
            now=now,
            ttl=_INVITE_TTL,
            clock=clock,
        )
        session.flush()
    return WorkspaceBootstrapResult(
        workspace_id=workspace_id,
        user_id=owner.id,
        url=pending.url,
    )
