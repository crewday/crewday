"""Cleanup helpers for signup integration tests that commit outside savepoints."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import (
    DeploymentOwner,
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import (
    ApiToken,
    MagicLinkNonce,
    PasskeyCredential,
    SignupAttempt,
    User,
    WebAuthnChallenge,
)
from app.adapters.db.identity.models import (
    Session as AuthSession,
)
from app.adapters.db.llm.models import BudgetLedger
from app.adapters.db.workspace.models import UserWorkspace, Workspace


def delete_signup_rows(
    factory: sessionmaker[Session],
    *,
    emails: Sequence[str] = (),
    slugs: Sequence[str] = (),
    email_like: str | None = None,
    slug_like: str | None = None,
) -> None:
    """Delete only the committed signup rows matching the given identifiers."""
    with factory() as s:
        attempt_filters: list[ColumnElement[bool]] = []
        if emails:
            attempt_filters.append(SignupAttempt.email_lower.in_(emails))
        if slugs:
            attempt_filters.append(SignupAttempt.desired_slug.in_(slugs))
        if email_like is not None:
            attempt_filters.append(SignupAttempt.email_lower.like(email_like))
        if slug_like is not None:
            attempt_filters.append(SignupAttempt.desired_slug.like(slug_like))

        user_filters: list[ColumnElement[bool]] = []
        if emails:
            user_filters.append(User.email_lower.in_(emails))
        if email_like is not None:
            user_filters.append(User.email_lower.like(email_like))

        workspace_filters: list[ColumnElement[bool]] = []
        if slugs:
            workspace_filters.append(Workspace.slug.in_(slugs))
        if slug_like is not None:
            workspace_filters.append(Workspace.slug.like(slug_like))

        attempt_ids: list[str] = []
        workspace_ids: list[str] = []
        if attempt_filters:
            attempts = s.scalars(select(SignupAttempt).where(or_(*attempt_filters)))
            for attempt in attempts:
                attempt_ids.append(attempt.id)
                if attempt.workspace_id is not None:
                    workspace_ids.append(attempt.workspace_id)

        if workspace_filters:
            workspace_ids.extend(
                s.scalars(select(Workspace.id).where(or_(*workspace_filters))).all()
            )
        workspace_ids = list(dict.fromkeys(workspace_ids))

        user_ids: list[str] = []
        if user_filters:
            user_ids.extend(s.scalars(select(User.id).where(or_(*user_filters))).all())
        user_ids = list(dict.fromkeys(user_ids))

        group_ids: list[str] = []
        if workspace_ids:
            group_ids = list(
                s.scalars(
                    select(PermissionGroup.id).where(
                        PermissionGroup.workspace_id.in_(workspace_ids)
                    )
                )
            )

        _delete_where(
            s,
            PasskeyCredential,
            PasskeyCredential.user_id.in_(user_ids) if user_ids else None,
        )
        _delete_where(
            s,
            AuthSession,
            AuthSession.user_id.in_(user_ids) if user_ids else None,
            AuthSession.workspace_id.in_(workspace_ids) if workspace_ids else None,
        )
        _delete_where(
            s,
            ApiToken,
            ApiToken.user_id.in_(user_ids) if user_ids else None,
            ApiToken.workspace_id.in_(workspace_ids) if workspace_ids else None,
        )
        _delete_where(
            s,
            WebAuthnChallenge,
            WebAuthnChallenge.user_id.in_(user_ids) if user_ids else None,
            WebAuthnChallenge.signup_session_id.in_(attempt_ids)
            if attempt_ids
            else None,
        )
        _delete_where(
            s,
            MagicLinkNonce,
            MagicLinkNonce.subject_id.in_(attempt_ids) if attempt_ids else None,
        )
        _delete_where(
            s,
            SignupAttempt,
            SignupAttempt.id.in_(attempt_ids) if attempt_ids else None,
        )
        _delete_where(
            s,
            PermissionGroupMember,
            PermissionGroupMember.group_id.in_(group_ids) if group_ids else None,
            PermissionGroupMember.user_id.in_(user_ids) if user_ids else None,
            PermissionGroupMember.workspace_id.in_(workspace_ids)
            if workspace_ids
            else None,
        )
        _delete_where(
            s,
            RoleGrant,
            RoleGrant.user_id.in_(user_ids) if user_ids else None,
            RoleGrant.workspace_id.in_(workspace_ids) if workspace_ids else None,
        )
        _delete_where(
            s,
            UserWorkspace,
            UserWorkspace.user_id.in_(user_ids) if user_ids else None,
            UserWorkspace.workspace_id.in_(workspace_ids) if workspace_ids else None,
        )
        _delete_where(
            s,
            PermissionGroup,
            PermissionGroup.id.in_(group_ids) if group_ids else None,
        )
        audit_entity_ids = [*attempt_ids, *workspace_ids, *user_ids]
        _delete_where(
            s,
            AuditLog,
            AuditLog.workspace_id.in_(workspace_ids) if workspace_ids else None,
            AuditLog.actor_id.in_(user_ids) if user_ids else None,
            AuditLog.entity_id.in_(audit_entity_ids) if audit_entity_ids else None,
        )
        _delete_where(
            s,
            DeploymentOwner,
            DeploymentOwner.user_id.in_(user_ids) if user_ids else None,
        )
        _delete_where(
            s,
            BudgetLedger,
            BudgetLedger.workspace_id.in_(workspace_ids) if workspace_ids else None,
        )
        _delete_where(
            s,
            Workspace,
            Workspace.id.in_(workspace_ids) if workspace_ids else None,
        )
        _delete_where(
            s,
            User,
            User.id.in_(user_ids) if user_ids else None,
        )
        s.commit()


def _delete_where(
    session: Session,
    model: type[object],
    *conditions: ColumnElement[bool] | None,
) -> None:
    clauses = [condition for condition in conditions if condition is not None]
    if clauses:
        session.execute(delete(model).where(or_(*clauses)))
