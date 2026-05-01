"""Public-holiday CRUD service for workspace-managed scheduling dates."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import ColumnElement

from app.adapters.db.holidays.models import PublicHoliday
from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "PublicHolidayConflict",
    "PublicHolidayCreate",
    "PublicHolidayListFilter",
    "PublicHolidayNotFound",
    "PublicHolidaySchedulingEffect",
    "PublicHolidayUpdate",
    "PublicHolidayView",
    "create_public_holiday",
    "delete_public_holiday",
    "get_public_holiday",
    "list_public_holidays",
    "update_public_holiday",
]

PublicHolidaySchedulingEffect = Literal["block", "allow", "reduced"]
PublicHolidayRecurrence = Literal["annual"]

_MAX_NAME_LEN = 160
_MAX_NOTES_LEN = 20_000


class PublicHolidayNotFound(LookupError):
    """No live public-holiday row exists for this id in the workspace."""


class PublicHolidayConflict(ValueError):
    """A live row already occupies the same workspace/date/country slot."""


class _PublicHolidayBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    date: dt.date
    country: str | None = Field(default=None, min_length=2, max_length=2)
    scheduling_effect: PublicHolidaySchedulingEffect
    reduced_starts_local: dt.time | None = None
    reduced_ends_local: dt.time | None = None
    payroll_multiplier: Decimal | None = Field(default=None, ge=Decimal("0"))
    recurrence: PublicHolidayRecurrence | None = None
    notes_md: str | None = Field(default=None, max_length=_MAX_NOTES_LEN)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must be a non-blank string")
        return stripped

    @field_validator("country")
    @classmethod
    def _normalise_country(cls, value: str | None) -> str | None:
        if value is None:
            return None
        country = value.strip().upper()
        if len(country) != 2 or not country.isalpha():
            raise ValueError("country must be an ISO-3166-1 alpha-2 code")
        return country

    @model_validator(mode="after")
    def _validate_reduced_hours(self) -> _PublicHolidayBase:
        has_start = self.reduced_starts_local is not None
        has_end = self.reduced_ends_local is not None
        if self.scheduling_effect == "reduced":
            if not has_start or not has_end:
                raise ValueError(
                    "reduced_starts_local and reduced_ends_local are required "
                    "when scheduling_effect is reduced"
                )
        elif has_start or has_end:
            raise ValueError(
                "reduced_starts_local and reduced_ends_local are only allowed "
                "when scheduling_effect is reduced"
            )
        return self


class PublicHolidayCreate(_PublicHolidayBase):
    """Full create body for a public-holiday row."""


class PublicHolidayUpdate(BaseModel):
    """Explicit-sparse partial update body for a public-holiday row."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME_LEN)
    date: dt.date | None = None
    country: str | None = Field(default=None, min_length=2, max_length=2)
    scheduling_effect: PublicHolidaySchedulingEffect | None = None
    reduced_starts_local: dt.time | None = None
    reduced_ends_local: dt.time | None = None
    payroll_multiplier: Decimal | None = Field(default=None, ge=Decimal("0"))
    recurrence: PublicHolidayRecurrence | None = None
    notes_md: str | None = Field(default=None, max_length=_MAX_NOTES_LEN)

    @field_validator("country")
    @classmethod
    def _normalise_country(cls, value: str | None) -> str | None:
        if value is None:
            return None
        country = value.strip().upper()
        if len(country) != 2 or not country.isalpha():
            raise ValueError("country must be an ISO-3166-1 alpha-2 code")
        return country

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must be a non-blank string")
        return stripped


@dataclass(frozen=True, slots=True)
class PublicHolidayListFilter:
    starts_on: dt.date | None = None
    ends_on: dt.date | None = None
    country: str | None = None


@dataclass(frozen=True, slots=True)
class PublicHolidayView:
    id: str
    workspace_id: str
    name: str
    date: dt.date
    country: str | None
    scheduling_effect: str
    reduced_starts_local: dt.time | None
    reduced_ends_local: dt.time | None
    payroll_multiplier: Decimal | None
    recurrence: str | None
    notes_md: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    deleted_at: dt.datetime | None


def _row_to_view(row: PublicHoliday) -> PublicHolidayView:
    return PublicHolidayView(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        date=row.date,
        country=row.country,
        scheduling_effect=row.scheduling_effect,
        reduced_starts_local=row.reduced_starts_local,
        reduced_ends_local=row.reduced_ends_local,
        payroll_multiplier=row.payroll_multiplier,
        recurrence=row.recurrence,
        notes_md=row.notes_md,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _load_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    public_holiday_id: str,
) -> PublicHoliday:
    row = session.scalar(
        select(PublicHoliday).where(
            PublicHoliday.id == public_holiday_id,
            PublicHoliday.workspace_id == ctx.workspace_id,
            PublicHoliday.deleted_at.is_(None),
        )
    )
    if row is None:
        raise PublicHolidayNotFound(public_holiday_id)
    return row


