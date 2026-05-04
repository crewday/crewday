"""Agent preference storage and prompt resolution.

See ``docs/specs/11-llm-and-agents.md`` §"Agent preferences".
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import AgentPreference, AgentPreferenceRevision
from app.adapters.db.workspace.models import Workspace
from app.audit import write_audit
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.redact import CONSENT_TOKENS
from app.util.ulid import new_ulid

__all__ = [
    "AGENT_PREFERENCES_EMPTY_HEADER",
    "APPROVAL_MODES",
    "PREFERENCE_HARD_TOKEN_CAP",
    "PreferenceBundle",
    "PreferenceContainsSecret",
    "PreferenceTooLarge",
    "PreferenceUpdate",
    "blocked_action_result_body",
    "default_approval_mode_for_workspace",
    "is_action_blocked",
    "read_preference",
    "read_workspace_upstream_pii_consent",
    "resolve_preferences",
    "save_preference",
    "save_workspace_upstream_pii_consent",
]

ScopeKind = Literal["workspace", "property", "user"]
ApprovalMode = Literal["bypass", "auto", "strict"]
ConsentToken = Literal["legal_name", "email", "phone", "address"]

APPROVAL_MODES: tuple[ApprovalMode, ...] = ("bypass", "auto", "strict")
CONSENT_TOKEN_ORDER: tuple[ConsentToken, ...] = (
    "legal_name",
    "email",
    "phone",
    "address",
)
if frozenset(CONSENT_TOKEN_ORDER) != CONSENT_TOKENS:  # pragma: no cover
    raise RuntimeError("CONSENT_TOKEN_ORDER must match app.util.redact.CONSENT_TOKENS")
PREFERENCE_HARD_TOKEN_CAP = 16_000
INJECTION_TOKEN_CAP = 8_000
AGENT_PREFERENCES_EMPTY_HEADER = "## Agent preferences\n(none)"

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    re.compile(r"\bmip_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\b(?:bearer|oauth)\s+[A-Za-z0-9._~+/=-]{16,}\b", re.I),
    re.compile(r"\b(?:door|alarm|access|wifi|wi-fi)\s+code\b", re.I),
    re.compile(r"\b(?:password|passwd|pwd)\s*[:=]\s*\S+", re.I),
)


@dataclass(frozen=True, slots=True)
class PreferenceUpdate:
    """Validated preference payload to persist."""

    body_md: str
    blocked_actions: tuple[str, ...] = ()
    default_approval_mode: ApprovalMode = "auto"
    change_note: str | None = None


@dataclass(frozen=True, slots=True)
class PreferenceBundle:
    """Resolved prompt text and structured controls for one agent turn."""

    text: str
    blocked_actions: tuple[str, ...]
    default_approval_mode: ApprovalMode


class PreferenceTooLarge(ValueError):
    """Preference body exceeds the hard save-time token cap."""


class PreferenceContainsSecret(ValueError):
    """Preference body matched a hard-drop secret pattern."""


def read_preference(
    session: Session,
    ctx: WorkspaceContext,
    *,
    scope_kind: ScopeKind,
    scope_id: str,
) -> AgentPreference | None:
    """Return the latest non-archived preference row for ``scope``."""
    stmt = select(AgentPreference).where(
        AgentPreference.workspace_id == ctx.workspace_id,
        AgentPreference.scope_kind == scope_kind,
        AgentPreference.scope_id == scope_id,
        AgentPreference.archived_at.is_(None),
    )
    return session.scalar(stmt)


def save_preference(
    session: Session,
    ctx: WorkspaceContext,
    *,
    scope_kind: ScopeKind,
    scope_id: str,
    update: PreferenceUpdate,
    actor_user_id: str,
    clock: Clock | None = None,
) -> AgentPreference:
    """Upsert a preference row, append a revision, and audit the diff."""
    eff_clock = clock if clock is not None else SystemClock()
    now = eff_clock.now()
    body_md = update.body_md.strip()
    _raise_if_secret(body_md)
    token_count = _estimate_tokens(body_md)
    if token_count > PREFERENCE_HARD_TOKEN_CAP:
        raise PreferenceTooLarge("preference_too_large")

    blocked_actions = _normalise_blocked_actions(update.blocked_actions)
    row = read_preference(session, ctx, scope_kind=scope_kind, scope_id=scope_id)
    before = _snapshot(row)
    if row is None:
        row = AgentPreference(
            id=new_ulid(clock=eff_clock),
            workspace_id=ctx.workspace_id,
            scope_kind=scope_kind,
            scope_id=scope_id,
            body_md=body_md,
            token_count=token_count,
            blocked_actions=list(blocked_actions),
            default_approval_mode=update.default_approval_mode,
            updated_by_user_id=actor_user_id,
            created_at=now,
            updated_at=now,
            archived_at=None,
        )
        session.add(row)
    else:
        row.body_md = body_md
        row.token_count = token_count
        row.blocked_actions = list(blocked_actions)
        row.default_approval_mode = update.default_approval_mode
        row.updated_by_user_id = actor_user_id
        row.updated_at = now

    session.flush()
    session.add(
        AgentPreferenceRevision(
            id=new_ulid(clock=eff_clock),
            workspace_id=ctx.workspace_id,
            preference_id=row.id,
            body_md=body_md,
            token_count=token_count,
            blocked_actions=list(blocked_actions),
            default_approval_mode=update.default_approval_mode,
            updated_by_user_id=actor_user_id,
            change_note=update.change_note,
            created_at=now,
        )
    )
    write_audit(
        session,
        ctx,
        entity_kind="agent_preference",
        entity_id=row.id,
        action="agent_preference.updated",
        diff={"before": before, "after": _snapshot(row)},
        clock=eff_clock,
    )
    return row


def read_workspace_upstream_pii_consent(
    session: Session,
    ctx: WorkspaceContext,
) -> tuple[ConsentToken, ...]:
    """Return the workspace's effective upstream PII consent tokens."""
    row = read_preference(
        session,
        ctx,
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
    )
    if row is None:
        return ()
    return _normalise_consent(row.upstream_pii_consent)


