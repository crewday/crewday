"""Receipt OCR capability for expense autofill.

The public entry point is :func:`extract`: callers pass a
:class:`ReceiptOcrContext` and receipt image bytes, and receive a typed
:class:`ReceiptDraft`. The function resolves the ``expenses.autofill``
assignment, preflights the workspace LLM budget, calls the configured LLM
through the existing port, records usage through
:mod:`app.domain.llm.usage_recorder`, then validates the model's JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from app.adapters.llm.ports import LLMClient, LLMResponse
from app.domain.llm.budget import (
    PricingTable,
    check_budget,
    default_pricing_table,
    estimate_cost_cents,
)
from app.domain.llm.consent import load_consent_set
from app.domain.llm.router import CapabilityUnassignedError, ModelPick, resolve_model
from app.domain.llm.usage_recorder import AgentAttribution, record
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.currency import ISO_4217_ALLOWLIST, normalise_currency
from app.util.ulid import new_ulid

__all__ = [
    "AUTOFILL_CAPABILITY",
    "ReceiptDraft",
    "ReceiptOcrContext",
    "ReceiptParseError",
    "extract",
]


AUTOFILL_CAPABILITY: Final[str] = "expenses.autofill"
_PROJECTED_PROMPT_TOKENS: Final[int] = 2048
_PROJECTED_COMPLETION_TOKENS: Final[int] = 512
_LOW_CONFIDENCE_WITHOUT_VENDOR: Final[int] = 40

_PROMPT: Final[str] = (
    "Return only JSON for this receipt OCR text. Shape: "
    '{"vendor": string|null, "amount_cents": integer, '
    '"currency_iso4217": string|null, "occurred_on": "YYYY-MM-DD"|null, '
    '"category": string|null, "confidence_pct": integer 0..100, '
    '"is_receipt": boolean}. If this is not a receipt, set '
    '"is_receipt": false. Do not invent missing vendor names.'
)

_THREE_DECIMAL_CURRENCIES: Final[frozenset[str]] = frozenset(
    {"BHD", "JOD", "KWD", "OMR", "TND"}
)
_ZERO_DECIMAL_CURRENCIES: Final[frozenset[str]] = frozenset(
    {"JPY", "KRW", "VND", "IDR", "CLP"}
)


class ReceiptParseError(ValueError):
    """The LLM output was not a usable receipt draft."""


class _ReceiptNonReceiptError(ReceiptParseError):
    """The LLM explicitly classified the image as not being a receipt."""


class ReceiptDraft(BaseModel):
    """Typed draft returned by the ``expenses.autofill`` capability."""

    model_config = ConfigDict(frozen=True)

    vendor: str | None = Field(default=None, max_length=200)
    amount_cents: int = Field(gt=0)
    currency_iso4217: str = Field(min_length=3, max_length=3)
    occurred_on: date
    category: str | None = Field(default=None, max_length=80)
    confidence_pct: int = Field(ge=0, le=100)

    @field_validator("vendor", "category")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("currency_iso4217")
    @classmethod
    def _currency_is_iso4217(cls, value: str) -> str:
        currency = normalise_currency(value)
        if currency not in ISO_4217_ALLOWLIST:
            raise ValueError(f"currency {value!r} is not in the ISO-4217 allow-list")
        return currency


@dataclass(frozen=True, slots=True)
class ReceiptOcrContext:
    """Dependencies and workspace defaults for one receipt OCR call."""

    session: Session
    workspace_ctx: WorkspaceContext
    llm: LLMClient
    workspace_currency_iso4217: str | None = None
    workspace_timezone: str | None = None
    pricing: PricingTable | None = None
    attribution: AgentAttribution | None = None
    clock: Clock | None = None


def extract(ctx: ReceiptOcrContext, image_bytes: bytes) -> ReceiptDraft:
    """Extract vendor, amount, date, category, and currency from a receipt.

    ``CapabilityUnassignedError`` and ``BudgetExceeded`` are deliberately not
    caught so API callers can surface the existing ``capability_unassigned`` and
    ``budget_exceeded`` domain errors.
    """
    if not image_bytes:
        raise ReceiptParseError("image_bytes is empty; cannot extract receipt data")

    clock = ctx.clock if ctx.clock is not None else SystemClock()
    pricing = ctx.pricing if ctx.pricing is not None else default_pricing_table()
    attribution = ctx.attribution or AgentAttribution(
        actor_user_id=ctx.workspace_ctx.actor_id,
        token_id=None,
        agent_label="expenses-autofill",
    )

    model_chain = resolve_model(
        ctx.session,
        ctx.workspace_ctx,
        AUTOFILL_CAPABILITY,
        clock=clock,
    )
    if not model_chain:
        raise CapabilityUnassignedError(
            AUTOFILL_CAPABILITY, ctx.workspace_ctx.workspace_id
        )

    correlation_id = new_ulid(clock=clock)
    # Workspace consent doesn't change inside the fallback retry loop; load
    # once and reuse so the latency timer below covers the LLM call only.
    consents = load_consent_set(ctx.session, ctx.workspace_ctx.workspace_id)
    last_parse_error: ReceiptParseError | None = None
    for attempt, model_pick in enumerate(model_chain):
        _check_budget(ctx, model_pick=model_pick, pricing=pricing, clock=clock)

        started = clock.now()
        ocr_text = ctx.llm.ocr(
            model_id=model_pick.api_model_id,
            image_bytes=image_bytes,
            consents=consents,
        )
        response = ctx.llm.chat(
            model_id=model_pick.api_model_id,
            messages=[{"role": "user", "content": f"{_PROMPT}\n\n{ocr_text}"}],
            max_tokens=model_pick.max_tokens or _PROJECTED_COMPLETION_TOKENS,
            temperature=(
                model_pick.temperature if model_pick.temperature is not None else 0.0
            ),
            consents=consents,
        )
        latency_ms = max(0, int((clock.now() - started).total_seconds() * 1000))
        _record_usage(
            ctx,
            model_pick=model_pick,
            response=response,
            correlation_id=correlation_id,
            latency_ms=latency_ms,
            pricing=pricing,
            clock=clock,
            attribution=attribution,
            fallback_attempts=attempt,
            attempt=attempt,
        )

        try:
            return _parse_response(response.text, ctx=ctx)
        except _ReceiptNonReceiptError:
            raise
        except ReceiptParseError as exc:
            last_parse_error = exc

    if last_parse_error is not None:
        raise last_parse_error
    raise ReceiptParseError("LLM output was not a usable receipt draft")


def _check_budget(
    ctx: ReceiptOcrContext,
    *,
    model_pick: ModelPick,
    pricing: PricingTable,
    clock: Clock,
) -> None:
    projected_cost = estimate_cost_cents(
        prompt_tokens=_PROJECTED_PROMPT_TOKENS,
        max_output_tokens=_PROJECTED_COMPLETION_TOKENS,
        api_model_id=model_pick.api_model_id,
        pricing=pricing,
        workspace_id=ctx.workspace_ctx.workspace_id,
    )
    check_budget(
        ctx.session,
        ctx.workspace_ctx,
        capability=AUTOFILL_CAPABILITY,
        projected_cost_cents=projected_cost,
        clock=clock,
    )


def _record_usage(
    ctx: ReceiptOcrContext,
    *,
    model_pick: ModelPick,
    response: LLMResponse,
    correlation_id: str,
    latency_ms: int,
    pricing: PricingTable,
    clock: Clock,
    attribution: AgentAttribution,
    fallback_attempts: int,
    attempt: int,
) -> None:
    cost_cents = estimate_cost_cents(
        prompt_tokens=response.usage.prompt_tokens,
        max_output_tokens=response.usage.completion_tokens,
        api_model_id=model_pick.api_model_id,
        pricing=pricing,
        workspace_id=ctx.workspace_ctx.workspace_id,
    )
    record(
        ctx.session,
        ctx.workspace_ctx,
        capability=AUTOFILL_CAPABILITY,
        model_pick=model_pick,
        fallback_attempts=fallback_attempts,
        correlation_id=correlation_id,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        cost_cents=cost_cents,
        latency_ms=latency_ms,
        status="ok",
        finish_reason=response.finish_reason,
        attribution=attribution,
        attempt=attempt,
        clock=clock,
    )


def _parse_response(text: str, *, ctx: ReceiptOcrContext) -> ReceiptDraft:
    raw = _load_json_object(text)
    if _explicit_non_receipt(raw):
        raise _ReceiptNonReceiptError("LLM output classified the image as non-receipt")

    normalised = _normalise_payload(raw, ctx=ctx)
    try:
        draft = ReceiptDraft.model_validate(normalised)
    except ValidationError as exc:
        raise ReceiptParseError(
            f"LLM receipt JSON failed schema validation: "
            f"{exc.errors(include_url=False)!r}"
        ) from exc

    if draft.vendor is None:
        draft = draft.model_copy(
            update={
                "confidence_pct": min(
                    draft.confidence_pct, _LOW_CONFIDENCE_WITHOUT_VENDOR
                )
            }
        )
    return draft


def _load_json_object(text: str) -> dict[str, Any]:
    body = _strip_markdown_fence(text.strip())
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ReceiptParseError(f"LLM output is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ReceiptParseError(
            f"LLM output must be a JSON object; got {type(payload).__name__}"
        )
    return payload


def _strip_markdown_fence(text: str) -> str:
    if not text.startswith("```") or not text.endswith("```"):
        return text
    inner = text[3:-3].strip()
    if inner.lower().startswith("json"):
        inner = inner[4:].lstrip()
    return inner


def _explicit_non_receipt(payload: dict[str, Any]) -> bool:
    marker = payload.get("is_receipt", payload.get("receipt"))
    if marker is False:
        return True
    document_type = payload.get("document_type")
    return isinstance(document_type, str) and document_type.strip().lower() in {
        "non-receipt",
        "not_receipt",
        "other",
    }


def _normalise_payload(
    payload: dict[str, Any], *, ctx: ReceiptOcrContext
) -> dict[str, Any]:
    vendor = _optional_string(_field_value(payload, "vendor"))
    currency = _normalise_currency_value(
        _field_value(payload, "currency_iso4217", "currency"),
        ctx=ctx,
    )
    amount_cents = _amount_cents(payload, currency=currency)
    occurred_on = _occurred_on(
        _field_value(payload, "occurred_on", "purchased_on", "purchased_at", "date"),
        ctx=ctx,
    )
    confidence_pct = _confidence_pct(payload, vendor=vendor)

    return {
        "vendor": vendor,
        "amount_cents": amount_cents,
        "currency_iso4217": currency,
        "occurred_on": occurred_on,
        "category": _optional_string(_field_value(payload, "category")),
        "confidence_pct": confidence_pct,
    }


def _first_present(payload: dict[str, Any], *keys: str) -> Any | None:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _field_value(payload: dict[str, Any], *keys: str) -> Any | None:
    for key in keys:
        raw = _first_present(payload, key)
        if raw is None:
            continue
        if isinstance(raw, dict):
            value = raw.get("value")
            if value is not None:
                return value
            continue
        return raw
    return None


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReceiptParseError(f"expected string or null; got {type(value).__name__}")
    stripped = value.strip()
    return stripped or None


def _normalise_currency_value(value: Any | None, *, ctx: ReceiptOcrContext) -> str:
    raw = value if value is not None else ctx.workspace_currency_iso4217
    if not isinstance(raw, str):
        raise ReceiptParseError(
            "LLM output omitted currency and no workspace currency default was provided"
        )
    currency = normalise_currency(raw)
    if currency not in ISO_4217_ALLOWLIST:
        raise ReceiptParseError(f"currency {raw!r} is not in the ISO-4217 allow-list")
    return currency


def _occurred_on(value: Any | None, *, ctx: ReceiptOcrContext) -> date:
    if value is None:
        return _local_today(ctx)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ReceiptParseError(
            f"occurred_on must be an ISO date string; got {type(value).__name__}"
        )

    stripped = value.strip()
    if not stripped:
        return _local_today(ctx)
    try:
        return date.fromisoformat(stripped)
    except ValueError:
        try:
            return datetime.fromisoformat(stripped.replace("Z", "+00:00")).date()
        except ValueError as exc:
            raise ReceiptParseError(
                f"occurred_on is not an ISO date: {value!r}"
            ) from exc


def _local_today(ctx: ReceiptOcrContext) -> date:
    if ctx.workspace_timezone is None:
        raise ReceiptParseError(
            "LLM output omitted occurred_on and no workspace timezone was provided"
        )
    try:
        tz = ZoneInfo(ctx.workspace_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ReceiptParseError(
            f"workspace timezone {ctx.workspace_timezone!r} is not valid"
        ) from exc
    clock = ctx.clock if ctx.clock is not None else SystemClock()
    return clock.now().astimezone(tz).date()


def _amount_cents(payload: dict[str, Any], *, currency: str) -> int:
    raw_cents = _field_value(payload, "amount_cents", "total_amount_cents")
    if raw_cents is not None:
        if isinstance(raw_cents, bool) or not isinstance(raw_cents, int):
            raise ReceiptParseError("amount_cents must be an integer")
        return int(raw_cents)

    raw_amount = _field_value(payload, "amount", "total_amount")
    if raw_amount is None:
        raise ReceiptParseError("LLM output omitted amount_cents")
    try:
        amount = Decimal(str(raw_amount))
    except (InvalidOperation, ValueError) as exc:
        raise ReceiptParseError(f"amount is not numeric: {raw_amount!r}") from exc
    if amount <= 0:
        raise ReceiptParseError(f"amount must be positive; got {amount}")

    if currency in _ZERO_DECIMAL_CURRENCIES:
        scale = Decimal(1)
    elif currency in _THREE_DECIMAL_CURRENCIES:
        scale = Decimal(1000)
    else:
        scale = Decimal(100)
    cents = (amount * scale).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    return int(cents)


def _confidence_pct(payload: dict[str, Any], *, vendor: str | None) -> int:
    values: list[float] = []
    raw = payload.get("confidence_pct")
    if raw is None:
        raw = payload.get("confidence")
    if isinstance(raw, dict):
        values.extend(
            _confidence_value(value, label=f"confidence[{key!r}]")
            for key, value in raw.items()
        )
    elif raw is not None:
        values.append(_confidence_value(raw, label="confidence_pct"))

    values.extend(_field_confidence_values(payload))
    pct = round(min(values)) if values else _default_confidence(vendor=vendor)
    if vendor is None:
        return min(pct, _LOW_CONFIDENCE_WITHOUT_VENDOR)
    return pct


def _field_confidence_values(payload: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for key in (
        "vendor",
        "amount",
        "amount_cents",
        "total_amount",
        "total_amount_cents",
        "currency",
        "currency_iso4217",
        "occurred_on",
        "purchased_on",
        "purchased_at",
        "date",
        "category",
    ):
        raw = payload.get(key)
        if isinstance(raw, dict) and raw.get("confidence") is not None:
            values.append(
                _confidence_value(raw["confidence"], label=f"{key}.confidence")
            )
    return values


def _confidence_value(raw: object, *, label: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ReceiptParseError(f"{label} must be numeric")
    numeric = float(raw)
    if 0.0 <= numeric <= 1.0:
        numeric *= 100
    if not (0.0 <= numeric <= 100.0):
        raise ReceiptParseError(f"{label} {numeric} is outside 0..100")
    return numeric


def _default_confidence(*, vendor: str | None) -> int:
    if vendor is None:
        return _LOW_CONFIDENCE_WITHOUT_VENDOR
    return 70
