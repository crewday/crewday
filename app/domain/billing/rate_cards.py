"""Billing rate-card service."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from sqlalchemy.orm import Session

from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.currency import is_valid_currency, normalise_currency
from app.util.ulid import new_ulid

__all__ = [
    "RateCardCreate",
    "RateCardInvalid",
    "RateCardNotFound",
    "RateCardOrganizationRow",
    "RateCardPatch",
    "RateCardRepository",
    "RateCardRow",
    "RateCardService",
    "RateCardView",
]

_MUTABLE_FIELDS = frozenset({"label", "currency", "rates", "active_from", "active_to"})


class RateCardInvalid(ValueError):
    """The requested rate-card mutation violates the billing contract."""


class RateCardNotFound(LookupError):
    """The rate card or covered service does not exist in the caller's workspace."""


@dataclass(frozen=True, slots=True)
class RateCardOrganizationRow:
    id: str
    workspace_id: str
    kind: str
    default_currency: str


@dataclass(frozen=True, slots=True)
class RateCardRow:
    id: str
    workspace_id: str
    organization_id: str
    label: str
    currency: str
    rates: Mapping[str, int]
    active_from: date
    active_to: date | None


@dataclass(frozen=True, slots=True)
class RateCardView:
    id: str
    workspace_id: str
    organization_id: str
    label: str
    currency: str
    rates: Mapping[str, int]
    active_from: date
    active_to: date | None


@dataclass(frozen=True, slots=True)
class RateCardCreate:
    label: str
    rates: Mapping[str, object]
    active_from: date
    active_to: date | None = None
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class RateCardPatch:
    fields: Mapping[str, object | None]


class RateCardRepository(Protocol):
    @property
    def session(self) -> Session: ...

    def get_organization(
        self, *, workspace_id: str, organization_id: str, for_update: bool = False
    ) -> RateCardOrganizationRow | None: ...

    def insert(
        self,
        *,
        rate_card_id: str,
        workspace_id: str,
        organization_id: str,
        label: str,
        currency: str,
        rates: Mapping[str, int],
        active_from: date,
        active_to: date | None,
    ) -> RateCardRow: ...

    def list(
        self, *, workspace_id: str, organization_id: str
    ) -> Sequence[RateCardRow]: ...

    def get(
        self,
        *,
        workspace_id: str,
        organization_id: str,
        rate_card_id: str,
        for_update: bool = False,
    ) -> RateCardRow | None: ...

    def update_fields(
        self,
        *,
        workspace_id: str,
        organization_id: str,
        rate_card_id: str,
        fields: Mapping[str, object | None],
    ) -> RateCardRow: ...