def _normalise_filter_country(country: str | None) -> str | None:
    if country is None:
        return None
    value = country.strip().upper()
    if len(value) != 2 or not value.isalpha():
        raise ValueError("country must be an ISO-3166-1 alpha-2 code")
    return value


def _annual_matches_window(
    reference: dt.date,
    *,
    starts_on: dt.date | None,
    ends_on: dt.date | None,
) -> bool:
    if starts_on is None or ends_on is None:
        return True
    for year in range(starts_on.year, ends_on.year + 1):
        try:
            occurrence = dt.date(year, reference.month, reference.day)
        except ValueError:
            continue
        if starts_on <= occurrence <= ends_on:
            return True
    return False


def _matches_date_filter(row: PublicHoliday, filters: PublicHolidayListFilter) -> bool:
    if filters.starts_on is not None and filters.ends_on is not None:
        if row.recurrence == "annual":
            return _annual_matches_window(
                row.date, starts_on=filters.starts_on, ends_on=filters.ends_on
            )
        return filters.starts_on <= row.date <= filters.ends_on
    if filters.starts_on is not None and row.recurrence != "annual":
        return row.date >= filters.starts_on
    if filters.ends_on is not None and row.recurrence != "annual":
        return row.date <= filters.ends_on
    return True


def _live_conflict_exists(
    session: Session,
    ctx: WorkspaceContext,
    *,
    holiday_date: dt.date,
    country: str | None,
    exclude_id: str | None = None,
) -> bool:
    stmt = select(PublicHoliday.id).where(
        PublicHoliday.workspace_id == ctx.workspace_id,
        PublicHoliday.date == holiday_date,
        PublicHoliday.deleted_at.is_(None),
    )
    if country is None:
        stmt = stmt.where(PublicHoliday.country.is_(None))
    else:
        stmt = stmt.where(PublicHoliday.country == country)
    if exclude_id is not None:
        stmt = stmt.where(PublicHoliday.id != exclude_id)
    return session.scalar(stmt.limit(1)) is not None


def _raise_conflict(holiday_date: dt.date, country: str | None) -> None:
    slot = "workspace-wide" if country is None else country
    raise PublicHolidayConflict(
        f"public_holiday for {holiday_date.isoformat()} and {slot} already exists"
    )


def _after_boundary(after_date: dt.date, after_id: str) -> ColumnElement[bool]:
    return or_(
        PublicHoliday.date > after_date,
        and_(PublicHoliday.date == after_date, PublicHoliday.id > after_id),
    )


