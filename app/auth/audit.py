"""Shared audit-context factory for tenant-agnostic identity events.

Six bare-host identity surfaces (signup, magic-link, recovery, session,
email-change, ``/me/avatar``, ``/me/tokens``) emit audit rows before
any workspace exists, or for actions whose subject is the identity
itself rather than workspace data. They all need the same synthetic
:class:`~app.tenancy.WorkspaceContext`: zero-ULID workspace + zero-ULID
actor + ``actor_kind="system"`` + ``principal_kind="system"`` so the
audit reader recognises the row as a pre-tenant identity event and
workspace-scoped views naturally exclude it.

This module is the single source of truth for that shape. Every
caller imports :func:`agnostic_audit_ctx` here instead of redefining
it; cd-rqhy consolidated six byte-identical copies that had drifted
into separate modules.

Different shape — :func:`app.auth.tokens._pat_audit_ctx`. PATs use the
**real** subject user as the actor (not the zero-ULID) so the ``/me``
audit view can filter on ``actor_id`` without a JSON scan. That helper
stays in :mod:`app.auth.tokens`; it is intentionally not consolidated
here because the actor identity is part of its contract.

See ``docs/specs/03-auth-and-tokens.md`` §"Audit" for the canonical
list of bare-host identity actions and ``docs/specs/15-security-
privacy.md`` §"Audit log" for the row shape.
"""

from __future__ import annotations

from typing import Final

from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid

__all__ = [
    "AGNOSTIC_ACTOR_ID",
    "AGNOSTIC_WORKSPACE_ID",
    "agnostic_audit_ctx",
]


# Zero-ULID sentinels. The audit reader recognises a 26-zero workspace
# id as "identity-scope event" so workspace-scoped views naturally
# filter it out. Both fields use the same sentinel because every
# bare-host identity event (signup, recovery, magic-link, session,
# email-change, avatar) lacks both a workspace and a bound user at
# the moment of emission. PAT mint / revoke uses
# :func:`app.auth.tokens._pat_audit_ctx` instead — that helper carries
# the real subject user as the actor.
AGNOSTIC_WORKSPACE_ID: Final[str] = "0" * 26
AGNOSTIC_ACTOR_ID: Final[str] = "0" * 26


def agnostic_audit_ctx() -> WorkspaceContext:
    """Return a synthetic :class:`WorkspaceContext` for identity-scope audit rows.

    Six bare-host writers (signup, magic-link, recovery, session,
    email-change, ``/me/avatar``, ``/me/tokens``) call this to build a
    tenant-agnostic context with:

    * ``workspace_id`` / ``actor_id`` pinned at the 26-zero ULID
      sentinel — workspace-scoped audit views filter it out.
    * ``actor_kind="system"`` + ``principal_kind="system"`` — the row
      represents a pre-tenant or identity-scope event without a bound
      user at emission time.
    * A fresh ``audit_correlation_id`` per call so sibling writes
      (rare — most identity flows emit one row) get their own trace
      cursor.

    ``actor_grant_role`` is unused for system actors; ``"manager"`` is
    the neutral default the field expects.

    The acting user's id, when one exists, rides in the ``diff``
    payload — see :mod:`app.api.v1.auth.me_avatar` /
    :mod:`app.api.v1.auth.me_tokens` for the ``user_id`` /
    ``before_hash`` / ``after_hash`` shape.
    """
    return WorkspaceContext(
        workspace_id=AGNOSTIC_WORKSPACE_ID,
        workspace_slug="",
        actor_id=AGNOSTIC_ACTOR_ID,
        actor_kind="system",
        actor_grant_role="manager",  # unused for system actors
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
        principal_kind="system",
    )
