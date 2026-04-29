"""Payroll context router (§01 "Context map", §12 "Time, payroll, expenses").

Mounted inside ``/w/<slug>/api/v1/payroll`` by the app factory.
Surface (cd-ea7):

```
GET    /users/{user_id}/pay-rules    # paginated, newest-first
POST   /users/{user_id}/pay-rules
GET    /pay-rules/{rule_id}
PATCH  /pay-rules/{rule_id}
DELETE /pay-rules/{rule_id}          # soft-retire (sets effective_to=now)
```

Every route requires an active :class:`~app.tenancy.WorkspaceContext`
and gates on ``pay_rules.edit`` at workspace scope (§05 action
catalog default-allow: ``owners, managers``). Reads gate too —
pay rates are compensation-PII (§15) and the v1 surface is
owner/manager-only end-to-end. The domain service layer also
re-asserts the same capability so non-HTTP transports (CLI, agent,
worker) get the same gate without re-implementing it.

The router is a thin DTO passthrough over the domain service in
:mod:`app.domain.payroll.rules`. Three error mappings carry weight:

* :class:`~app.domain.payroll.rules.PayRuleNotFound` → 404 (unknown
  id, soft-retired rows are still found via :func:`get_rule` — the
  service does not distinguish wrong-workspace from really-missing).
* :class:`~app.domain.payroll.rules.PayRuleInvariantViolated` → 422
  (validation failure: bad currency, multiplier out of range,
  bad window).
* :class:`~app.domain.payroll.rules.PayRuleLocked` → 409 (rule is
  consumed by a paid payslip; callers author a successor row with a
  later ``effective_from`` instead).

Routes follow the §12 "Pagination" envelope verbatim — listings
return ``{data, next_cursor, has_more}``; non-list reads + writes
return the bare resource shape.

See ``docs/specs/09-time-payroll-expenses.md`` §"Pay rules",
``docs/specs/02-domain-model.md`` §"pay_rule",
``docs/specs/12-rest-api.md`` §"Time, payroll, expenses".
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.adapters.db.payroll.repositories import (
    SqlAlchemyPayPeriodRepository,
    SqlAlchemyPayrollExportRepository,
    SqlAlchemyPayRuleRepository,
    SqlAlchemyPayslipComputeRepository,
    SqlAlchemyPayslipReadRepository,
)
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.audit import write_audit
from app.authz import require
from app.authz.dep import Permission
from app.authz.enforce import PermissionDenied
from app.domain.payroll.compute import (
    PayslipComputeConflict,
    PayslipInvariantViolated,
    payslip_recompute,
)
from app.domain.payroll.exports import (
    ExpenseStatusInvalid,
    ExportWindowInvalid,
    PayPeriodNotFound,
    export_expense_ledger_csv,
    export_payslips_csv,
    export_timesheets_csv,
    stream_csv_with_audit,
)
from app.domain.payroll.periods import (
    PayPeriodInvariantViolated,
    PayPeriodTransitionConflict,
    PayPeriodView,
    create_period,
    delete_period,
    get_period,
    list_periods,
    lock_period,
    mark_paid,
    reopen_period,
    update_period,
)
from app.domain.payroll.periods import (
    PayPeriodNotFound as DomainPayPeriodNotFound,
)
from app.domain.payroll.ports import PayslipReadRow
from app.domain.payroll.rules import (
    BASE_CENTS_MAX,
    PayRuleCreate,
    PayRuleInvariantViolated,
    PayRuleLocked,
    PayRuleNotFound,
    PayRuleUpdate,
    PayRuleView,
    create_rule,
    cursor_for_view,
    get_rule,
    list_rules,
    soft_delete_rule,
    update_rule,
)
from app.domain.privacy import payout_manifest_available
from app.events import bus as default_event_bus
from app.events.types import PayrollExportReady
from app.tenancy import WorkspaceContext
from app.util.clock import SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "PayRuleCreateRequest",
    "PayRuleListResponse",
    "PayRuleResponse",
    "PayRuleUpdateRequest",
    "build_payroll_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


_MAX_ID_LEN = 64


# ---------------------------------------------------------------------------
# Wire-facing shapes
# ---------------------------------------------------------------------------


class _PayRuleBodyRequest(BaseModel):
    """Shared mutable body for :class:`PayRuleCreateRequest` and update."""

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(..., min_length=3, max_length=3)
    base_cents_per_hour: int = Field(..., ge=0, le=BASE_CENTS_MAX)
    overtime_multiplier: Decimal = Field(default=Decimal("1.5"))
    night_multiplier: Decimal = Field(default=Decimal("1.25"))
    weekend_multiplier: Decimal = Field(default=Decimal("1.5"))
    effective_from: datetime
    effective_to: datetime | None = None


class PayRuleCreateRequest(_PayRuleBodyRequest):
    """Request body for ``POST /users/{user_id}/pay-rules``.

    ``user_id`` lives on the URL path; including it on the body
    would let a caller mismatch the two and silently target the
    wrong user.
    """


class PayRuleUpdateRequest(_PayRuleBodyRequest):
    """Request body for ``PATCH /pay-rules/{rule_id}``.

    Full-replacement update — v1 does not yet expose a per-field
    PATCH on pay rules. Same shape as
    :class:`PayRuleCreateRequest` minus the path-bound ``user_id``.
    """


class PayRuleResponse(BaseModel):
    """Response shape for pay-rule operations."""

    id: str
    workspace_id: str
    user_id: str
    currency: str
    base_cents_per_hour: int
    overtime_multiplier: Decimal
    night_multiplier: Decimal
    weekend_multiplier: Decimal
    effective_from: datetime
    effective_to: datetime | None
    created_by: str | None
    created_at: datetime


class PayRuleListResponse(BaseModel):
    """Collection envelope for the per-user pay-rule listing.

    Matches §12 "Pagination" verbatim — ``{data, next_cursor,
    has_more}``.
    """

    data: list[PayRuleResponse]
    next_cursor: str | None = None
    has_more: bool = False


class PayPeriodCreateRequest(BaseModel):
    """Request body for ``POST /pay-periods``."""

    model_config = ConfigDict(extra="forbid")

    starts_at: datetime
    ends_at: datetime


class PayPeriodUpdateRequest(PayPeriodCreateRequest):
    """Request body for ``PATCH /pay-periods/{id}``."""


class PayPeriodResponse(BaseModel):
    """Response shape for pay-period operations."""

    id: str
    workspace_id: str
    starts_at: datetime
    ends_at: datetime
    state: str
    locked_at: datetime | None
    locked_by: str | None
    created_at: datetime


class PayPeriodListResponse(BaseModel):
    """Collection envelope for pay-period listings."""

    data: list[PayPeriodResponse]


class MoneyResponse(BaseModel):
    """Integer-minor-unit monetary value."""

    cents: int
    currency: str


class PayslipResponse(BaseModel):
    """Response shape for payslip read/list operations."""

    id: str
    workspace_id: str
    pay_period_id: str
    user_id: str
    currency: str
    shift_hours_decimal: Decimal
    overtime_hours_decimal: Decimal
    gross: MoneyResponse
    deductions: list[MoneyResponse]
    net: MoneyResponse
    components: dict[str, object]
    status: str
    issued_at: datetime | None
    paid_at: datetime | None
    created_at: datetime


class PayslipListResponse(BaseModel):
    """Collection envelope for payslip listings."""

    data: list[PayslipResponse]


class PayrollExportJobResponse(BaseModel):
    """Synchronous-ready export job placeholder until durable jobs land."""

    job_id: str
    status: str
    kind: str
    pay_period_id: str


class PayoutManifestResponse(BaseModel):
    """JIT payout manifest placeholder for issued payslips."""

    payslip_id: str
    status: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _view_to_response(view: PayRuleView) -> PayRuleResponse:
    return PayRuleResponse(
        id=view.id,
        workspace_id=view.workspace_id,
        user_id=view.user_id,
        currency=view.currency,
        base_cents_per_hour=view.base_cents_per_hour,
        overtime_multiplier=view.overtime_multiplier,
        night_multiplier=view.night_multiplier,
        weekend_multiplier=view.weekend_multiplier,
        effective_from=view.effective_from,
        effective_to=view.effective_to,
        created_by=view.created_by,
        created_at=view.created_at,
    )


def _period_to_response(view: PayPeriodView) -> PayPeriodResponse:
    return PayPeriodResponse(
        id=view.id,
        workspace_id=view.workspace_id,
        starts_at=view.starts_at,
        ends_at=view.ends_at,
        state=view.state,
        locked_at=view.locked_at,
        locked_by=view.locked_by,
        created_at=view.created_at,
    )


def _payslip_to_response(row: PayslipReadRow) -> PayslipResponse:
    return PayslipResponse(
        id=row.id,
        workspace_id=row.workspace_id,
        pay_period_id=row.pay_period_id,
        user_id=row.user_id,
        currency=row.currency,
        shift_hours_decimal=row.shift_hours_decimal,
        overtime_hours_decimal=row.overtime_hours_decimal,
        gross=MoneyResponse(cents=row.gross_cents, currency=row.currency),
        deductions=[
            MoneyResponse(cents=cents, currency=row.currency)
            for _key, cents in sorted(row.deductions_cents.items())
        ],
        net=MoneyResponse(cents=row.net_cents, currency=row.currency),
        components=row.components_json,
        status=row.status,
        issued_at=row.issued_at,
        paid_at=row.paid_at,
        created_at=row.created_at,
    )


def _request_to_create(body: PayRuleCreateRequest) -> PayRuleCreate:
    return PayRuleCreate.model_validate(body.model_dump())


def _request_to_update(body: PayRuleUpdateRequest) -> PayRuleUpdate:
    return PayRuleUpdate.model_validate(body.model_dump())


def _http_for_invariant(exc: PayRuleInvariantViolated) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": "pay_rule_invariant", "message": str(exc)},
    )


def _http_for_locked(exc: PayRuleLocked) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "pay_rule_locked", "message": str(exc)},
    )


def _http_for_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "pay_rule_not_found"},
    )


def _http_for_period_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "pay_period_not_found"},
    )


def _http_for_period_invariant(exc: Exception) -> HTTPException:
    message = str(exc)
    error = (
        "bookings_unsettled"
        if message.startswith("bookings_unsettled")
        else "pay_period_invariant"
    )
    return HTTPException(
        status_code=422,
        detail={"error": error, "message": message},
    )


def _http_for_period_conflict(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "pay_period_transition_conflict", "message": str(exc)},
    )


def _http_for_payslip_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "payslip_not_found"},
    )


def _http_for_payslip_conflict(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "payslip_transition_conflict", "message": message},
    )


def _default_payout_snapshot() -> dict[str, object]:
    return {"schema_version": 1, "destinations": [], "reimbursements": []}


def _require_workspace_permission(
    session: Session,
    ctx: WorkspaceContext,
    *,
    action_key: str,
) -> None:
    try:
        require(
            session,
            ctx,
            action_key=action_key,
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
        )
    except PermissionDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "permission_denied", "action_key": action_key},
        ) from exc


class _ImmediatePayPeriodRecomputeScheduler:
    """Adapter that makes period locking compute payslips in the same request."""

    def __init__(self, session: Session, ctx: WorkspaceContext) -> None:
        self._session = session
        self._ctx = ctx

    def schedule_period_recompute(self, *, workspace_id: str, period_id: str) -> None:
        _ = workspace_id
        payslip_recompute(
            SqlAlchemyPayslipComputeRepository(self._session),
            self._ctx,
            period_id=period_id,
        )


_UserIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=_MAX_ID_LEN,
        description="Owner of the pay-rule chain — usually a ``user.id`` ULID.",
    ),
]
_RuleIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=_MAX_ID_LEN,
        description="ULID of the target ``pay_rule`` row.",
    ),
]
_ExportKindPath = Annotated[
    str,
    Path(
        pattern="^(timesheets|payslips|expense-ledger)$",
        description="Export kind.",
    ),
]
_PeriodIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=_MAX_ID_LEN,
        description="ULID of the target ``pay_period`` row.",
    ),
]
_PayslipIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=_MAX_ID_LEN,
        description="ULID of the target ``payslip`` row.",
    ),
]


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_payroll_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for the payroll surface."""
    api = APIRouter(tags=["payroll"])

    edit_gate = Depends(Permission("pay_rules.edit", scope_kind="workspace"))
    payout_gate = Depends(Permission("payroll.issue_payslip", scope_kind="workspace"))
    period_gate = Depends(Permission("payroll.lock_period", scope_kind="workspace"))
    export_gate = Depends(Permission("payroll.export", scope_kind="workspace"))

    @api.get(
        "/users/{user_id}/pay-rules",
        response_model=PayRuleListResponse,
        operation_id="payroll.pay_rules.list",
        summary="List a user's pay rules — newest effective_from first",
        dependencies=[edit_gate],
    )
    def list_(
        ctx: _Ctx,
        session: _Db,
        user_id: _UserIdPath,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> PayRuleListResponse:
        """Cursor-paginated listing for ``(workspace, user_id)``."""
        after_cursor = decode_cursor(cursor)
        try:
            views = list_rules(
                SqlAlchemyPayRuleRepository(session),
                ctx,
                user_id=user_id,
                limit=limit,
                after_cursor=after_cursor,
            )
        except ValueError as exc:
            # The repo's cursor-split raises ``ValueError`` on a
            # tampered cursor that base64-decodes cleanly but
            # doesn't carry the ``"<isoformat>|<id>"`` shape. Map
            # to 422 so the surface mirrors :func:`decode_cursor`'s
            # ``invalid_cursor`` error envelope.
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_cursor", "message": str(exc)},
            ) from exc
        page = paginate(
            views,
            limit=limit,
            key_getter=cursor_for_view,
        )
        return PayRuleListResponse(
            data=[_view_to_response(v) for v in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @api.post(
        "/users/{user_id}/pay-rules",
        status_code=status.HTTP_201_CREATED,
        response_model=PayRuleResponse,
        operation_id="payroll.pay_rules.create",
        summary="Create a pay rule for a user",
        dependencies=[edit_gate],
    )
    def create(
        body: PayRuleCreateRequest,
        ctx: _Ctx,
        session: _Db,
        user_id: _UserIdPath,
    ) -> PayRuleResponse:
        """Insert a new ``pay_rule`` row for ``user_id``.

        Domain validators reject: currency outside the ISO-4217
        allow-list, multipliers outside ``[1.0, 5.0]``, zero-or-negative
        windows. All three surface as 422 ``pay_rule_invariant``.
        """
        try:
            view = create_rule(
                SqlAlchemyPayRuleRepository(session),
                ctx,
                user_id=user_id,
                body=_request_to_create(body),
            )
        except PayRuleInvariantViolated as exc:
            raise _http_for_invariant(exc) from exc
        return _view_to_response(view)

    @api.get(
        "/pay-rules/{rule_id}",
        response_model=PayRuleResponse,
        operation_id="payroll.pay_rules.get",
        summary="Read a single pay rule",
        dependencies=[edit_gate],
    )
    def get_(
        ctx: _Ctx,
        session: _Db,
        rule_id: _RuleIdPath,
    ) -> PayRuleResponse:
        try:
            view = get_rule(
                SqlAlchemyPayRuleRepository(session),
                ctx,
                rule_id=rule_id,
            )
        except PayRuleNotFound as exc:
            raise _http_for_not_found() from exc
        return _view_to_response(view)

    @api.patch(
        "/pay-rules/{rule_id}",
        response_model=PayRuleResponse,
        operation_id="payroll.pay_rules.update",
        summary="Replace the mutable body of a pay rule",
        dependencies=[edit_gate],
    )
    def update(
        body: PayRuleUpdateRequest,
        ctx: _Ctx,
        session: _Db,
        rule_id: _RuleIdPath,
    ) -> PayRuleResponse:
        """Full-replacement update.

        Refused with 409 ``pay_rule_locked`` if any payslip in a
        paid pay_period already cites this row — historical
        evidence is fixed; callers author a successor row with a
        later ``effective_from`` instead.
        """
        try:
            view = update_rule(
                SqlAlchemyPayRuleRepository(session),
                ctx,
                rule_id=rule_id,
                body=_request_to_update(body),
            )
        except PayRuleNotFound as exc:
            raise _http_for_not_found() from exc
        except PayRuleLocked as exc:
            raise _http_for_locked(exc) from exc
        except PayRuleInvariantViolated as exc:
            raise _http_for_invariant(exc) from exc
        return _view_to_response(view)

    @api.delete(
        "/pay-rules/{rule_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="payroll.pay_rules.delete",
        summary="Soft-retire a pay rule (stamp effective_to = now)",
        dependencies=[edit_gate],
    )
    def delete(
        ctx: _Ctx,
        session: _Db,
        rule_id: _RuleIdPath,
    ) -> Response:
        """Stamp ``effective_to`` so the rule no longer applies forward.

        Pay rules are never hard-deleted (§09 §"Labour-law
        compliance"); historical payslips keep a live FK to the
        row. Refused with 409 if the rule is consumed by a paid
        payslip. No response body per §12 "Deletion".
        """
        try:
            soft_delete_rule(
                SqlAlchemyPayRuleRepository(session),
                ctx,
                rule_id=rule_id,
            )
        except PayRuleNotFound as exc:
            raise _http_for_not_found() from exc
        except PayRuleLocked as exc:
            raise _http_for_locked(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get(
        "/pay-periods",
        response_model=PayPeriodListResponse,
        operation_id="payroll.pay_periods.list",
        summary="List pay periods",
        dependencies=[period_gate],
    )
    def list_pay_periods(
        ctx: _Ctx,
        session: _Db,
    ) -> PayPeriodListResponse:
        views = list_periods(SqlAlchemyPayPeriodRepository(session), ctx)
        return PayPeriodListResponse(data=[_period_to_response(v) for v in views])

    @api.post(
        "/pay-periods",
        status_code=status.HTTP_201_CREATED,
        response_model=PayPeriodResponse,
        operation_id="payroll.pay_periods.create",
        summary="Create a pay period",
        dependencies=[period_gate],
    )
    def create_pay_period(
        body: PayPeriodCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> PayPeriodResponse:
        try:
            view = create_period(
                SqlAlchemyPayPeriodRepository(session),
                ctx,
                starts_at=body.starts_at,
                ends_at=body.ends_at,
            )
        except PayPeriodInvariantViolated as exc:
            raise _http_for_period_invariant(exc) from exc
        return _period_to_response(view)

    @api.get(
        "/pay-periods/{period_id}",
        response_model=PayPeriodResponse,
        operation_id="payroll.pay_periods.get",
        summary="Read a pay period",
        dependencies=[period_gate],
    )
    def get_pay_period(
        ctx: _Ctx,
        session: _Db,
        period_id: _PeriodIdPath,
    ) -> PayPeriodResponse:
        try:
            view = get_period(
                SqlAlchemyPayPeriodRepository(session),
                ctx,
                period_id=period_id,
            )
        except DomainPayPeriodNotFound as exc:
            raise _http_for_period_not_found() from exc
        return _period_to_response(view)

    @api.patch(
        "/pay-periods/{period_id}",
        response_model=PayPeriodResponse,
        operation_id="payroll.pay_periods.update",
        summary="Update an open pay period window",
        dependencies=[period_gate],
    )
    def update_pay_period(
        body: PayPeriodUpdateRequest,
        ctx: _Ctx,
        session: _Db,
        period_id: _PeriodIdPath,
    ) -> PayPeriodResponse:
        try:
            view = update_period(
                SqlAlchemyPayPeriodRepository(session),
                ctx,
                period_id=period_id,
                starts_at=body.starts_at,
                ends_at=body.ends_at,
            )
        except DomainPayPeriodNotFound as exc:
            raise _http_for_period_not_found() from exc
        except PayPeriodInvariantViolated as exc:
            raise _http_for_period_invariant(exc) from exc
        except PayPeriodTransitionConflict as exc:
            raise _http_for_period_conflict(exc) from exc
        return _period_to_response(view)

    @api.delete(
        "/pay-periods/{period_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="payroll.pay_periods.delete",
        summary="Delete an open pay period",
        dependencies=[period_gate],
    )
    def delete_pay_period(
        ctx: _Ctx,
        session: _Db,
        period_id: _PeriodIdPath,
    ) -> Response:
        try:
            delete_period(
                SqlAlchemyPayPeriodRepository(session),
                ctx,
                period_id=period_id,
            )
        except DomainPayPeriodNotFound as exc:
            raise _http_for_period_not_found() from exc
        except PayPeriodTransitionConflict as exc:
            raise _http_for_period_conflict(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post(
        "/pay-periods/{period_id}/lock",
        response_model=PayPeriodResponse,
        operation_id="payroll.pay_periods.lock",
        summary="Lock a pay period and compute draft payslips",
        dependencies=[period_gate],
    )
    def lock_pay_period(
        ctx: _Ctx,
        session: _Db,
        period_id: _PeriodIdPath,
    ) -> PayPeriodResponse:
        repo = SqlAlchemyPayPeriodRepository(session)
        row = repo.get(workspace_id=ctx.workspace_id, period_id=period_id)
        if row is None:
            raise _http_for_period_not_found()
        if row.state == "locked":
            return _period_to_response(get_period(repo, ctx, period_id=period_id))
        try:
            view = lock_period(
                repo,
                ctx,
                period_id=period_id,
                recompute_scheduler=_ImmediatePayPeriodRecomputeScheduler(
                    session,
                    ctx,
                ),
            )
        except DomainPayPeriodNotFound as exc:
            raise _http_for_period_not_found() from exc
        except PayPeriodInvariantViolated as exc:
            raise _http_for_period_invariant(exc) from exc
        except PayPeriodTransitionConflict as exc:
            raise _http_for_period_conflict(exc) from exc
        except (PayslipInvariantViolated, PayslipComputeConflict) as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "payslip_compute_failed", "message": str(exc)},
            ) from exc
        return _period_to_response(view)

    @api.post(
        "/pay-periods/{period_id}/reopen",
        response_model=PayPeriodResponse,
        operation_id="payroll.pay_periods.reopen",
        summary="Reopen a locked pay period",
        dependencies=[period_gate],
    )
    def reopen_pay_period(
        ctx: _Ctx,
        session: _Db,
        period_id: _PeriodIdPath,
    ) -> PayPeriodResponse:
        try:
            view = reopen_period(
                SqlAlchemyPayPeriodRepository(session),
                ctx,
                period_id=period_id,
            )
        except DomainPayPeriodNotFound as exc:
            raise _http_for_period_not_found() from exc
        except PayPeriodTransitionConflict as exc:
            raise _http_for_period_conflict(exc) from exc
        return _period_to_response(view)

    @api.post(
        "/pay-periods/{period_id}/exports",
        response_model=PayrollExportJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="payroll.pay_periods.export",
        summary="Create a payroll export job for a pay period",
        dependencies=[export_gate],
    )
    def create_period_export(
        ctx: _Ctx,
        session: _Db,
        period_id: _PeriodIdPath,
    ) -> PayrollExportJobResponse:
        repo = SqlAlchemyPayPeriodRepository(session)
        if repo.get(workspace_id=ctx.workspace_id, period_id=period_id) is None:
            raise _http_for_period_not_found()
        clock = SystemClock()
        now = clock.now()
        job_id = new_ulid(clock=clock)
        write_audit(
            session,
            ctx,
            entity_kind="payroll_export",
            entity_id=job_id,
            action="payroll_export.ready",
            diff={"kind": "payslips", "pay_period_id": period_id, "status": "ready"},
            clock=clock,
        )
        default_event_bus.publish(
            PayrollExportReady(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=now,
                job_id=job_id,
                pay_period_id=period_id,
                kind="payslips",
            )
        )
        return PayrollExportJobResponse(
            job_id=job_id,
            status="ready",
            kind="payslips",
            pay_period_id=period_id,
        )

    @api.get(
        "/payslips",
        response_model=PayslipListResponse,
        operation_id="payroll.payslips.list",
        summary="List payslips visible to the caller",
    )
    def list_payslips_route(
        ctx: _Ctx,
        session: _Db,
        user_id: Annotated[
            str | None,
            Query(max_length=_MAX_ID_LEN, description="Filter by payslip user."),
        ] = None,
        pay_period_id: Annotated[
            str | None,
            Query(max_length=_MAX_ID_LEN, description="Filter by pay period."),
        ] = None,
    ) -> PayslipListResponse:
        visible_user_id = user_id
        if (
            visible_user_id is None
            and not ctx.actor_was_owner_member
            and ctx.actor_grant_role == "worker"
        ):
            visible_user_id = ctx.actor_id
        needs_view_other = visible_user_id is None or visible_user_id != ctx.actor_id
        if needs_view_other:
            _require_workspace_permission(
                session,
                ctx,
                action_key="payroll.view_other",
            )
        rows = SqlAlchemyPayslipReadRepository(session).list_payslips(
            workspace_id=ctx.workspace_id,
            user_id=visible_user_id,
            pay_period_id=pay_period_id,
        )
        return PayslipListResponse(data=[_payslip_to_response(row) for row in rows])

    @api.get(
        "/payslips/{payslip_id}",
        response_model=PayslipResponse,
        operation_id="payroll.payslips.get",
        summary="Read a payslip visible to the caller",
    )
    def get_payslip_route(
        ctx: _Ctx,
        session: _Db,
        payslip_id: _PayslipIdPath,
    ) -> PayslipResponse:
        row = SqlAlchemyPayslipReadRepository(session).get_payslip(
            workspace_id=ctx.workspace_id,
            payslip_id=payslip_id,
        )
        if row is None:
            raise _http_for_payslip_not_found()
        if row.user_id != ctx.actor_id:
            _require_workspace_permission(
                session,
                ctx,
                action_key="payroll.view_other",
            )
        return _payslip_to_response(row)

    @api.post(
        "/payslips/{payslip_id}/issue",
        response_model=PayslipResponse,
        operation_id="payroll.payslips.issue",
        summary="Issue a draft payslip",
        dependencies=[payout_gate],
    )
    def issue_payslip_route(
        ctx: _Ctx,
        session: _Db,
        payslip_id: _PayslipIdPath,
    ) -> PayslipResponse:
        repo = SqlAlchemyPayslipReadRepository(session)
        row = repo.get_payslip(workspace_id=ctx.workspace_id, payslip_id=payslip_id)
        if row is None:
            raise _http_for_payslip_not_found()
        if row.status == "voided":
            raise _http_for_payslip_conflict("voided payslips cannot be issued")
        if row.status in {"issued", "paid"}:
            return _payslip_to_response(row)

        clock = SystemClock()
        now = clock.now()
        updated = repo.set_payslip_state(
            workspace_id=ctx.workspace_id,
            payslip_id=payslip_id,
            status="issued",
            issued_at=now,
            paid_at=None,
            payout_snapshot_json=_default_payout_snapshot(),
        )
        write_audit(
            session,
            ctx,
            entity_kind="payslip",
            entity_id=payslip_id,
            action="payslip.issued",
            diff={"before": {"status": row.status}, "after": {"status": "issued"}},
            clock=clock,
        )
        return _payslip_to_response(updated)

    @api.post(
        "/payslips/{payslip_id}/mark_paid",
        response_model=PayslipResponse,
        operation_id="payroll.payslips.mark_paid",
        summary="Mark an issued payslip paid",
        dependencies=[payout_gate],
    )
    def mark_payslip_paid_route(
        ctx: _Ctx,
        session: _Db,
        payslip_id: _PayslipIdPath,
    ) -> PayslipResponse:
        payslip_repo = SqlAlchemyPayslipReadRepository(session)
        row = payslip_repo.get_payslip(
            workspace_id=ctx.workspace_id,
            payslip_id=payslip_id,
        )
        if row is None:
            raise _http_for_payslip_not_found()
        if row.status == "paid":
            return _payslip_to_response(row)
        if row.status == "voided":
            raise _http_for_payslip_conflict("voided payslips cannot be paid")
        if row.status != "issued":
            raise _http_for_payslip_conflict("only issued payslips can be paid")

        clock = SystemClock()
        now = clock.now()
        updated = payslip_repo.set_payslip_state(
            workspace_id=ctx.workspace_id,
            payslip_id=payslip_id,
            status="paid",
            issued_at=row.issued_at or now,
            paid_at=now,
        )
        write_audit(
            session,
            ctx,
            entity_kind="payslip",
            entity_id=payslip_id,
            action="payslip.paid",
            diff={"before": {"status": row.status}, "after": {"status": "paid"}},
            clock=clock,
        )

        period_repo = SqlAlchemyPayPeriodRepository(session)
        period = period_repo.get(
            workspace_id=ctx.workspace_id,
            period_id=row.pay_period_id,
        )
        if (
            period is not None
            and period.state == "locked"
            and not period_repo.has_unpaid_payslip(
                workspace_id=ctx.workspace_id,
                period_id=row.pay_period_id,
            )
        ):
            mark_paid(
                period_repo,
                ctx,
                period_id=row.pay_period_id,
                clock=clock,
            )
        return _payslip_to_response(updated)

    @api.post(
        "/payslips/{payslip_id}/void",
        response_model=PayslipResponse,
        operation_id="payroll.payslips.void",
        summary="Void a draft or issued payslip",
        dependencies=[payout_gate],
    )
    def void_payslip_route(
        ctx: _Ctx,
        session: _Db,
        payslip_id: _PayslipIdPath,
    ) -> PayslipResponse:
        repo = SqlAlchemyPayslipReadRepository(session)
        row = repo.get_payslip(workspace_id=ctx.workspace_id, payslip_id=payslip_id)
        if row is None:
            raise _http_for_payslip_not_found()
        if row.status == "paid":
            raise _http_for_payslip_conflict("paid payslips cannot be voided")
        if row.status == "voided":
            return _payslip_to_response(row)

        clock = SystemClock()
        updated = repo.set_payslip_state(
            workspace_id=ctx.workspace_id,
            payslip_id=payslip_id,
            status="voided",
            issued_at=row.issued_at,
            paid_at=None,
        )
        write_audit(
            session,
            ctx,
            entity_kind="payslip",
            entity_id=payslip_id,
            action="payslip.voided",
            diff={"before": {"status": row.status}, "after": {"status": "voided"}},
            clock=clock,
        )
        return _payslip_to_response(updated)

    @api.get(
        "/exports/{kind}.csv",
        operation_id="payroll.exports.csv",
        summary="Stream a payroll CSV export",
        dependencies=[export_gate],
    )
    def export_csv(
        ctx: _Ctx,
        session: _Db,
        kind: _ExportKindPath,
        since: Annotated[
            datetime | None, Query(description="Inclusive UTC start.")
        ] = None,
        until: Annotated[
            datetime | None, Query(description="Exclusive UTC end.")
        ] = None,
        period_id: Annotated[
            str | None,
            Query(max_length=_MAX_ID_LEN, description="Pay period id for payslips."),
        ] = None,
        bom: Annotated[
            bool,
            Query(description="Prefix the CSV with a UTF-8 BOM for spreadsheet tools."),
        ] = False,
        status_filter: Annotated[
            str,
            Query(
                min_length=1,
                max_length=32,
                description="Expense claim state to export; use 'all' for every state.",
            ),
        ] = "approved",
    ) -> StreamingResponse:
        repo = SqlAlchemyPayrollExportRepository(session)
        try:
            if kind == "timesheets":
                if since is None or until is None:
                    raise ExportWindowInvalid("timesheets exports require since/until")
                export = export_timesheets_csv(repo, ctx, since=since, until=until)
            elif kind == "payslips":
                export = export_payslips_csv(
                    repo,
                    ctx,
                    period_id=period_id,
                    since=since,
                    until=until,
                )
            else:
                if since is None or until is None:
                    raise ExportWindowInvalid(
                        "expense ledger exports require since/until"
                    )
                export = export_expense_ledger_csv(
                    repo,
                    ctx,
                    since=since,
                    until=until,
                    status_filter=status_filter,
                )
        except ExportWindowInvalid as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_export_window", "message": str(exc)},
            ) from exc
        except PayPeriodNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "pay_period_not_found", "period_id": str(exc)},
            ) from exc
        except ExpenseStatusInvalid as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_expense_status", "message": str(exc)},
            ) from exc

        return StreamingResponse(
            stream_csv_with_audit(export, repo, ctx, include_bom=bom),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{export.filename}"',
            },
        )

    @api.post(
        "/payslips/{payslip_id}/payout_manifest",
        response_model=PayoutManifestResponse,
        operation_id="payroll.payslips.payout_manifest",
        summary="Stream the payout manifest for a payslip",
        dependencies=[payout_gate],
    )
    def payout_manifest(
        ctx: _Ctx,
        session: _Db,
        payslip_id: Annotated[
            str,
            Path(
                min_length=1,
                max_length=_MAX_ID_LEN,
                description="Payslip id.",
            ),
        ],
    ) -> PayoutManifestResponse:
        if not payout_manifest_available(
            session,
            payslip_id=payslip_id,
            workspace_id=ctx.workspace_id,
        ):
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={"error": "payout_manifest_purged"},
            )
        return PayoutManifestResponse(payslip_id=payslip_id, status="available")

    return api


router = build_payroll_router()