def _audit_value(value: Any) -> Any:
    if isinstance(value, dt.date | dt.datetime | dt.time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def list_public_holidays(
    session: Session,
    ctx: WorkspaceContext,
    *,
    filters: PublicHolidayListFilter,
    limit: int,
    after: tuple[dt.date, str] | None = None,
) -> Sequence[PublicHolidayView]:
    starts_on = filters.starts_on
    ends_on = filters.ends_on
    country = _normalise_filter_country(filters.country)
    filters = PublicHolidayListFilter(
        starts_on=starts_on, ends_on=ends_on, country=country
    )

    stmt = select(PublicHoliday).where(
        PublicHoliday.workspace_id == ctx.workspace_id,
        PublicHoliday.deleted_at.is_(None),
    )
    if country is not None:
        stmt = stmt.where(
            or_(PublicHoliday.country.is_(None), PublicHoliday.country == country)
        )
    if starts_on is not None and ends_on is not None:
        stmt = stmt.where(
            or_(
                PublicHoliday.recurrence == "annual",
                PublicHoliday.date.between(starts_on, ends_on),
            )
        )
    elif starts_on is not None:
        stmt = stmt.where(
            or_(PublicHoliday.recurrence == "annual", PublicHoliday.date >= starts_on)
        )
    elif ends_on is not None:
        stmt = stmt.where(
            or_(PublicHoliday.recurrence == "annual", PublicHoliday.date <= ends_on)
        )

    ordered = stmt.order_by(PublicHoliday.date.asc(), PublicHoliday.id.asc())
    batch_size = max(limit + 1, 100)
    matched: list[PublicHoliday] = []
    page_after = after
    while len(matched) <= limit:
        batch_stmt = ordered
        if page_after is not None:
            page_after_date, page_after_id = page_after
            batch_stmt = batch_stmt.where(
                _after_boundary(page_after_date, page_after_id)
            )
        batch = session.scalars(batch_stmt.limit(batch_size)).all()
        if not batch:
            break
        matched.extend(row for row in batch if _matches_date_filter(row, filters))
        if len(batch) < batch_size:
            break
        last = batch[-1]
        page_after = (last.date, last.id)

    return [_row_to_view(row) for row in matched[: limit + 1]]


def get_public_holiday(
    session: Session,
    ctx: WorkspaceContext,
    *,
    public_holiday_id: str,
) -> PublicHolidayView:
    return _row_to_view(_load_row(session, ctx, public_holiday_id=public_holiday_id))


def create_public_holiday(
    session: Session,
    ctx: WorkspaceContext,
    *,
    body: PublicHolidayCreate,
    clock: Clock | None = None,
) -> PublicHolidayView:
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    if _live_conflict_exists(
        session, ctx, holiday_date=body.date, country=body.country
    ):
        _raise_conflict(body.date, body.country)

    row_id = new_ulid(clock=clock)
    row = PublicHoliday(
        id=row_id,
        workspace_id=ctx.workspace_id,
        name=body.name,
        date=body.date,
        country=body.country,
        scheduling_effect=body.scheduling_effect,
        reduced_starts_local=body.reduced_starts_local,
        reduced_ends_local=body.reduced_ends_local,
        payroll_multiplier=body.payroll_multiplier,
        recurrence=body.recurrence,
        notes_md=body.notes_md,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        _raise_conflict(body.date, body.country)
        raise AssertionError("unreachable") from exc

    write_audit(
        session,
        ctx,
        entity_kind="public_holiday",
        entity_id=row_id,
        action="public_holiday.created",
        diff={"name": row.name, "date": row.date.isoformat(), "country": row.country},
        clock=resolved_clock,
    )
    return _row_to_view(row)


def update_public_holiday(
    session: Session,
    ctx: WorkspaceContext,
    *,
    public_holiday_id: str,
    body: PublicHolidayUpdate,
    clock: Clock | None = None,
) -> PublicHolidayView:
    resolved_clock = clock if clock is not None else SystemClock()
    row = _load_row(session, ctx, public_holiday_id=public_holiday_id)
    sent = body.model_fields_set
    if not sent:
        return _row_to_view(row)

    values: dict[str, Any] = {
        "name": row.name,
        "date": row.date,
        "country": row.country,
        "scheduling_effect": row.scheduling_effect,
        "reduced_starts_local": row.reduced_starts_local,
        "reduced_ends_local": row.reduced_ends_local,
        "payroll_multiplier": row.payroll_multiplier,
        "recurrence": row.recurrence,
        "notes_md": row.notes_md,
    }
    for field in sent:
        values[field] = getattr(body, field)
    validated = PublicHolidayCreate.model_validate(values)

    slot_changed = validated.date != row.date or validated.country != row.country
    if slot_changed and _live_conflict_exists(
        session,
        ctx,
        holiday_date=validated.date,
        country=validated.country,
        exclude_id=row.id,
    ):
        _raise_conflict(validated.date, validated.country)

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for field, new_value in validated.model_dump().items():
        old_value = getattr(row, field)
        if old_value != new_value:
            before[field] = _audit_value(old_value)
            after[field] = _audit_value(new_value)
            setattr(row, field, new_value)

    if not after:
        return _row_to_view(row)

    row.updated_at = resolved_clock.now()
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        _raise_conflict(validated.date, validated.country)
        raise AssertionError("unreachable") from exc

    write_audit(
        session,
        ctx,
        entity_kind="public_holiday",
        entity_id=row.id,
        action="public_holiday.updated",
        diff={"before": before, "after": after},
        clock=resolved_clock,
    )
    return _row_to_view(row)


def delete_public_holiday(
    session: Session,
    ctx: WorkspaceContext,
    *,
    public_holiday_id: str,
    clock: Clock | None = None,
) -> None:
    resolved_clock = clock if clock is not None else SystemClock()
    row = _load_row(session, ctx, public_holiday_id=public_holiday_id)
    now = resolved_clock.now()
    row.deleted_at = now
    row.updated_at = now
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="public_holiday",
        entity_id=row.id,
        action="public_holiday.deleted",
        diff={"deleted_at": now.isoformat()},
        clock=resolved_clock,
    )
