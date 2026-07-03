"""Worker-facing booking amend / decline write path (§09).

The §09 "Amend operation" and "Worker decline" flows share one thin
service seam consumed by :mod:`app.api.v1.bookings`. Both:

* load the booking through :class:`BookingWriteRepository` (workspace
  scoped; ``None`` -> :class:`BookingNotFound` -> HTTP 404),
* gate through the injected
  :class:`~app.domain.identity.availability_ports.CapabilityChecker`
  (SA-backed by
  :class:`~app.adapters.db.availability.repositories.SqlAlchemyCapabilityChecker`),
* persist the resolved field-set, and
* append one :func:`app.audit.write_audit` row inside the same UoW.

**Amend authority + threshold** (§09 "Amend operation").

* A self-amend (``booking.user_id == ctx.actor_id``) holds
  ``bookings.amend_self``. It auto-approves when it *decreases* time
  (any amount) or *increases* it by at most the engagement's resolved
  ``bookings.auto_approve_overrun_minutes`` (default 30):
  ``actual_minutes`` and ``actual_minutes_paid`` move together,
  ``adjusted = true``, ``adjustment_reason`` is set, ``pending_*``
  clear. A larger increase records ``pending_amend_minutes`` /
  ``pending_amend_reason`` and leaves ``actual_minutes_paid`` at the
  current value for the manager amend queue.
* Amending someone else's booking holds ``bookings.amend_other``
  (manager / owner) and is unconditional — no threshold, always
  approved.

**Decline** (§09 "Worker decline"). The assigned worker declines a
``scheduled`` booking via ``bookings.decline_self``: the server stamps
``declined_at`` / ``declined_reason`` and returns the row to
``status = pending_approval`` for manager reassignment. The
``work_engagement_id`` is NOT cleared — the v1 column is a NOT NULL FK,
so "unassigned" is represented by the ``pending_approval`` status plus
``declined_at`` rather than a null engagement; the original assignee is
excluded from the reassignment pool by the manager coverage flow
(§09 "Coverage / reassignment"), not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.audit import AuditVia, write_audit
from app.domain.identity.availability_ports import (
    CapabilityChecker,
    SeamPermissionDenied,
)
from app.domain.payroll.ports import BookingWriteRepository, BookingWriteRow
from app.domain.settings.cascade import SettingScopeChain, resolve_most_specific
from app.tenancy import WorkspaceContext

__all__ = [
    "AmendOutcome",
    "BookingNotDeclinable",
    "BookingNotFound",
    "BookingPermissionDenied",
    "amend_booking",
    "compute_amend",
    "decline_booking",
]

_AUTO_APPROVE_OVERRUN_KEY = "bookings.auto_approve_overrun_minutes"
_DEFAULT_AUTO_APPROVE_OVERRUN_MINUTES = 30


class BookingNotFound(Exception):
    """The booking does not exist in the caller's workspace (HTTP 404)."""


class BookingPermissionDenied(Exception):
    """The caller may not amend / decline this booking (HTTP 403).

    ``str(exc)`` is the denied ``action_key`` so the router echoes it
    verbatim in the §12 ``permission_denied`` envelope.
    """


class BookingNotDeclinable(Exception):
    """The booking is not in a ``scheduled`` state and cannot be declined.

    ``str(exc)`` is the current status so the router surfaces it in the
    conflict envelope.
    """


@dataclass(frozen=True, slots=True)
class AmendOutcome:
    """Resolved field-set an amend writes onto the ``booking`` row."""

    auto_approved: bool
    actual_minutes: int | None
    actual_minutes_paid: int
    adjusted: bool
    adjustment_reason: str | None
    pending_amend_minutes: int | None
    pending_amend_reason: str | None


def compute_amend(
    *,
    row: BookingWriteRow,
    requested_minutes: int,
    reason: str,
    is_self_amend: bool,
    auto_approve_overrun_minutes: int,
) -> AmendOutcome:
    """Return the §09 amend field-set for ``requested_minutes``.

    Pure: no I/O. The baseline the increase is measured against is the
    booking's current ``actual_minutes_paid`` (which defaults to the
    scheduled minutes until a prior amend moved it). A manager amend
    (``is_self_amend=False``) is always approved; a self-amend approves
    only when it decreases time or increases it within the threshold.
    """
    baseline = row.actual_minutes_paid
    delta = requested_minutes - baseline
    if (not is_self_amend) or delta <= auto_approve_overrun_minutes:
        return AmendOutcome(
            auto_approved=True,
            actual_minutes=requested_minutes,
            actual_minutes_paid=requested_minutes,
            adjusted=True,
            adjustment_reason=reason,
            pending_amend_minutes=None,
            pending_amend_reason=None,
        )
    # Over threshold: record the worker's pending claim; pay is held at
    # the current value and the row surfaces on the manager amend queue.
    return AmendOutcome(
        auto_approved=False,
        actual_minutes=row.actual_minutes,
        actual_minutes_paid=baseline,
        adjusted=row.adjusted,
        adjustment_reason=row.adjustment_reason,
        pending_amend_minutes=requested_minutes,
        pending_amend_reason=reason,
    )


