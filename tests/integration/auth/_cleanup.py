"""Shared cleanup helpers for auth integration tests."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, or_
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.adapters.db.identity.models import ApiToken


def delete_api_tokens_for_scope(
    session: Session | Connection,
    *,
    workspace_ids: Sequence[str] = (),
    user_ids: Sequence[str] = (),
) -> None:
    """Delete tokens tied to the given test-owned workspaces or users."""
    clauses: list[ColumnElement[bool]] = []
    if workspace_ids:
        clauses.append(ApiToken.workspace_id.in_(workspace_ids))
    if user_ids:
        clauses.extend(
            (
                ApiToken.user_id.in_(user_ids),
                ApiToken.delegate_for_user_id.in_(user_ids),
                ApiToken.subject_user_id.in_(user_ids),
            )
        )
    if clauses:
        session.execute(delete(ApiToken).where(or_(*clauses)))
