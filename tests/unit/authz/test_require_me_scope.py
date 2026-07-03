"""Unit tests — :func:`app.authz.enforce.require_me_scope` (cd-fktzw).

The ``me.*`` scope gate for the PAT-reachable ``/w/<slug>/api/v1/me/...``
self-service routes. Proves the principal matrix §03 "Scopes" pins:

* scope-bearing tokens (``personal`` / ``scoped``) must hold the route's
  ``me.*`` scope, honouring the single ``me.<r>:read`` ⊂ ``me.<r>:write``
  implication;
* a scoped workspace token — which can never hold a ``me.*`` scope — is
  refused, keeping the ``me.*`` surface reserved for PATs;
* every non-scope-bearing principal (session, demo, system, and the
  scope-ignoring **delegated** token) is a no-op, so cookie/session and
  delegated self-service is unaffected by the gate.
"""

from __future__ import annotations

import pytest

from app.authz.enforce import InsufficientScope, require_me_scope
from app.tenancy import WorkspaceContext


def _ctx(
    *,
    principal_kind: str = "token",
    token_kind: str | None = None,
    token_scopes: frozenset[str] = frozenset(),
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id="ws-1",
        workspace_slug="acme",
        actor_id="user-alice",
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
        audit_correlation_id="corr-1",
        principal_kind=principal_kind,  # type: ignore[arg-type]
        token_kind=token_kind,
        token_scopes=token_scopes,
    )


class TestPersonalTokenGating:
    """A PAT must hold the exact ``me.*`` scope the route declares."""

    def test_pat_with_scope_passes(self) -> None:
        ctx = _ctx(token_kind="personal", token_scopes=frozenset({"me.tasks:read"}))
        # No raise == allowed.
        require_me_scope(ctx, required_scope="me.tasks:read")

    def test_pat_missing_scope_raises(self) -> None:
        ctx = _ctx(token_kind="personal", token_scopes=frozenset({"me.tasks:read"}))
        with pytest.raises(InsufficientScope) as exc:
            require_me_scope(ctx, required_scope="me.expenses:read")
        assert exc.value.required_scope == "me.expenses:read"
        # The scope string doubles as the action_key so the single-scope
        # wire envelope stays informative.
        assert exc.value.action_key == "me.expenses:read"

    def test_write_scope_implies_read(self) -> None:
        ctx = _ctx(token_kind="personal", token_scopes=frozenset({"me.expenses:write"}))
        # me.expenses:write covers the me.expenses:read requirement…
        require_me_scope(ctx, required_scope="me.expenses:read")
        require_me_scope(ctx, required_scope="me.expenses:write")

    def test_read_scope_does_not_imply_write(self) -> None:
        ctx = _ctx(token_kind="personal", token_scopes=frozenset({"me.expenses:read"}))
        with pytest.raises(InsufficientScope):
            require_me_scope(ctx, required_scope="me.expenses:write")


class TestReservedForPersonalTokens:
    """A scoped workspace token can never satisfy a ``me.*`` requirement."""

    def test_scoped_token_denied(self) -> None:
        # A scoped token holds workspace scopes; §03 forbids mixing in a
        # me.* scope, so it can never pass the me.* gate.
        ctx = _ctx(token_kind="scoped", token_scopes=frozenset({"tasks:read"}))
        with pytest.raises(InsufficientScope):
            require_me_scope(ctx, required_scope="me.tasks:read")


class TestNonScopeBearingPrincipalsUngated:
    """Sessions, demo, system, and delegated tokens skip the scope gate."""

    def test_session_is_noop(self) -> None:
        ctx = _ctx(principal_kind="session", token_kind=None)
        # A session holds no token_scopes but is un-gated — its only
        # confinement is the route's own ctx.actor_id self-keying.
        require_me_scope(ctx, required_scope="me.expenses:write")

    def test_delegated_token_is_noop(self) -> None:
        # Delegated tokens ignore scopes and inherit the delegating
        # user's full grants (§03), so the me.* gate must not block them.
        ctx = _ctx(
            principal_kind="token", token_kind="delegated", token_scopes=frozenset()
        )
        require_me_scope(ctx, required_scope="me.tasks:read")

    @pytest.mark.parametrize("principal_kind", ["demo", "system"])
    def test_other_principals_are_noop(self, principal_kind: str) -> None:
        ctx = _ctx(principal_kind=principal_kind, token_kind=None)
        require_me_scope(ctx, required_scope="me.profile:write")