def save_workspace_upstream_pii_consent(
    session: Session,
    ctx: WorkspaceContext,
    *,
    tokens: Sequence[str],
    actor_user_id: str,
    clock: Clock | None = None,
) -> tuple[AgentPreference, bool]:
    """Upsert workspace upstream PII consent and audit effective changes."""
    eff_clock = clock if clock is not None else SystemClock()
    now = eff_clock.now()
    next_tokens = _normalise_consent(tokens)
    row = read_preference(
        session,
        ctx,
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
    )
    before = _normalise_consent(row.upstream_pii_consent) if row is not None else ()
    changed = before != next_tokens

    if row is None:
        row = AgentPreference(
            id=new_ulid(clock=eff_clock),
            workspace_id=ctx.workspace_id,
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
            body_md="",
            token_count=0,
            blocked_actions=[],
            default_approval_mode="auto",
            upstream_pii_consent=list(next_tokens),
            updated_by_user_id=actor_user_id,
            created_at=now,
            updated_at=now,
            archived_at=None,
        )
        session.add(row)
    elif changed or tuple(row.upstream_pii_consent) != next_tokens:
        row.upstream_pii_consent = list(next_tokens)
        row.updated_by_user_id = actor_user_id
        row.updated_at = now

    session.flush()
    if changed:
        write_audit(
            session,
            ctx,
            entity_kind="agent_preference",
            entity_id=row.id,
            action="agent_preference.upstream_pii_consent.updated",
            diff={
                "before": {"upstream_pii_consent": list(before)},
                "after": {"upstream_pii_consent": list(next_tokens)},
            },
            clock=eff_clock,
        )
    return row, changed