class RateCardService:
    """Workspace-scoped billing rate-card use cases."""

    def __init__(self, ctx: WorkspaceContext, *, clock: Clock | None = None) -> None:
        self._ctx = ctx
        self._clock = clock if clock is not None else SystemClock()

    def create(
        self, repo: RateCardRepository, organization_id: str, body: RateCardCreate
    ) -> RateCardView:
        organization = self._get_client_organization(
            repo, organization_id, for_update=True
        )
        currency_value = body.currency
        if currency_value is None:
            currency_value = organization.default_currency
        currency = _validate_currency(currency_value)
        label = _clean_required(body.label, field="label")
        rates = _clean_rates(body.rates)
        _validate_window(body.active_from, body.active_to)
        self._reject_overlaps(
            repo,
            organization_id=organization.id,
            rates=rates,
            active_from=body.active_from,
            active_to=body.active_to,
            exclude_id=None,
        )
        row = repo.insert(
            rate_card_id=new_ulid(),
            workspace_id=self._ctx.workspace_id,
            organization_id=organization.id,
            label=label,
            currency=currency,
            rates=rates,
            active_from=body.active_from,
            active_to=body.active_to,
        )
        view = _to_view(row)
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="rate_card",
            entity_id=view.id,
            action="billing.rate_card.created",
            diff={"after": _audit_shape(view)},
            clock=self._clock,
        )
        return view

    def list(
        self, repo: RateCardRepository, organization_id: str
    ) -> list[RateCardView]:
        organization = self._get_client_organization(repo, organization_id)
        rows = repo.list(
            workspace_id=self._ctx.workspace_id,
            organization_id=organization.id,
        )
        return [_to_view(row) for row in rows]

    def get(
        self, repo: RateCardRepository, organization_id: str, rate_card_id: str
    ) -> RateCardView:
        organization = self._get_client_organization(repo, organization_id)
        row = repo.get(
            workspace_id=self._ctx.workspace_id,
            organization_id=organization.id,
            rate_card_id=rate_card_id,
        )
        if row is None:
            raise RateCardNotFound("rate card not found")
        return _to_view(row)

    def update(
        self,
        repo: RateCardRepository,
        organization_id: str,
        rate_card_id: str,
        patch: RateCardPatch,
    ) -> RateCardView:
        if not patch.fields:
            raise RateCardInvalid("PATCH body must include at least one field")
        unknown = sorted(set(patch.fields) - _MUTABLE_FIELDS)
        if unknown:
            raise RateCardInvalid(f"unknown rate-card fields: {', '.join(unknown)}")
        organization = self._get_client_organization(
            repo, organization_id, for_update=True
        )
        current = repo.get(
            workspace_id=self._ctx.workspace_id,
            organization_id=organization.id,
            rate_card_id=rate_card_id,
            for_update=True,
        )
        if current is None:
            raise RateCardNotFound("rate card not found")

        fields = self._normalize_patch(current, patch)
        target_rates = fields.get("rates_json", current.rates)
        if not isinstance(target_rates, Mapping):
            raise RateCardInvalid("rates must be an object")
        clean_rates = _clean_rates(target_rates)
        fields["rates_json"] = clean_rates
        target_from = fields.get("active_from", current.active_from)
        target_to = fields.get("active_to", current.active_to)
        if not isinstance(target_from, date):
            raise RateCardInvalid("active_from must be a date")
        if target_to is not None and not isinstance(target_to, date):
            raise RateCardInvalid("active_to must be a date or null")
        _validate_window(target_from, target_to)
        self._reject_overlaps(
            repo,
            organization_id=organization.id,
            rates=clean_rates,
            active_from=target_from,
            active_to=target_to,
            exclude_id=current.id,
        )

        changed = {
            key: value
            for key, value in fields.items()
            if _field_value(current, key) != value
        }
        if not changed:
            return _to_view(current)
        updated = repo.update_fields(
            workspace_id=self._ctx.workspace_id,
            organization_id=organization.id,
            rate_card_id=rate_card_id,
            fields=changed,
        )
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="rate_card",
            entity_id=updated.id,
            action="billing.rate_card.updated",
            diff={
                "changed": sorted(_external_field(key) for key in changed),
                "before": _audit_shape(_to_view(current)),
                "after": _audit_shape(_to_view(updated)),
            },
            clock=self._clock,
        )
        return _to_view(updated)

    def resolve(
        self,
        repo: RateCardRepository,
        organization_id: str,
        service_key: str,
        *,
        on: date,
    ) -> int:
        organization = self._get_client_organization(repo, organization_id)
        clean_key = _clean_required(service_key, field="service_key")
        rows = repo.list(
            workspace_id=self._ctx.workspace_id,
            organization_id=organization.id,
        )
        matches = [
            row for row in rows if _covers(row, on=on) and clean_key in row.rates
        ]
        if not matches:
            raise RateCardNotFound(
                f"no rate card covers service {clean_key!r} on {on.isoformat()}"
            )
        matches.sort(key=lambda row: (row.active_from, row.id), reverse=True)
        return matches[0].rates[clean_key]

    def _normalize_patch(
        self, current: RateCardRow, patch: RateCardPatch
    ) -> dict[str, object | None]:
        fields: dict[str, object | None] = {}
        for key, value in patch.fields.items():
            if key == "label":
                if not isinstance(value, str):
                    raise RateCardInvalid("label must be a string")
                fields[key] = _clean_required(value, field="label")
            elif key == "currency":
                if not isinstance(value, str):
                    raise RateCardInvalid("currency must be a string")
                fields[key] = _validate_currency(value)
            elif key == "rates":
                if not isinstance(value, Mapping):
                    raise RateCardInvalid("rates must be an object")
                fields["rates_json"] = _clean_rates(value)
            elif key == "active_from":
                if not isinstance(value, date):
                    raise RateCardInvalid("active_from must be a date")
                fields[key] = value
            elif key == "active_to":
                if value is not None and not isinstance(value, date):
                    raise RateCardInvalid("active_to must be a date or null")
                fields[key] = value
        if "rates_json" not in fields:
            fields["rates_json"] = dict(current.rates)
        return fields

    def _get_client_organization(
        self,
        repo: RateCardRepository,
        organization_id: str,
        *,
        for_update: bool = False,
    ) -> RateCardOrganizationRow:
        clean_id = _clean_required(organization_id, field="organization_id")
        row = repo.get_organization(
            workspace_id=self._ctx.workspace_id,
            organization_id=clean_id,
            for_update=for_update,
        )
        if row is None:
            raise RateCardNotFound("organization not found")
        if row.kind == "vendor":
            raise RateCardInvalid("vendor-only organizations cannot have rate cards")
        return row

    def _reject_overlaps(
        self,
        repo: RateCardRepository,
        *,
        organization_id: str,
        rates: Mapping[str, int],
        active_from: date,
        active_to: date | None,
        exclude_id: str | None,
    ) -> None:
        for row in repo.list(
            workspace_id=self._ctx.workspace_id,
            organization_id=organization_id,
        ):
            if row.id == exclude_id:
                continue
            services = sorted(set(row.rates) & set(rates))
            if services and _windows_overlap(
                active_from,
                active_to,
                row.active_from,
                row.active_to,
            ):
                raise RateCardInvalid(
                    "rate card overlaps existing window for services: "
                    + ", ".join(services)
                )


