"""WorkspaceContext — canonical per-request tenancy + actor record.

Every domain service function accepts a :class:`WorkspaceContext` as its
first argument. The context is resolved once per request by the tenancy
middleware and passed down into every service call and repository method.

See ``docs/specs/01-architecture.md`` §"WorkspaceContext".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["ActorGrantRole", "ActorKind", "PrincipalKind", "WorkspaceContext"]


ActorKind = Literal["user", "agent", "system"]
ActorGrantRole = Literal["manager", "worker", "client", "guest"]

# §03 "Delegated tokens" / "Personal access tokens" both pin "can only
# be created by a passkey session — cannot be created by another token
# (no transitive delegation)". The route-level guard needs to tell the
# three caller arms apart at the seam:
#
# * ``"session"`` — the request carried the
#   ``__Host-crewday_session`` cookie (the tenancy middleware's
#   :func:`resolve_actor` cookie branch).
# * ``"token"`` — the request carried an ``Authorization: Bearer
#   mip_<key_id>_<secret>`` header (the bearer-token branch). Covers
#   ``scoped`` / ``delegated`` / ``personal`` token kinds; routes that
#   need to refuse a specific kind can additionally branch on the
#   verified token's ``kind`` via the actor.
# * ``"demo"`` — the request carried a signed demo binding cookie in
#   ``CREWDAY_DEMO_MODE``. It acts as a seeded user inside one demo
#   workspace, but it is not a passkey session and must not satisfy
#   session-only surfaces such as token minting.
# * ``"system"`` — synthesised by a worker / job / signup helper that
#   has no live caller (e.g. cd-yqm4's reconciler, the magic-link
#   issuer, the dev-login script). Sites that exercise governance
#   guards must either reject these or surface the system actor in
#   audit; the field exists so they can branch deliberately.
#
# Mirrors :attr:`ActorKind` in spirit but is **orthogonal**: ``actor_kind``
# stamps audit ("a user did this") while ``principal_kind`` records the
# transport / authentication source ("the user proved that via a session
# cookie"). The two values agree for the common case (session ⇒ user,
# system worker ⇒ system) but diverge for token paths
# (``actor_kind="user"`` + ``principal_kind="token"``) — only with the
# transport bit can routes refuse a transitive delegated mint.
PrincipalKind = Literal["session", "token", "demo", "system"]


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    """Canonical per-request tenancy + actor record.

    Immutable: equality compares every field. The middleware builds one
    instance per request; downstream services consume it read-only.

    ``principal_kind`` records the transport that authenticated the
    caller — ``"session"`` (passkey cookie), ``"token"`` (bearer
    header), ``"demo"`` (signed demo binding cookie), or ``"system"``
    (worker / signup helper / magic-link issuer). It defaults to
    ``"session"`` so existing call sites
    (factories, fixtures, domain helpers that build a synthetic ctx)
    behave as if they were a session-presented request — the field
    only carries weight for routes that gate on transport (e.g.
    delegated-mint refusal of token-presented callers per §03
    "Delegated tokens"). The middleware overrides the default on
    every request from the resolver branch that fired (token /
    cookie); system actors override at construction time. Keep the
    default conservative: ``"session"`` is the most-permissive
    transport, so a misconfigured caller (no override) errs on the
    side of being usable rather than silently locked out.
    """

    workspace_id: str
    workspace_slug: str
    actor_id: str
    actor_kind: ActorKind
    actor_grant_role: ActorGrantRole
    actor_was_owner_member: bool
    audit_correlation_id: str
    principal_kind: PrincipalKind = "session"
