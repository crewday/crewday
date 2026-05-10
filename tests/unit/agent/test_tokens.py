"""Tests for embedded-agent delegated token lifecycle helpers."""

from __future__ import annotations

from typing import Literal
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from app.agent.tokens import DelegatedTokenFactory
from app.tenancy import WorkspaceContext


class _CapturingTokenFactory(DelegatedTokenFactory):
    def __init__(self, *, mode: Literal["success", "fail"]) -> None:
        super().__init__()
        self.mode = mode
        self.seen: list[tuple[str, ...]] = []

    def _revoke_with_session(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        token_ids: tuple[str, ...],
    ) -> None:
        del session, ctx
        self.seen.append(token_ids)
        if self.mode == "fail":
            raise RuntimeError("database unavailable")


def test_revoke_minted_keeps_token_ids_when_revoke_fails() -> None:
    factory = _CapturingTokenFactory(mode="fail")
    factory._minted_token_ids.append("tok_1")
    session = Mock(spec=Session)
    ctx = WorkspaceContext(
        workspace_id="ws_1",
        workspace_slug="demo",
        actor_id="user_1",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="corr_1",
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        factory.revoke_minted(ctx, session=session)

    assert factory.seen == [("tok_1",)]
    assert factory._minted_token_ids == ["tok_1"]


def test_revoke_minted_clears_token_ids_after_success() -> None:
    factory = _CapturingTokenFactory(mode="success")
    factory._minted_token_ids.extend(["tok_1", "tok_2"])
    session = Mock(spec=Session)
    ctx = WorkspaceContext(
        workspace_id="ws_1",
        workspace_slug="demo",
        actor_id="user_1",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="corr_1",
    )

    factory.revoke_minted(ctx, session=session)

    assert factory.seen == [("tok_1", "tok_2")]
    assert factory._minted_token_ids == []