def _resolve_auto_approve_overrun_minutes(
    repo: BookingWriteRepository, ctx: WorkspaceContext, row: BookingWriteRow
) -> int:
    value = resolve_most_specific(
        repo.session,
        _AUTO_APPROVE_OVERRUN_KEY,
        SettingScopeChain(
            workspace_id=ctx.workspace_id,
            property_id=row.property_id,
            actor_user_id=row.user_id,
        ),
        default=_DEFAULT_AUTO_APPROVE_OVERRUN_MINUTES,
    )
    if isinstance(value, bool) or not isinstance(value, int):
        # A malformed setting value (str, float, bool) is operator
        # error, not a reason to widen or block the threshold silently.
        return _DEFAULT_AUTO_APPROVE_OVERRUN_MINUTES
    return max(0, value)


def amend_booking(
    repo: BookingWriteRepository,
    checker: CapabilityChecker,
    ctx: WorkspaceContext,
    *,
    booking_id: str,
    requested_minutes: int,
    reason: str,
    now: datetime,
    via: AuditVia = "web",
) -> BookingWriteRow:
    """Apply a §09 amend and return the refreshed row (or raise)."""
    row = repo.get(workspace_id=ctx.workspace_id, booking_id=booking_id)
    if row is None:
        raise BookingNotFound(booking_id)

    is_self = row.user_id == ctx.actor_id
    action_key = "bookings.amend_self" if is_self else "bookings.amend_other"
    try:
        checker.require(action_key)
    except SeamPermissionDenied as exc:
        raise BookingPermissionDenied(action_key) from exc

    threshold = (
        _resolve_auto_approve_overrun_minutes(repo, ctx, row)
        if is_self
        else _DEFAULT_AUTO_APPROVE_OVERRUN_MINUTES
    )
    outcome = compute_amend(
        row=row,
        requested_minutes=requested_minutes,
        reason=reason,
        is_self_amend=is_self,
        auto_approve_overrun_minutes=threshold,
    )
    updated = repo.apply_amend(
        workspace_id=ctx.workspace_id,
        booking_id=booking_id,
        actual_minutes=outcome.actual_minutes,
        actual_minutes_paid=outcome.actual_minutes_paid,
        adjusted=outcome.adjusted,
        adjustment_reason=outcome.adjustment_reason,
        pending_amend_minutes=outcome.pending_amend_minutes,
        pending_amend_reason=outcome.pending_amend_reason,
        now=now,
    )
    write_audit(
        repo.session,
        ctx,
        entity_kind="booking",
        entity_id=booking_id,
        action="booking.amended"
        if outcome.auto_approved
        else "booking.amend_requested",
        diff={
            "requested_minutes": requested_minutes,
            "reason": reason,
            "auto_approved": outcome.auto_approved,
            "actual_minutes_paid": {
                "before": row.actual_minutes_paid,
                "after": updated.actual_minutes_paid,
            },
            "pending_amend_minutes": {
                "before": row.pending_amend_minutes,
                "after": updated.pending_amend_minutes,
            },
        },
        via=via,
    )
    return updated


def decline_booking(
    repo: BookingWriteRepository,
    checker: CapabilityChecker,
    ctx: WorkspaceContext,
    *,
    booking_id: str,
    reason: str | None,
    now: datetime,
    via: AuditVia = "web",
) -> BookingWriteRow:
    """Decline an assigned ``scheduled`` booking and return the refreshed row."""
    row = repo.get(workspace_id=ctx.workspace_id, booking_id=booking_id)
    if row is None:
        raise BookingNotFound(booking_id)

    try:
        checker.require("bookings.decline_self")
    except SeamPermissionDenied as exc:
        raise BookingPermissionDenied("bookings.decline_self") from exc
    if row.user_id != ctx.actor_id:
        # Decline is self-only: a booking assigned to another worker is
        # not the caller's to refuse (§09 "Worker decline").
        raise BookingPermissionDenied("bookings.decline_self")
    if row.status != "scheduled":
        raise BookingNotDeclinable(row.status)

    updated = repo.apply_decline(
        workspace_id=ctx.workspace_id,
        booking_id=booking_id,
        status="pending_approval",
        declined_at=now,
        declined_reason=reason,
        now=now,
    )
    write_audit(
        repo.session,
        ctx,
        entity_kind="booking",
        entity_id=booking_id,
        action="booking.declined",
        diff={
            "status": {"before": row.status, "after": updated.status},
            "declined_reason": reason,
        },
        via=via,
    )
    return updated
