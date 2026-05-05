"""Internal helper for ``scripts/schemathesis_run.sh`` (cd-3j25).

Mints a workspace-scoped API token plaintext on stdout so the runner
can hand a Bearer token to ``schemathesis run``. Runs in the same
process as the rest of the schemathesis sweep tooling, against a
dev-only SQLite database.

The token is minted via the domain service
(:func:`app.auth.tokens.mint`) directly rather than through the
HTTP surface — the HTTP path requires a CSRF round-trip + a session
cookie that we'd otherwise have to thread through curl, which is
brittle. The domain call has the same audit + cap semantics as the
HTTP route; bypassing the wire layer here is fine for a dev-only
seed helper.

Hard-gated on ``CREWDAY_DEV_AUTH=1`` + ``profile=dev`` + a SQLite
URL — same gates as :mod:`scripts.dev_login`. A misconfigured prod
deploy that happens to flip the env var still fails.

Output (one line, no trailing newline):

* ``--output token``   : ``<plaintext>``
* ``--output bearer``  : ``Authorization: Bearer <plaintext>``
* ``--output session`` : ``<session-cookie-value>``
* ``--output cookie``  : ``__Host-crewday_session=<session-cookie-value>``

Spec refs: ``docs/specs/03-auth-and-tokens.md`` §"API tokens",
``docs/specs/17-testing-quality.md`` §"API contract".
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Final, Literal

import click
from sqlalchemy import select
from sqlalchemy.orm import Session as SqlaSession

from app.adapters.db.authz.models import DeploymentOwner, RoleGrant
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.llm.models import AgentDoc
from app.adapters.db.messaging.models import Notification
from app.adapters.db.session import make_uow
from app.adapters.db.workspace.models import Workspace
from app.auth.tokens import mint as mint_token
from app.config import get_settings
from app.domain.messaging.push_tokens import SETTINGS_KEY_VAPID_PUBLIC
from app.services.agent.system_docs import seed_agent_docs
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import SystemClock
from app.util.ulid import new_ulid
from scripts.dev_login import mint_session

OutputFormat = Literal["token", "bearer", "session", "cookie", "admin-contract-env"]
_SESSION_COOKIE_NAME: Final[str] = "__Host-crewday_session"
_ADMIN_REVOKE_EMAIL: Final[str] = "schemathesis-admin-revoke@dev.local"
_CONTRACT_VAPID_PUBLIC_KEY: Final[str] = "schemathesis-vapid-public-key"


_DEV_AUTH_ENV_VAR: Final[str] = "CREWDAY_DEV_AUTH"


@dataclass(frozen=True, slots=True)
class ContractPathResources:
    workspace_id: str
    admin_revoke_grant_id: str
    agent_doc_slug: str
    notification_id: str


def _ensure_deployment_admin(
    session: SqlaSession,
    *,
    user_id: str,
) -> None:
    """Give the schemathesis actor access to the bare-host admin tree."""
    now = SystemClock().now()
    with tenant_agnostic():
        existing_grant = session.scalar(
            select(RoleGrant.id)
            .where(RoleGrant.scope_kind == "deployment")
            .where(RoleGrant.user_id == user_id)
            .where(RoleGrant.grant_role == "manager")
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
        if session.get(DeploymentOwner, user_id) is None:
            session.add(
                DeploymentOwner(
                    user_id=user_id,
                    added_at=now,
                    added_by_user_id=None,
                )
            )
        session.flush()


def _resolve_or_create_user(
    session: SqlaSession,
    *,
    email: str,
    display_name: str,
) -> User:
    email_lower = canonicalise_email(email)
    with tenant_agnostic():
        existing = session.scalar(select(User).where(User.email_lower == email_lower))
        if existing is not None:
            return existing
        now = SystemClock().now()
        user = User(
            id=new_ulid(),
            email=email,
            email_lower=email_lower,
            display_name=display_name,
            locale=None,
            timezone=None,
            created_at=now,
        )
        session.add(user)
        session.flush()
        return user


def _ensure_revokable_admin_grant(
    session: SqlaSession,
    *,
    actor_user_id: str,
) -> str:
    target = _resolve_or_create_user(
        session,
        email=_ADMIN_REVOKE_EMAIL,
        display_name="Schemathesis Admin Revoke Target",
    )
    with tenant_agnostic():
        existing_grant = session.scalar(
            select(RoleGrant)
            .where(RoleGrant.scope_kind == "deployment")
            .where(RoleGrant.user_id == target.id)
            .where(RoleGrant.grant_role == "manager")
            .where(RoleGrant.revoked_at.is_(None))
            .limit(1)
        )
        if existing_grant is not None:
            return existing_grant.id

        grant = RoleGrant(
            id=new_ulid(),
            workspace_id=None,
            user_id=target.id,
            grant_role="manager",
            scope_kind="deployment",
            created_at=SystemClock().now(),
            created_by_user_id=actor_user_id,
        )
        session.add(grant)
        session.flush()
        return grant.id


def _ensure_contract_vapid_public_key(workspace: Workspace) -> None:
    """Seed the public VAPID key expected by messaging contract runs."""
    settings = dict(workspace.settings_json or {})
    if settings.get(SETTINGS_KEY_VAPID_PUBLIC):
        return
    settings[SETTINGS_KEY_VAPID_PUBLIC] = _CONTRACT_VAPID_PUBLIC_KEY
    workspace.settings_json = settings


def _ensure_contract_notification(
    session: SqlaSession,
    *,
    workspace_id: str,
    user_id: str,
) -> str:
    with tenant_agnostic():
        existing_id = session.scalar(
            select(Notification.id)
            .where(Notification.workspace_id == workspace_id)
            .where(Notification.recipient_user_id == user_id)
            .order_by(Notification.id.asc())
            .limit(1)
        )
        if existing_id is not None:
            return existing_id

        notification = Notification(
            id=new_ulid(),
            workspace_id=workspace_id,
            recipient_user_id=user_id,
            kind="agent_message",
            subject="Schemathesis contract notification",
            body_md="Seed row for notification detail contract coverage.",
            read_at=None,
            created_at=SystemClock().now(),
            payload_json={},
        )
        session.add(notification)
        session.flush()
        return notification.id


def seed_contract_path_resources(
    session: SqlaSession,
    *,
    actor_user_id: str,
    workspace_id: str,
) -> ContractPathResources:
    """Seed live path resources consumed by Schemathesis hooks."""
    seed_agent_docs(session)
    with tenant_agnostic():
        agent_doc_slug = session.scalar(
            select(AgentDoc.slug)
            .where(AgentDoc.is_active.is_(True))
            .order_by(AgentDoc.slug.asc())
            .limit(1)
        )
    if agent_doc_slug is None:
        raise RuntimeError("no active agent docs available for schemathesis seed")
    return ContractPathResources(
        workspace_id=workspace_id,
        admin_revoke_grant_id=_ensure_revokable_admin_grant(
            session,
            actor_user_id=actor_user_id,
        ),
        agent_doc_slug=agent_doc_slug,
        notification_id=_ensure_contract_notification(
            session,
            workspace_id=workspace_id,
            user_id=actor_user_id,
        ),
    )


def _format_contract_env(resources: ContractPathResources) -> str:
    return "\n".join(
        (
            f"CREWDAY_SCHEMATHESIS_ADMIN_WORKSPACE_ID={resources.workspace_id}",
            "CREWDAY_SCHEMATHESIS_ADMIN_REVOKE_GRANT_ID="
            f"{resources.admin_revoke_grant_id}",
            f"CREWDAY_SCHEMATHESIS_ADMIN_AGENT_DOC_SLUG={resources.agent_doc_slug}",
            f"CREWDAY_SCHEMATHESIS_NOTIFICATION_ID={resources.notification_id}",
        )
    )


def _check_gates() -> None:
    """Refuse to run unless the dev-auth gates are green.

    Mirrors :mod:`scripts.dev_login`'s gate set so a single env switch
    blocks both helpers.
    """
    raw = os.environ.get(_DEV_AUTH_ENV_VAR, "0").lower()
    if raw not in {"1", "yes", "true"}:
        raise SystemExit(
            f"error: {_DEV_AUTH_ENV_VAR} not set to 1/yes/true; refusing to run."
        )
    settings = get_settings()
    if settings.profile != "dev":
        raise SystemExit(
            f"error: CREWDAY_PROFILE={settings.profile!r} — schemathesis seed "
            "requires profile=dev."
        )
    scheme = settings.database_url.split(":", 1)[0].lower()
    if not scheme.startswith("sqlite"):
        raise SystemExit(
            f"error: database_url scheme {scheme!r} is not SQLite; refusing."
        )


def mint_seed_session_cookie_value(*, email: str, workspace_slug: str) -> str:
    """Return a dev-login session cookie value for the schemathesis seed actor."""
    _check_gates()
    session_result = mint_session(
        email=email,
        workspace_slug=workspace_slug,
        role="owner",
    )
    return session_result.session_issue.cookie_value


@click.command(
    help=(
        "Dev-only: seed a workspace + mint a Bearer token + dev session "
        "for schemathesis."
    )
)
@click.option(
    "--email", default="schemathesis@dev.local", help="Dev-login email address."
)
@click.option(
    "--workspace",
    "workspace_slug",
    default="schemathesis",
    help="Workspace slug (created if missing).",
)
@click.option("--label", default="schemathesis", help="Token audit label.")
@click.option(
    "--output",
    type=click.Choice(["token", "bearer", "session", "cookie", "admin-contract-env"]),
    default="token",
    help="Output format. 'token' prints the API-token plaintext only; "
    "'bearer' prefixes with 'Authorization: Bearer '. 'session' prints the "
    "session cookie value only; 'cookie' prints the full "
    "'__Host-crewday_session=<value>' pair; 'admin-contract-env' prints "
    "KEY=value lines consumed by the Schemathesis hooks.",
)
def main(email: str, workspace_slug: str, label: str, output: OutputFormat) -> None:
    """CLI entry point — gate, seed, mint, print plaintext."""
    _check_gates()

    # 1. Drive the dev-login flow so user + workspace + role grants
    #    + the 4 system permission groups exist. The session is also
    #    surfaced — the runner uses it for bare-host paths that the
    #    workspace Bearer token can't reach.
    session_cookie_value = mint_seed_session_cookie_value(
        email=email,
        workspace_slug=workspace_slug,
    )

    if output == "session":
        sys.stdout.write(session_cookie_value)
        return
    if output == "cookie":
        sys.stdout.write(f"{_SESSION_COOKIE_NAME}={session_cookie_value}")
        return

    # 2. Resolve the row ids the token mint needs. The dev-login
    #    helper is idempotent on (email, workspace_slug); we look the
    #    rows up with a fresh UoW so the token mint runs in its own
    #    transaction. Keep the lookups under :func:`tenant_agnostic`
    #    because both queries hit identity / workspace tables that
    #    pre-date any :class:`WorkspaceContext` we'd otherwise
    #    install.
    email_lower = canonicalise_email(email)
    with make_uow() as uow:
        assert isinstance(uow, SqlaSession)
        with tenant_agnostic():
            user = uow.scalars(
                select(User).where(User.email_lower == email_lower)
            ).one()
            workspace = uow.scalars(
                select(Workspace).where(Workspace.slug == workspace_slug)
            ).one()
            _ensure_deployment_admin(uow, user_id=user.id)
            _ensure_contract_vapid_public_key(workspace)

        if output == "admin-contract-env":
            resources = seed_contract_path_resources(
                uow,
                actor_user_id=user.id,
                workspace_id=workspace.id,
            )
            sys.stdout.write(_format_contract_env(resources))
            return

        # 3. Mint a workspace-scoped token. ``scopes`` is left empty
        #    on purpose — empty-scope workspace tokens are the v1
        #    contract (`docs/specs/03-auth-and-tokens.md` §"Scopes":
        #    "Empty is allowed on v1"); the token still resolves
        #    capabilities through the user's role grants. That gives
        #    the schemathesis fuzzer a token that exercises every
        #    operation a real owner would.
        ctx = WorkspaceContext(
            workspace_id=workspace.id,
            workspace_slug=workspace_slug,
            actor_id=user.id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id=new_ulid(),
        )
        result = mint_token(
            uow,
            ctx,
            user_id=user.id,
            label=label,
            scopes={},
            expires_at=None,
            kind="scoped",
            now=SystemClock().now(),
        )
        plaintext = result.token

    if output == "token":
        sys.stdout.write(plaintext)
    else:  # output == "bearer"
        sys.stdout.write(f"Authorization: Bearer {plaintext}")


if __name__ == "__main__":
    main()
