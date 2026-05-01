#!/usr/bin/env python3
"""Dev-only seed for the GA journey-4 e2e test (cd-9cdn).

The expense-to-payslip end-to-end test exercises the full claim →
payslip → mark-paid pipeline against the dev compose stack, but two
seams cannot be driven over HTTP today:

* :class:`app.adapters.db.workspace.models.WorkEngagement` rows are
  only created via the invite-accept ceremony
  (:mod:`app.domain.identity.membership`); there is no
  ``POST /work_engagements`` create endpoint, and
  :func:`scripts.dev_login.mint_session` deliberately skips engagement
  seeding because most other journeys do not need it.
* :func:`app.domain.payroll.compute.compute_payslip` raises
  ``PayslipInvariantViolated("no pay-bearing bookings for user")`` for
  workers that have only expense claims (no shift bookings). The
  expense-only worker therefore cannot get a payslip via the normal
  ``/pay-periods/{id}/lock`` flow; the test instead seeds a draft
  :class:`Payslip` directly and exercises issue + mark-paid through
  the API. The same shape the integration test
  ``test_mark_paid_settles_approved_claims_for_period_window`` uses.

This script lives alongside :mod:`scripts.dev_login` and is bind-
mounted into the ``app-api`` container by ``mocks/docker-compose.yml``.
Same hard-gate triple as ``dev_login``: ``CREWDAY_DEV_AUTH=1``,
``CREWDAY_PROFILE=dev``, SQLite-only database URL.

**Outputs.** A single JSON line on stdout carrying the seeded
``work_engagement_id``, ``payslip_id``, ``pay_period_id``, and the
worker's ``user_id``. The e2e test parses that envelope to drive the
HTTP API for create-claim / submit / approve / issue / mark-paid.

Idempotent on ``(worker_email, workspace_slug)``: an existing active
engagement is reused; an existing draft payslip for the same period
is reused too. The pay-period window is fixed (the e2e test treats
the timestamps as opaque inputs to ``POST /pay-periods``).

Use only from the e2e test — manual operators should drive the full
journey through the SPA / CLI instead.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final

import click
from sqlalchemy import select
from sqlalchemy.orm import Session as SqlaSession

from app.adapters.db.expenses.models import ExpenseClaim
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.payroll.models import PayPeriod, Payslip
from app.adapters.db.session import make_uow
from app.adapters.db.workspace.models import WorkEngagement, Workspace
from app.config import get_settings
from app.tenancy import tenant_agnostic
from app.util.clock import SystemClock
from app.util.ulid import new_ulid

__all__ = ["main", "seed_journey4"]


_DEV_AUTH_ENV_VAR: Final[str] = "CREWDAY_DEV_AUTH"

# Pay-period window the e2e test pins on. Uses a stable wall-clock
# range so the seed (and the integration smoke equivalent) is
# repeatable. ``purchased_at`` for the seeded claim is mid-window so
# the ``settle_payslip_reimbursements`` query (which filters
# ``purchased_at`` to ``[starts_at, ends_at)``) flips the claim to
# ``reimbursed`` on ``mark_paid``.
_PERIOD_STARTS_AT: Final[datetime] = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
_PERIOD_ENDS_AT: Final[datetime] = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
# Mid-window timestamp matching ``_high_confidence_payload`` shape so
# the autofill assertions in the e2e test (vendor / amount / currency
# / category) line up with the canned ``FakeLLMClient`` payload.
_DEFAULT_PURCHASED_AT: Final[datetime] = datetime(2026, 4, 17, 12, 30, tzinfo=UTC)


class _GateError(RuntimeError):
    """Refused — one of the hard gates failed."""


def _check_gates() -> None:
    """Refuse to run unless every dev-auth gate is green.

    Mirrors :func:`scripts.dev_login._check_gates` 1:1 — the seed has
    the same blast radius as ``dev_login`` (it writes engagement +
    payslip rows), so the same triple gate applies. We do not import
    the helper across modules because each script is meant to read as
    a self-contained dev affordance.
    """
    raw = os.environ.get(_DEV_AUTH_ENV_VAR, "0").lower()
    if raw not in {"1", "yes", "true"}:
        raise _GateError(
            f"{_DEV_AUTH_ENV_VAR} is not set to 1/yes/true (got {raw!r}); "
            f"dev-seed is hard-gated off."
        )
    settings = get_settings()
    if settings.profile != "dev":
        raise _GateError(
            f"CREWDAY_PROFILE={settings.profile!r} — dev-seed requires profile=dev."
        )
    scheme = settings.database_url.split(":", 1)[0].lower()
    if not scheme.startswith("sqlite"):
        raise _GateError(
            f"database_url scheme {scheme!r} is not SQLite; dev-seed refuses "
            "to mint rows against a non-SQLite DB."
        )


def _find_user(session: SqlaSession, email_lower: str) -> User:
    user = session.scalars(
        select(User).where(User.email_lower == email_lower)
    ).one_or_none()
    if user is None:
        raise RuntimeError(
            f"user {email_lower!r} does not exist; "
            f"call ``scripts.dev_login`` for them first."
        )
    return user


def _find_workspace(session: SqlaSession, slug: str) -> Workspace:
    ws = session.scalars(select(Workspace).where(Workspace.slug == slug)).one_or_none()
    if ws is None:
        raise RuntimeError(
            f"workspace {slug!r} does not exist; "
            f"call ``scripts.dev_login`` for it first."
        )
    return ws


def _ensure_engagement(
    session: SqlaSession, *, workspace_id: str, user_id: str, now: datetime
) -> str:
    """Return the active engagement id, inserting one if missing."""
    existing = session.scalars(
        select(WorkEngagement)
        .where(WorkEngagement.workspace_id == workspace_id)
        .where(WorkEngagement.user_id == user_id)
        .where(WorkEngagement.archived_on.is_(None))
    ).one_or_none()
    if existing is not None:
        return existing.id

    engagement = WorkEngagement(
        id=new_ulid(),
        user_id=user_id,
        workspace_id=workspace_id,
        engagement_kind="payroll",
        supplier_org_id=None,
        pay_destination_id=None,
        reimbursement_destination_id=None,
        started_on=date(2026, 1, 1),
        archived_on=None,
        notes_md="",
        created_at=now,
        updated_at=now,
    )
    session.add(engagement)
    session.flush()
    return engagement.id


def _ensure_pay_period(
    session: SqlaSession, *, workspace_id: str, now: datetime
) -> str:
    """Return the pay-period id, inserting one if missing."""
    existing = session.scalars(
        select(PayPeriod)
        .where(PayPeriod.workspace_id == workspace_id)
        .where(PayPeriod.starts_at == _PERIOD_STARTS_AT)
        .where(PayPeriod.ends_at == _PERIOD_ENDS_AT)
    ).one_or_none()
    if existing is not None:
        return existing.id

    period = PayPeriod(
        id=new_ulid(),
        workspace_id=workspace_id,
        starts_at=_PERIOD_STARTS_AT,
        ends_at=_PERIOD_ENDS_AT,
        state="open",
        locked_at=None,
        locked_by=None,
        created_at=now,
    )
    session.add(period)
    session.flush()
    return period.id


def _ensure_payslip(
    session: SqlaSession,
    *,
    workspace_id: str,
    period_id: str,
    user_id: str,
    now: datetime,
) -> str:
    """Return the draft payslip id, inserting one if missing.

    The reimbursement amount stays at zero on the seeded row — the
    e2e test patches it after the manager has approved the worker's
    claim, mirroring the integration-test pattern. We seed a minimal
    valid payslip (zero gross, zero hours, zero deductions) so the
    ``GET /payslips/{id}`` surface returns an ``issuable`` row.
    """
    existing = session.scalars(
        select(Payslip)
        .where(Payslip.workspace_id == workspace_id)
        .where(Payslip.pay_period_id == period_id)
        .where(Payslip.user_id == user_id)
    ).one_or_none()
    if existing is not None:
        return existing.id

    slip = Payslip(
        id=new_ulid(),
        workspace_id=workspace_id,
        pay_period_id=period_id,
        user_id=user_id,
        shift_hours_decimal=Decimal("0.00"),
        overtime_hours_decimal=Decimal("0.00"),
        gross_cents=0,
        deductions_cents={},
        expense_reimbursements_cents=0,
        net_cents=0,
        # ``settle_payslip_reimbursements`` (the post-mark-paid hook)
        # reads ``components_json["reimbursements"]`` to derive the
        # claim list to settle; the e2e test rewrites this dict after
        # approval so the seeded shape just needs to be a valid empty
        # bag.
        components_json={
            "schema_version": 1,
            "currency": "EUR",
            "reimbursements": [],
        },
        status="draft",
        created_at=now,
    )
    session.add(slip)
    session.flush()
    return slip.id


def seed_journey4(*, worker_email: str, workspace_slug: str) -> dict[str, str]:
    """Seed engagement + pay period + draft payslip; return their ids.

    The engagement is the prerequisite for ``POST /expenses`` (the
    create-claim service refuses any ``work_engagement_id`` whose
    ``user_id`` does not match the caller). The pay period anchors
    the wall-clock window over which the post-mark-paid hook will
    sweep approved claims into ``reimbursed``. The payslip is the
    draft row the manager flips through the
    ``/payslips/{id}/issue`` and ``/payslips/{id}/mark_paid`` API
    after approving the worker's claim.

    Idempotent — see the module docstring. Returns the new (or
    existing) row ids in a flat dict so the caller can JSON-encode
    it onto stdout.
    """
    canonical_email = canonicalise_email(worker_email)
    with make_uow() as uow_session:
        assert isinstance(uow_session, SqlaSession)
        session = uow_session
        now = SystemClock().now()
        with tenant_agnostic():
            user = _find_user(session, canonical_email)
            workspace = _find_workspace(session, workspace_slug)
            engagement_id = _ensure_engagement(
                session,
                workspace_id=workspace.id,
                user_id=user.id,
                now=now,
            )
            period_id = _ensure_pay_period(session, workspace_id=workspace.id, now=now)
            payslip_id = _ensure_payslip(
                session,
                workspace_id=workspace.id,
                period_id=period_id,
                user_id=user.id,
                now=now,
            )

        return {
            "user_id": user.id,
            "workspace_id": workspace.id,
            "work_engagement_id": engagement_id,
            "pay_period_id": period_id,
            "payslip_id": payslip_id,
            "purchased_at": _DEFAULT_PURCHASED_AT.isoformat(),
            "period_starts_at": _PERIOD_STARTS_AT.isoformat(),
            "period_ends_at": _PERIOD_ENDS_AT.isoformat(),
        }


def attach_reimbursement_to_payslip(
    *,
    workspace_slug: str,
    payslip_id: str,
    claim_id: str,
    work_engagement_id: str,
    purchased_at: str,
    amount_cents: int,
    currency: str,
    description: str,
) -> dict[str, str]:
    """Patch a draft payslip's reimbursement bag for a freshly approved claim.

    The e2e test calls this after the manager has approved the
    worker's claim through the public API, so the
    ``settle_payslip_reimbursements`` query (which fires on
    ``mark_paid`` and re-derives the claim list from
    ``components_json["reimbursements"]``) has the right rows to
    flip. This step is the seed equivalent of the
    period-close worker writing ``components_json`` during the
    booking-driven recompute path; we cannot use the real recompute
    path because the worker has no bookings (see module docstring).
    """
    with make_uow() as uow_session:
        assert isinstance(uow_session, SqlaSession)
        session = uow_session
        with tenant_agnostic():
            workspace = _find_workspace(session, workspace_slug)
            slip = session.scalars(
                select(Payslip)
                .where(Payslip.workspace_id == workspace.id)
                .where(Payslip.id == payslip_id)
            ).one_or_none()
            if slip is None:
                raise RuntimeError(
                    f"payslip {payslip_id!r} not found in workspace {workspace_slug!r}"
                )
            # Verify the claim exists and belongs to the workspace —
            # the ``settle_payslip_reimbursements`` SQL would no-op
            # silently otherwise, leaving the test with a confusing
            # "stayed approved" failure.
            claim = session.scalars(
                select(ExpenseClaim)
                .where(ExpenseClaim.workspace_id == workspace.id)
                .where(ExpenseClaim.id == claim_id)
            ).one_or_none()
            if claim is None:
                raise RuntimeError(
                    f"expense claim {claim_id!r} not found in workspace "
                    f"{workspace_slug!r}; approve it through the API first"
                )
            slip.expense_reimbursements_cents = amount_cents
            slip.net_cents = amount_cents
            slip.components_json = {
                "schema_version": 1,
                "currency": currency,
                "reimbursements": [
                    {
                        "claim_id": claim_id,
                        "work_engagement_id": work_engagement_id,
                        "purchased_at": purchased_at,
                        "decided_at": claim.decided_at.isoformat()
                        if claim.decided_at is not None
                        else None,
                        "description": description,
                        "currency": currency,
                        "amount_cents": amount_cents,
                    }
                ],
            }
            session.flush()
        return {
            "payslip_id": payslip_id,
            "expense_reimbursements_cents": str(amount_cents),
        }


@click.group(
    help=(
        "Dev-only seed helpers for the GA journey-4 e2e test. "
        "Hard-gated on CREWDAY_DEV_AUTH=1 + profile=dev + sqlite."
    )
)
def main() -> None:
    """CLI entry point — sub-commands route to the two seed steps."""


@main.command("seed")
@click.option("--worker-email", required=True, help="Worker user email.")
@click.option(
    "--workspace",
    "workspace_slug",
    required=True,
    help="Workspace slug (must exist; call dev_login first).",
)
def seed_cmd(worker_email: str, workspace_slug: str) -> None:
    """Seed engagement + pay period + draft payslip; print the ids as JSON."""
    try:
        _check_gates()
    except _GateError as exc:
        print(f"error: dev-seed refused to run: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    result = seed_journey4(worker_email=worker_email, workspace_slug=workspace_slug)
    click.echo(json.dumps(result))


@main.command("attach-reimbursement")
@click.option(
    "--workspace",
    "workspace_slug",
    required=True,
    help="Workspace slug.",
)
@click.option("--payslip-id", required=True, help="Draft payslip id.")
@click.option("--claim-id", required=True, help="Approved expense claim id.")
@click.option(
    "--work-engagement-id",
    required=True,
    help="Engagement bound to the approved claim.",
)
@click.option(
    "--purchased-at",
    required=True,
    help="Claim purchased_at as ISO-8601 (mirrors the API's wire shape).",
)
@click.option("--amount-cents", required=True, type=int, help="Claim total in cents.")
@click.option("--currency", required=True, help="Three-letter ISO-4217 code.")
@click.option(
    "--description",
    required=True,
    help="Human-readable description (vendor + category, usually).",
)
def attach_cmd(
    workspace_slug: str,
    payslip_id: str,
    claim_id: str,
    work_engagement_id: str,
    purchased_at: str,
    amount_cents: int,
    currency: str,
    description: str,
) -> None:
    """Patch the draft payslip's reimbursement bag; print the result as JSON."""
    try:
        _check_gates()
    except _GateError as exc:
        print(f"error: dev-seed refused to run: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    result = attach_reimbursement_to_payslip(
        workspace_slug=workspace_slug,
        payslip_id=payslip_id,
        claim_id=claim_id,
        work_engagement_id=work_engagement_id,
        purchased_at=purchased_at,
        amount_cents=amount_cents,
        currency=currency,
        description=description,
    )
    click.echo(json.dumps(result))


if __name__ == "__main__":
    main()
