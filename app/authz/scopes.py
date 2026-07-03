"""Action-key → API-token scope mapping for scoped-token enforcement.

Scoped API tokens (§03 "API tokens") carry a narrow set of
resource-scoped verbs on ``scope_json`` — ``tasks:read``,
``expenses:approve``, ``payroll:run``, etc. (§03 "Scopes"). Until
cd-7t1f1 that authority was **stored but never enforced**: a token
minted with ``{tasks:read}`` could still run payroll or manage users
because :func:`app.authz.enforce.require` authorised purely on the
token user's ``role_grant`` rows. This module closes that gap by
mapping every catalogued ``action_key`` (§05 "Action catalog") onto the
single §03 scope a scoped token must hold to perform it.

**Enforcement is layered, never widening.** The scope gate runs
*alongside* the existing role walk (:func:`require`): a scoped token
must satisfy **both** the mapped scope here **and** whatever the role
resolution already required. It can only narrow a scoped token's
authority, never grant more than the token user's grants already allow.
Sessions, owner principals, demo, system, delegated, and personal
tokens never hit this gate (see :class:`app.tenancy.WorkspaceContext`).

**Deny-by-default for unmapped actions.** Many catalogued actions have
no corresponding §03 workspace scope — governance (``permissions.*``,
``scope.transfer``), deployment admin (``deployment.*``, handled by its
own scope split in :mod:`app.api.admin.deps`), and resource families
the §03 scope taxonomy simply doesn't name (``bookings.*``, ``assets.*``,
``leaves.*``, ``quotes.*``, ``vendor_invoices.*``, ``work_orders.*``,
``scope.view``). For those a scoped token has **no** granting scope, so
:func:`required_scope_for` returns ``None`` and the gate denies — a
scoped API credential should never silently pass an action whose
authority the operator could not have granted it at mint time. Widening
any of these requires a spec edit to §03's scope list first.

**Read implied by write** (§03 "Scopes": "``*:read`` implied by
``*:write``"). :func:`scope_satisfied` honours that one implication and
nothing else — ``tasks:complete`` is not implied by ``tasks:write``,
``inventory:adjust`` is not implied by ``inventory:write``.

See ``docs/specs/03-auth-and-tokens.md`` §"Scopes" / §"Usage" (the 403
``insufficient_scope`` + ``WWW-Authenticate`` contract) and
``docs/specs/05-employees-and-roles.md`` §"Action catalog".
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

__all__ = ["ACTION_SCOPE", "required_scope_for", "scope_satisfied"]


# ---------------------------------------------------------------------------
# Explicit action_key → required scope map (§05 catalog → §03 scopes).
# ---------------------------------------------------------------------------
#
# Every entry is a deliberate spec mapping; an action absent from this
# table has no scoped-token authority (deny-by-default — see the module
# docstring). Grouped by §03 resource for readability; keep alphabetical
# within a group so drift is a one-line diff.
_ACTION_SCOPE: dict[str, str] = {
    # tasks:{read,write,complete}
    "tasks.assign_other": "tasks:write",
    "tasks.comment": "tasks:write",
    "tasks.comment_moderate": "tasks:write",
    "tasks.complete_other": "tasks:complete",
    "tasks.create": "tasks:write",
    "tasks.review.decide": "tasks:write",
    "tasks.skip_other": "tasks:write",
    # users:{read,write} — §03 notes this family covers "identity,
    # grants, engagements", so role-grant mutations map here too.
    "employees.read": "users:read",
    "role_grants.create": "users:write",
    "role_grants.revoke": "users:write",
    "users.archive": "users:write",
    "users.edit_profile_other": "users:write",
    "users.invite": "users:write",
    "users.reset_passkey": "users:write",
    # properties:{read,write}
    "places.share": "properties:write",
    "properties.archive": "properties:write",
    "properties.create": "properties:write",
    "properties.edit": "properties:write",
    "properties.read": "properties:read",
    "properties.view_access_codes": "properties:read",
    "property_workspace.revoke": "properties:write",
    "property_workspace_invite.accept": "properties:write",
    "property_workspace_invite.create": "properties:write",
    "property_workspace_invite.reject": "properties:write",
    "property_workspace_invite.revoke": "properties:write",
    # stays:{read,write}
    "stays.manage": "stays:write",
    "stays.read": "stays:read",
    # inventory:{read,write,adjust} — stocktake is a full recount, i.e.
    # an adjust-class mutation.
    "inventory.adjust": "inventory:adjust",
    "inventory.stocktake": "inventory:adjust",
    # time:{read,write}
    "time.clock_self": "time:write",
    "time.edit_others": "time:write",
    # expenses:{read,write,approve} — reimbursement is the pay-out arm
    # of approval, the closest documented verb.
    "expenses.approve": "expenses:approve",
    "expenses.reimburse": "expenses:approve",
    "expenses.submit": "expenses:write",
    # payroll:{read,run} — issuing / locking are the "run" mutations;
    # export + cross-user views are reads.
    "payroll.export": "payroll:read",
    "payroll.issue_payslip": "payroll:run",
    "payroll.lock_period": "payroll:run",
    "payroll.view_other": "payroll:read",
    # instructions:{read,write}
    "instructions.edit": "instructions:write",
    # messaging:{read,write}
    "chat_gateway.read": "messaging:read",
    "messaging.comments.author_global": "messaging:write",
    "messaging.manager_channel": "messaging:write",
    "messaging.report_issue.triage": "messaging:write",
    # admin:{impersonate,rotate,purge} — §03 "Guardrails" pins that a
    # token needs ``admin:rotate`` to manage tokens; purge is the
    # workspace hard-delete.
    "admin.purge": "admin:purge",
    "api_tokens.manage": "admin:rotate",
}


#: Public, immutable view of the action → scope table.
ACTION_SCOPE: Mapping[str, str] = MappingProxyType(_ACTION_SCOPE)


def required_scope_for(action_key: str) -> str | None:
    """Return the §03 scope a scoped token needs for ``action_key``.

    ``None`` means the action has no scoped-token authority in the §03
    taxonomy — the caller (:func:`app.authz.enforce.require`) treats
    that as deny-by-default for scoped tokens (see the module
    docstring). Unknown action keys also return ``None``; the resolver
    validates ``action_key`` against the catalog before ever consulting
    this map, so a truly-unknown key surfaces as ``unknown_action_key``,
    not an insufficient-scope denial.
    """
    return _ACTION_SCOPE.get(action_key)


def scope_satisfied(granted: frozenset[str], required: str) -> bool:
    """Return ``True`` iff ``granted`` covers the ``required`` scope.

    Direct membership, plus the single §03 implication "``*:read``
    implied by ``*:write``": a ``resource:read`` requirement is
    satisfied by holding ``resource:write``. No other verb is inferred
    — ``tasks:complete`` and ``inventory:adjust`` stand alone.
    """
    if required in granted:
        return True
    read_suffix = ":read"
    if required.endswith(read_suffix):
        resource = required[: -len(read_suffix)]
        if f"{resource}:write" in granted:
            return True
    return False
