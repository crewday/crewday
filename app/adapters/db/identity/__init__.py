"""identity — users, passkey credentials, sessions, API tokens.

Importing this package registers per-table tenancy behaviour:

* ``user``: NOT workspace-scoped (one row per human, globally unique
  email). Access is governed by ``role_grants`` via workspace
  membership, not a tenant filter on the users table itself.
* ``passkey_credential``, ``session``, ``api_token``,
  ``user_push_token``: user-scoped. They carry a ``workspace_id``
  only where relevant (``session``, ``api_token``); they are NOT
  registered as workspace-scoped tables because the primary access
  pattern is ``user_id``. The domain layer (cd-cyq session, cd-c91
  tokens, cd-nq9s push tokens) owns their tenancy.

Skipping scope registration is deliberate: sign-in runs before any
:class:`~app.tenancy.WorkspaceContext` exists (the ceremony picks
the user first, the workspace second), so forcing a tenant filter
on these tables would make the login-before-workspace-pick flow
impossible.

See ``docs/specs/02-domain-model.md`` §"users" / §"passkey_credential"
/ §"session" / §"api_token" and ``docs/specs/03-auth-and-tokens.md``
§"Data model".
"""

from __future__ import annotations

from app.adapters.db.identity.models import (
    ApiToken,
    BreakGlassCode,
    Invite,
    MagicLinkNonce,
    PasskeyCredential,
    Session,
    SignupAttempt,
    User,
    UserPushToken,
    WebAuthnChallenge,
)
from app.tenancy.registry import register

# ``invite`` carries a ``workspace_id`` and is always queried under a
# live :class:`~app.tenancy.WorkspaceContext` once the manager is
# authenticated. The ORM tenant filter auto-injects the predicate on
# every SELECT / UPDATE / DELETE so a misconfigured membership
# service can't leak a sibling workspace's invites. The accept flow
# at the bare host wraps its lookup in :func:`app.tenancy.tenant_agnostic`
# because the redeemed token has not yet resolved a workspace ctx.
register("invite")

# ``break_glass_code`` is workspace-scoped at *management* time (a
# future ``/me/security`` panel + the workspace bootstrap ritual mint
# codes under the active :class:`~app.tenancy.WorkspaceContext`) but
# tenant-agnostic at *redemption* time. The recovery flow runs before
# any workspace ctx is resolved (the user may belong to any number of
# workspaces and we don't pick one at recovery time), so the
# redemption helper in :mod:`app.auth.break_glass` wraps its
# read/write in :func:`app.tenancy.tenant_agnostic` — same pattern
# the invite-accept flow uses on the bare host.
register("break_glass_code")

__all__ = [
    "ApiToken",
    "BreakGlassCode",
    "Invite",
    "MagicLinkNonce",
    "PasskeyCredential",
    "Session",
    "SignupAttempt",
    "User",
    "UserPushToken",
    "WebAuthnChallenge",
]
