"""Workspace agent approval policy projection.

The workspace settings surface currently exposes the v1 approval policy as a
static catalog. Persisted, owner-editable policy rows are deferred, so this
module is the shared source for the non-configurable/default lists that both
settings and embedded-agent dispatch consume.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

__all__ = [
    "WORKSPACE_ALWAYS_GATED_ACTIONS",
    "WORKSPACE_CONFIGURABLE_APPROVAL_ACTIONS",
    "tool_aliases_for_policy_action",
]


WORKSPACE_ALWAYS_GATED_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "expense_claim.set_destination_override",
        "groups.manage_members",
        "groups.manage_owners_membership",
        "organization.update_default_pay_destination",
        "payout_destination.change_default",
        "payout_destination.create",
        "payout_destination.update",
        "payroll.issue_payslip",
        "role_grants.create",
        "role_grants.revoke",
        "vendor_invoice.mark_paid",
        "vendor_invoice.pay",
        "vendor_invoices.approve",
        "vendor_invoices.mark_paid",
        "work_engagement.set_default_pay_destination",
        "work_engagement.set_default_reimbursement_destination",
        "work_engagement.set_engagement_kind",
        "work_order.accept_quote",
        "workspace.archive",
        "permission_group.membership.change",
    }
)

WORKSPACE_CONFIGURABLE_APPROVAL_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "expenses.create",
        "tasks.complete",
        "inventory.adjust",
        "booking.amend",
    }
)

_POLICY_ACTION_TOOL_ALIASES: Final[Mapping[str, frozenset[str]]] = {
    "groups.manage_members": frozenset(
        {
            "permission_groups.members.add",
            "permission_groups.members.remove",
        }
    ),
    "groups.manage_owners_membership": frozenset(
        {
            "permission_groups.members.add",
            "permission_groups.members.remove",
        }
    ),
    "permission_group.membership.change": frozenset(
        {
            "permission_groups.members.add",
            "permission_groups.members.remove",
        }
    ),
    "role_grants.create": frozenset({"role_grants.create", "role_grants.update"}),
    "role_grants.revoke": frozenset({"role_grants.revoke"}),
    "vendor_invoice.mark_paid": frozenset({"billing.vendor_invoices.mark_paid"}),
    "vendor_invoice.pay": frozenset({"billing.vendor_invoices.mark_paid"}),
    "vendor_invoices.approve": frozenset({"billing.vendor_invoices.approve"}),
    "vendor_invoices.mark_paid": frozenset({"billing.vendor_invoices.mark_paid"}),
}


def tool_aliases_for_policy_action(action_key: str) -> frozenset[str]:
    """Return operation ids that implement a policy action under another name."""
    return _POLICY_ACTION_TOOL_ALIASES.get(action_key, frozenset())
