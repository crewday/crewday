#!/usr/bin/env python3
"""Dev-only personal seed — rehydrate your user + workspace + passkeys.

Companion to :mod:`scripts.dev_login`. After a SQLite reset, ``apply``
re-creates the rows your physical authenticator needs to log into
``https://dev.crew.day`` without re-tapping a fresh registration:

1. Sign up once at ``https://dev.crew.day/signup`` (signup is on by
   default; the login page just doesn't link to it). Register your
   passkey through the normal ceremony — it binds to
   ``rp_id="dev.crew.day"``.
2. ``python -m scripts.dev_seed_personal capture --email <e> --workspace <slug>``
   writes :data:`SEED_FILE` (``scripts/dev_seed_personal.json``)
   carrying the user, workspace, and every ``passkey_credential`` row.
   Public material only — the private key never leaves your device.
3. After every DB reset: ``python -m scripts.dev_seed_personal apply``.
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
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import click
from sqlalchemy import select
from sqlalchemy.orm import Session as SqlaSession

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
from app.adapters.db.session import make_uow
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.auth.signup import FALLBACK_CAP_CENTS
from app.auth.webauthn import base64url_to_bytes, bytes_to_base64url
from app.config import get_settings
from app.domain.llm.budget import new_ledger_row
from app.domain.plans import seed_free_tier_10pct, tight_cap_cents
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import SystemClock
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
