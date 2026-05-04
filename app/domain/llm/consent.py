"""Workspace-level upstream PII consent loader (cd-ddy0).

Translates the workspace-scope ``agent_preference.upstream_pii_consent``
column into a typed :class:`~app.util.redact.ConsentSet` for the §11
redaction layer.

Public surface:

* :func:`load_consent_set` — returns the :class:`ConsentSet` for the
  given workspace. Defaults to :meth:`ConsentSet.none` (the
  redact-everything posture) when the row is missing or the column is
  empty, so call sites that have not yet been wired stay safe.

Behavioural notes:

* **Allow-list defence.** Only tokens listed in
  :data:`app.util.redact.CONSENT_TOKENS` reach the returned set. A typo
  or stale value in the DB column does NOT widen what flows upstream
  — the redactor would silently honour any token, so we re-validate at
  load time rather than trust the stored body. The allow-list is the
  spec's single source of truth for both the loader and any UI dropdown
  that exposes the toggle.
* **Workspace scope only.** ``agent_preference`` carries property /
  user / workspace rows. Per §11 "Redaction layer / Consent" the opt-in
  is a workspace-wide operator decision; we read the
  ``scope_kind='workspace'`` row whose ``scope_id`` equals the
  workspace id and ignore property / user rows here.
* **Pure read.** No writes, no audit. The loader is called on the
  outbound LLM hot path, so we hold the query to a single indexed
  lookup against the existing
  ``ix_agent_preference_workspace_scope`` composite index.

See ``docs/specs/11-llm-and-agents.md`` §"Redaction / PII" and
:mod:`app.util.redact`.
"""

from __future__ import annotations

from sqlalchemy import JSON, Column, MetaData, String, Table, select
from sqlalchemy.orm import Session

from app.util.redact import CONSENT_TOKENS, ConsentSet

__all__ = ["load_consent_set"]


_WORKSPACE_SCOPE: str = "workspace"
_AGENT_PREFERENCE = Table(
    "agent_preference",
    MetaData(),
    Column("workspace_id", String),
    Column("scope_kind", String),
    Column("scope_id", String),
    Column("upstream_pii_consent", JSON),
)


def load_consent_set(session: Session, workspace_id: str) -> ConsentSet:
    """Return the upstream PII :class:`ConsentSet` for ``workspace_id``.

    Reads the workspace-scope ``agent_preference`` row and projects
    its ``upstream_pii_consent`` JSON list through the
    :data:`~app.util.redact.CONSENT_TOKENS` allow-list. Returns
    :meth:`ConsentSet.none` when:

    * no workspace-scope row exists,
    * the row exists but the column is empty / null,
    * none of the stored tokens survives the allow-list filter.

    Tokens that are not on the allow-list are silently dropped;
    duplicates collapse via the underlying frozenset. The returned
    set is safe to pass to
    :class:`~app.adapters.llm.openrouter.OpenRouterClient` whose
    ``consents`` kwarg already accepts ``None`` as the
    redact-everything default.
    """
    stmt = select(_AGENT_PREFERENCE.c.upstream_pii_consent).where(
        _AGENT_PREFERENCE.c.workspace_id == workspace_id,
        _AGENT_PREFERENCE.c.scope_kind == _WORKSPACE_SCOPE,
        _AGENT_PREFERENCE.c.scope_id == workspace_id,
    )
    raw = session.execute(stmt).scalar_one_or_none()
    if not raw:
        return ConsentSet.none()
    allowed = frozenset(token for token in raw if token in CONSENT_TOKENS)
    if not allowed:
        return ConsentSet.none()
    return ConsentSet(fields=allowed)