def resolve_preferences(
    session: Session,
    ctx: WorkspaceContext,
    *,
    capability: str,
    property_ids: Sequence[str] = (),
    user_id: str | None = None,
) -> PreferenceBundle:
    """Resolve workspace → property → user prompt text for ``capability``."""
    if not _capability_receives_preferences(capability):
        return PreferenceBundle(
            text=AGENT_PREFERENCES_EMPTY_HEADER,
            blocked_actions=(),
            default_approval_mode=default_approval_mode_for_workspace(session, ctx),
        )

    workspace = read_preference(
        session,
        ctx,
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
    )
    rows: list[tuple[str, AgentPreference | None]] = [
        (_workspace_heading(session, ctx), workspace)
    ]
    for property_id in property_ids:
        rows.append(
            (
                f"## Property preferences -- {property_id}",
                read_preference(
                    session,
                    ctx,
                    scope_kind="property",
                    scope_id=property_id,
                ),
            )
        )
    if user_id is not None:
        rows.append(
            (
                f"## Your preferences -- {user_id}",
                read_preference(
                    session,
                    ctx,
                    scope_kind="user",
                    scope_id=user_id,
                ),
            )
        )

    if not any(row is not None and row.body_md for _heading, row in rows):
        text = AGENT_PREFERENCES_EMPTY_HEADER
    else:
        text = "\n\n".join(_render_section(heading, row) for heading, row in rows)
    if _estimate_tokens(text) > INJECTION_TOKEN_CAP:
        text = _truncate_to_budget(text)

    return PreferenceBundle(
        text=text,
        blocked_actions=tuple(workspace.blocked_actions) if workspace else (),
        default_approval_mode=_coerce_approval_mode(
            workspace.default_approval_mode if workspace else "auto"
        ),
    )


def is_action_blocked(bundle: PreferenceBundle, action_key: str) -> bool:
    """Return whether ``action_key`` is denied by workspace preferences."""
    return action_key in set(bundle.blocked_actions)


def blocked_action_result_body(action_key: str) -> dict[str, str]:
    """Stable dispatcher-style body for preference-blocked actions."""
    return {
        "error": "action_blocked_by_preferences",
        "action_key": action_key,
    }


def default_approval_mode_for_workspace(
    session: Session, ctx: WorkspaceContext
) -> ApprovalMode:
    """Return the workspace default approval mode for future users."""
    row = read_preference(
        session,
        ctx,
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
    )
    if row is None:
        return "auto"
    value = row.default_approval_mode
    if value in APPROVAL_MODES:
        return value
    return "auto"


def _estimate_tokens(value: str) -> int:
    if not value:
        return 0
    return max(1, (len(value) + 3) // 4)


def _raise_if_secret(value: str) -> None:
    for pattern in _SECRET_PATTERNS:
        if pattern.search(value):
            raise PreferenceContainsSecret("preference_contains_secret")


def _normalise_blocked_actions(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    normalised: list[str] = []
    for raw in values:
        value = raw.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalised.append(value)
    return tuple(normalised)


def _normalise_consent(values: Sequence[str]) -> tuple[ConsentToken, ...]:
    raw = frozenset(value for value in values if value in CONSENT_TOKENS)
    return tuple(token for token in CONSENT_TOKEN_ORDER if token in raw)


def _coerce_approval_mode(value: str) -> ApprovalMode:
    if value in APPROVAL_MODES:
        return value
    return "auto"


def _snapshot(row: AgentPreference | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "scope_kind": row.scope_kind,
        "scope_id": row.scope_id,
        "body_md": row.body_md,
        "token_count": row.token_count,
        "blocked_actions": list(row.blocked_actions),
        "default_approval_mode": row.default_approval_mode,
    }


def _render_section(heading: str, row: AgentPreference | None) -> str:
    body = row.body_md if row is not None and row.body_md else "(none)"
    return f"{heading}\n{body}"


def _truncate_to_budget(text: str) -> str:
    max_chars = INJECTION_TOKEN_CAP * 4
    return f"{text[:max_chars].rstrip()}\n[truncated]"


def _capability_receives_preferences(capability: str) -> bool:
    return capability in {
        "chat.manager",
        "chat.employee",
        "chat.compact",
        "digest.manager",
        "digest.employee",
        "tasks.nl_intake",
        "tasks.assist",
        "instructions.draft",
        "stay.summarize",
        "issue.triage",
    }


def _workspace_heading(session: Session, ctx: WorkspaceContext) -> str:
    with tenant_agnostic():
        workspace = session.get(Workspace, ctx.workspace_id)
    name = workspace.name if workspace is not None else ctx.workspace_slug
    return f"## Workspace preferences -- {name}"