def _field_value(row: RateCardRow, key: str) -> object | None:
    if key == "rates_json":
        return dict(row.rates)
    if key == "label":
        return row.label
    if key == "currency":
        return row.currency
    if key == "active_from":
        return row.active_from
    if key == "active_to":
        return row.active_to
    raise KeyError(key)


def _external_field(key: str) -> str:
    if key == "rates_json":
        return "rates"
    return key


def _clean_required(value: str, *, field: str) -> str:
    clean = value.strip()
    if not clean:
        raise RateCardInvalid(f"{field} is required")
    return clean


def _clean_rates(value: Mapping[str, object]) -> dict[str, int]:
    if not value:
        raise RateCardInvalid("rates must include at least one service")
    clean: dict[str, int] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise RateCardInvalid("rate service keys must be strings")
        key = raw_key.strip()
        if not key:
            raise RateCardInvalid("rate service keys cannot be blank")
        if (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, int)
            or raw_value <= 0
        ):
            raise RateCardInvalid(
                f"rate for service {key!r} must be a positive integer"
            )
        clean[key] = raw_value
    return clean


def _validate_currency(value: str) -> str:
    currency = normalise_currency(value)
    if not is_valid_currency(currency):
        raise RateCardInvalid(f"currency {value!r} is not a valid ISO-4217 code")
    return currency


def _validate_window(active_from: date, active_to: date | None) -> None:
    if active_to is not None and active_to <= active_from:
        raise RateCardInvalid("active_to must be after active_from")


def _covers(row: RateCardRow, *, on: date) -> bool:
    return row.active_from <= on and (row.active_to is None or on < row.active_to)


def _windows_overlap(
    left_from: date,
    left_to: date | None,
    right_from: date,
    right_to: date | None,
) -> bool:
    left_ends_after_right_starts = left_to is None or right_from < left_to
    right_ends_after_left_starts = right_to is None or left_from < right_to
    return left_ends_after_right_starts and right_ends_after_left_starts


def _to_view(row: RateCardRow) -> RateCardView:
    return RateCardView(
        id=row.id,
        workspace_id=row.workspace_id,
        organization_id=row.organization_id,
        label=row.label,
        currency=row.currency,
        rates=dict(row.rates),
        active_from=row.active_from,
        active_to=row.active_to,
    )


def _audit_shape(view: RateCardView) -> dict[str, object]:
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "organization_id": view.organization_id,
        "label": view.label,
        "currency": view.currency,
        "rates": dict(view.rates),
        "active_from": view.active_from.isoformat(),
        "active_to": view.active_to.isoformat() if view.active_to is not None else None,
    }
