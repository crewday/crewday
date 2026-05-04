"""Delegated-token factory for embedded agent turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.adapters.db.session import make_uow
from app.auth import tokens as auth_tokens
from app.domain.agent.runtime import DelegatedToken
from app.tenancy import WorkspaceContext
from app.util.clock import Clock

__all__ = ["DelegatedTokenFactory"]


@dataclass(slots=True)
class DelegatedTokenFactory:
    """Mint short-lived delegated tokens for one agent turn.

    ``session=None`` uses an independent UoW, which commits the token before
    the in-process dispatcher opens the nested request that verifies it.
    Tests may pass their pinned session when they override route dependencies.
    """

    session: Session | None = None
    clock: Clock | None = None
    _minted_token_ids: list[str] = field(default_factory=list, init=False)

    def mint_for(
        self,
        ctx: WorkspaceContext,
        *,
        agent_label: str,
        expires_at: datetime,
    ) -> DelegatedToken:
        if self.session is not None:
            return self._mint_with_session(
                self.session,
                ctx,
                agent_label=agent_label,
                expires_at=expires_at,
            )
        with make_uow() as session:
            assert isinstance(session, Session)
            return self._mint_with_session(
                session,
                ctx,
                agent_label=agent_label,
                expires_at=expires_at,
            )

    def revoke_minted(self, ctx: WorkspaceContext) -> None:
        """Revoke tokens minted by this factory after the turn finishes."""
        if not self._minted_token_ids:
            return
        token_ids = tuple(self._minted_token_ids)
        self._minted_token_ids.clear()
        if self.session is not None:
            self._revoke_with_session(self.session, ctx, token_ids=token_ids)
            return
        with make_uow() as session:
            assert isinstance(session, Session)
            self._revoke_with_session(session, ctx, token_ids=token_ids)

    def _mint_with_session(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        agent_label: str,
        expires_at: datetime,
    ) -> DelegatedToken:
        minted = auth_tokens.mint(
            session,
            ctx,
            user_id=ctx.actor_id,
            label=agent_label,
            scopes={},
            expires_at=expires_at,
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            clock=self.clock,
        )
        self._minted_token_ids.append(minted.key_id)
        return DelegatedToken(plaintext=minted.token, token_id=minted.key_id)

    def _revoke_with_session(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        token_ids: tuple[str, ...],
    ) -> None:
        for token_id in token_ids:
            auth_tokens.revoke(session, ctx, token_id=token_id, clock=self.clock)
