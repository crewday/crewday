"""Mint an ``approval_request`` row for a HITL-gated direct human action.

The §11 LLM agent runtime mints :class:`~app.adapters.db.llm.models.ApprovalRequest`
rows for tool calls that hit a gate. This module reuses the same
table for the §12 ``409 approval_required`` envelope on a *direct
human* call: when :func:`app.authz.require` raises
:class:`~app.authz.ApprovalRequired` (the catalog flagged the action
``requires_approval=True``), the seam catches it, calls
:func:`mint_approval_request`, and surfaces the envelope.

Direct-human approval rows look distinct from agent-runtime rows:

* ``inline_channel`` is ``None`` — there is no agent chat surface
  driving the decision; the operator decides on the manager
  approvals desk.
* ``for_user_id`` is ``None`` — the row carries no delegating user
  because the caller is acting in their own session, not via a
  delegated agent token.
* ``resolved_user_mode`` is ``None`` — the per-user agent approval
  mode (``bypass | auto | strict``) does not apply.
* ``action_json`` carries the four resolver fields (``action_key``,
  ``scope_kind``, ``scope_id``, ``actor_id``) plus the request
  ``method`` / ``path`` shape so the desk surface can render what
  the caller was trying to do without a second join. The agent
  runtime's ``tool_name`` / ``tool_call_id`` / ``tool_input`` keys
  are deliberately absent — the consumer pipeline
  (:mod:`app.domain.agent.approval`) reads those keys to *replay*
  the recorded tool call, which is meaningless for a direct-human
  request. The follow-up that promotes the ``/approvals`` desk to
  decide direct-human rows will add a parallel "re-issue" path that
  doesn't replay tool calls.

The helper writes one ``audit.approval.requested`` row attributing
the mint to ``ctx.actor_id`` so the audit trail records who was
gated, on which action, before the operator decides.

The helper flushes (so the row's id is settled before the caller
constructs the §12 envelope), but does not commit — the caller's
UoW owns the transaction (§01 "Key runtime invariants" #3). The
HTTP seam (:func:`mint_and_envelope_for_http`) is the one place
that does call :meth:`Session.commit`, because the FastAPI
:class:`UnitOfWorkImpl` rolls back the per-request transaction on
the HTTPException leaving the route, which would otherwise drop the
freshly-minted row.

See ``docs/specs/12-rest-api.md`` §"Examples" (the 409
``approval_required`` envelope) and ``docs/specs/11-llm-and-agents.md``
§"Approval pipeline" (the agent-side mint, which this helper mirrors).
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import ApprovalRequest
from app.audit import write_audit
from app.authz.enforce import ApprovalRequired
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = ["mint_and_envelope_for_http", "mint_approval_request"]


def mint_approval_request(
    session: Session,
    ctx: WorkspaceContext,
    *,
    action_key: str,
    scope_kind: str,
    scope_id: str,
    method: str | None = None,
    path: str | None = None,
    clock: Clock | None = None,
) -> ApprovalRequest:
    """Insert one ``pending`` :class:`ApprovalRequest` and return it.

    Called by the seam that caught :class:`app.authz.ApprovalRequired`
    from :func:`app.authz.require`. The returned row's ``id`` is the
    ``approval_request_id`` echoed in the §12 ``409 approval_required``
    envelope.

    ``method`` / ``path`` are optional — when present, they go on the
    row's ``action_json`` so the manager approvals desk can render
    the request shape (``POST /w/foo/api/v1/tasks``) without a second
    lookup. The seam is responsible for resolving them; the helper
    does not introspect FastAPI request state.

    No ``expires_at`` is set: the §12 envelope marks ``expires_at``
    as optional, and direct-human approvals do not yet ship a TTL
    (the LLM-side TTL knob is workspace-config-driven and lives on
    the agent runtime). Leaving the column ``NULL`` opts out of the
    :func:`app.domain.agent.approval.expire_due` sweep, which keeps
    that worker a strict consumer of agent-driven rows.
    """
    eff_clock: Clock = clock if clock is not None else SystemClock()
    now = eff_clock.now()

    action_json: dict[str, Any] = {
        "action_key": action_key,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "actor_id": ctx.actor_id,
    }
    if method is not None:
        action_json["method"] = method
    if path is not None:
        action_json["path"] = path

    row = ApprovalRequest(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        requester_actor_id=ctx.actor_id,
        action_json=action_json,
        status="pending",
        created_at=now,
        # Direct-human marker fields — see module docstring on why
        # the §11 agent-runtime columns stay NULL on this path.
        expires_at=None,
        decided_by=None,
        decided_at=None,
        rationale_md=None,
        result_json=None,
        decision_note_md=None,
        inline_channel=None,
        for_user_id=None,
        resolved_user_mode=None,
    )
    session.add(row)
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="approval_request",
        entity_id=row.id,
        action="approval.requested",
        diff={
            "approval_request_id": row.id,
            "action_key": action_key,
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "method": method,
            "path": path,
        },
        clock=eff_clock,
    )

    return row


def mint_and_envelope_for_http(
    request: Request,
    session: Session,
    ctx: WorkspaceContext,
    exc: ApprovalRequired,
) -> HTTPException:
    """Mint the row, commit the open session, return the §12 409 envelope.

    Single seam reused by every HTTP catch-site that maps an
    :class:`ApprovalRequired` to the §12 envelope — the
    :func:`app.authz.dep.Permission` dep (when :func:`require` runs
    *before* the route body) and the route-level ``except`` blocks
    around domain services that call :func:`require` themselves
    (e.g. :func:`app.domain.tasks.oneoff.create_oneoff`). Routing both
    seams through here keeps the envelope shape, the audit row, and
    the commit semantics single-sourced.

    Why an explicit ``session.commit()``:
    :class:`~app.adapters.db.session.UnitOfWorkImpl` rolls back the
    per-request transaction on **any** exception leaving the dep
    generator (HTTPException included), so without an explicit commit
    the freshly-minted ``approval_request`` row would vanish. The mint
    is the only deliberate write on this path; committing here keeps
    the §12 ``409`` envelope's ``approval_request_id`` honest (a
    follow-up GET to ``/approvals`` finds the row).

    The envelope:

    * ``error`` — ``"approval_required"``.
    * ``approval_request_id`` — the freshly-minted row's ULID.
    * ``expires_at`` — currently ``None`` (direct-human approvals
      ship without a TTL — see :func:`mint_approval_request`).
    """
    row = mint_approval_request(
        session,
        ctx,
        action_key=exc.action_key,
        scope_kind=exc.scope_kind,
        scope_id=exc.scope_id,
        method=request.method,
        path=request.url.path,
    )
    session.commit()
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "approval_required",
            "approval_request_id": row.id,
            "expires_at": (
                row.expires_at.isoformat() if row.expires_at is not None else None
            ),
        },
    )
