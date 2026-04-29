"""Daily digest prose capability.

The digest orchestrator owns the structured facts; this capability only turns
those facts into recipient-facing prose. Model output is accepted only when its
numbers, currency amounts, and proper nouns are already present in the
structured payload. Otherwise the capability retries twice and then falls back
to a deterministic template.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.llm.ports import ChatMessage, LLMClient, LLMResponse
from app.domain.llm.budget import (
    BudgetExceeded,
    PricingTable,
    check_budget,
    default_pricing_table,
    estimate_cost_cents,
)
from app.domain.llm.router import CapabilityUnassignedError, ModelPick, resolve_model
from app.domain.llm.usage_recorder import AgentAttribution, record
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "DIGEST_COMPOSE_CAPABILITY",
    "DigestComposeContext",
    "DigestProse",
    "compose",
]


DIGEST_COMPOSE_CAPABILITY: Final[str] = "digest.compose"
_PROJECTED_PROMPT_TOKENS: Final[int] = 2048
_PROJECTED_COMPLETION_TOKENS: Final[int] = 512
_MAX_MODEL_ATTEMPTS: Final[int] = 3

_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w-])\d+(?:[.,]\d+)?(?![\w-])")
_CURRENCY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:[$€£¥]\s*\d+(?:[.,]\d+)?)|(?:\d+(?:[.,]\d+)?\s*(?:USD|EUR|GBP|CAD|AUD|JPY))",
    re.IGNORECASE,
)
_PROPER_NOUN_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:[A-Z][a-z]+|[A-Z]{2,})(?:[\s'-]+(?:[A-Z][a-z]+|[A-Z]{2,}))*\b"
)
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z'-]*")

_PROPER_NOUN_STOP_WORDS: Final[frozenset[str]] = frozenset(
    {
        "A",
        "An",
        "And",
        "Bonjour",
        "Daily",
        "Digest",
        "Good",
        "Hi",
        "No",
        "Nothing",
        "Overdue",
        "Summary",
        "The",
        "There",
        "Today",
        "Upcoming",
        "Voici",
        "You",
    }
)


class DigestProse(BaseModel):
    """Recipient-facing digest prose returned by ``digest.compose``."""

    model_config = ConfigDict(frozen=True)

    body_md: str = Field(min_length=1)
    locale: str
    used_fallback: bool = False
    attempts: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class DigestComposeContext:
    """Dependencies for one daily digest prose call."""

    session: Session
    workspace_ctx: WorkspaceContext
    llm: LLMClient | None
    model_chain: Sequence[ModelPick] | None = None
    pricing: PricingTable | None = None
    attribution: AgentAttribution | None = None
    clock: Clock | None = None


def compose(
    ctx: DigestComposeContext,
    recipient_user_id: str,
    structured_payload: Mapping[str, object],
) -> DigestProse:
    """Compose digest prose from authoritative structured data."""

    clock = ctx.clock if ctx.clock is not None else SystemClock()
    locale = _recipient_locale(ctx.session, recipient_user_id)
    pricing = ctx.pricing if ctx.pricing is not None else default_pricing_table()
    attribution = ctx.attribution or AgentAttribution(
        actor_user_id=ctx.workspace_ctx.actor_id,
        token_id=None,
        agent_label="digest-compose",
    )

    model_chain = (
        tuple(ctx.model_chain)
        if ctx.model_chain is not None
        else resolve_model(
            ctx.session,
            ctx.workspace_ctx,
            DIGEST_COMPOSE_CAPABILITY,
            clock=clock,
        )
    )
    if not model_chain or ctx.llm is None:
        raise CapabilityUnassignedError(
            DIGEST_COMPOSE_CAPABILITY, ctx.workspace_ctx.workspace_id
        )

    correlation_id = new_ulid(clock=clock)
    allowed = _AllowedClaims.from_payload(structured_payload)
    prompt = _prompt(
        recipient_user_id=recipient_user_id,
        locale=locale,
        structured_payload=structured_payload,
    )

    attempts = 0
    for model_pick in model_chain:
        for _ in range(_MAX_MODEL_ATTEMPTS - attempts):
            try:
                _check_budget(ctx, model_pick=model_pick, pricing=pricing, clock=clock)
            except BudgetExceeded:
                return _fallback(
                    structured_payload,
                    locale=locale,
                    attempts=attempts,
                )

            started = clock.now()
            response = ctx.llm.chat(
                model_id=model_pick.api_model_id,
                messages=_messages(prompt=prompt, locale=locale, attempts=attempts),
                max_tokens=model_pick.max_tokens or _PROJECTED_COMPLETION_TOKENS,
                temperature=(
                    model_pick.temperature
                    if model_pick.temperature is not None
                    else 0.2
                ),
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
                fallback_attempts=0,
                attempt=attempts,
            )

            attempts += 1
            text = response.text.strip()
            if text and _valid_against_payload(text, allowed=allowed):
                return DigestProse(
                    body_md=text,
                    locale=locale,
                    used_fallback=False,
                    attempts=attempts,
                )

    return _fallback(structured_payload, locale=locale, attempts=attempts)


def _recipient_locale(session: Session, recipient_user_id: str) -> str:
    user = session.get(User, recipient_user_id)
    if user is None or user.locale is None or not user.locale.strip():
        return "en"
    return user.locale.strip()


def _messages(
    *,
    prompt: str,
    locale: str,
    attempts: int,
) -> Sequence[ChatMessage]:
    language = "French" if _is_french(locale) else "English"
    system = (
        "You write concise daily-digest prose. Use only the structured facts "
        f"provided by the user. Write in {language}. Do not invent numbers, "
        "currency amounts, people, places, task names, or proper nouns."
    )
    if attempts:
        system += " Previous output failed validation; rephrase without adding facts."
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]


def _prompt(
    *,
    recipient_user_id: str,
    locale: str,
    structured_payload: Mapping[str, object],
) -> str:
    payload = json.dumps(structured_payload, ensure_ascii=False, sort_keys=True)
    if _is_french(locale):
        return (
            "Redige le texte du digest quotidien pour ce destinataire. "
            "Les donnees structurees sont la seule source de verite; reformule "
            "sans ajouter de faits.\n"
            f"recipient_user_id: {recipient_user_id}\n"
            f"locale: {locale}\n"
            f"structured_payload: {payload}"
        )
    return (
        "Write the daily digest prose for this recipient. The structured data "
        "is the only source of truth; rephrase it without adding facts.\n"
        f"recipient_user_id: {recipient_user_id}\n"
        f"locale: {locale}\n"
        f"structured_payload: {payload}"
    )


def _check_budget(
    ctx: DigestComposeContext,
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
        capability=DIGEST_COMPOSE_CAPABILITY,
        projected_cost_cents=projected_cost,
        clock=clock,
    )


def _record_usage(
    ctx: DigestComposeContext,
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
        capability=DIGEST_COMPOSE_CAPABILITY,
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


@dataclass(frozen=True, slots=True)
class _AllowedClaims:
    text: str
    numbers: frozenset[str]
    words: frozenset[str]

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> _AllowedClaims:
        values = tuple(_flatten_values(payload))
        text = " ".join(_value_text(value) for value in values)
        words = frozenset(word.lower() for word in _WORD_RE.findall(text))
        numbers: set[str] = set()
        for value in values:
            for token in _NUMBER_RE.findall(_value_text(value)):
                number = _normalise_number(token)
                if number is not None:
                    numbers.add(number)
        return cls(text=text.lower(), numbers=frozenset(numbers), words=words)


def _valid_against_payload(text: str, *, allowed: _AllowedClaims) -> bool:
    for token in _NUMBER_RE.findall(text):
        number = _normalise_number(token)
        if number is not None and number not in allowed.numbers:
            return False

    for token in _CURRENCY_RE.findall(text):
        if token.strip().lower() not in allowed.text:
            return False

    for name in _PROPER_NOUN_RE.findall(text):
        if _proper_noun_allowed(name, allowed=allowed):
            continue
        return False

    return True


def _proper_noun_allowed(name: str, *, allowed: _AllowedClaims) -> bool:
    stripped = " ".join(name.split())
    if stripped in _PROPER_NOUN_STOP_WORDS:
        return True
    lowered = stripped.lower()
    if lowered in allowed.text:
        return True
    words = _WORD_RE.findall(stripped)
    return bool(words) and all(
        word in _PROPER_NOUN_STOP_WORDS or word.lower() in allowed.words
        for word in words
    )


def _flatten_values(value: object) -> Sequence[object]:
    if isinstance(value, Mapping):
        flattened: list[object] = []
        for key, item in value.items():
            flattened.append(key)
            flattened.extend(_flatten_values(item))
        return flattened
    if isinstance(value, list | tuple):
        flattened = []
        for item in value:
            flattened.extend(_flatten_values(item))
        return flattened
    if isinstance(value, str | int | float | Decimal):
        return [value]
    if value is None or isinstance(value, bool):
        return []
    return [str(value)]


def _value_text(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    return str(value)


def _normalise_number(token: str) -> str | None:
    try:
        return str(Decimal(token.replace(",", ".")).normalize())
    except InvalidOperation:
        return None


def _fallback(
    structured_payload: Mapping[str, object],
    *,
    locale: str,
    attempts: int,
) -> DigestProse:
    overdue = _list_items(structured_payload.get("overdue_tasks"))
    anomalies = _list_items(structured_payload.get("anomalies"))
    upcoming = _list_items(structured_payload.get("upcoming_stays"))
    if _is_french(locale):
        lines = ["Bonjour, voici votre digest quotidien."]
        if overdue:
            lines.append("Taches en retard :")
            lines.extend(f"- {_task_line(item, locale=locale)}" for item in overdue)
        else:
            lines.append("Aucune tache en retard.")
        if anomalies:
            lines.append("Anomalies :")
            lines.extend(f"- {_label_for(item)}" for item in anomalies)
        else:
            lines.append("Aucune anomalie.")
        if upcoming:
            lines.append("Sejours a venir :")
            lines.extend(f"- {_label_for(item)}" for item in upcoming)
    else:
        lines = ["Good morning, here is your daily digest."]
        if overdue:
            lines.append("Overdue tasks:")
            lines.extend(f"- {_task_line(item, locale=locale)}" for item in overdue)
        else:
            lines.append("No overdue tasks.")
        if anomalies:
            lines.append("Anomalies:")
            lines.extend(f"- {_label_for(item)}" for item in anomalies)
        else:
            lines.append("No anomalies.")
        if upcoming:
            lines.append("Upcoming stays:")
            lines.extend(f"- {_label_for(item)}" for item in upcoming)

    return DigestProse(
        body_md="\n".join(lines),
        locale=locale,
        used_fallback=True,
        attempts=attempts,
    )


def _list_items(value: object) -> Sequence[Mapping[str, object]]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _task_line(item: Mapping[str, object], *, locale: str) -> str:
    name = _string_field(item, "task_name", "name", "title") or _label_for(item)
    assignee = _string_field(item, "assignee_name", "assignee")
    if assignee:
        if _is_french(locale):
            return f"{name} - assignee : {assignee}"
        return f"{name} - assignee: {assignee}"
    return name


def _label_for(item: Mapping[str, object]) -> str:
    for key in ("label", "name", "title", "summary", "kind"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(item, ensure_ascii=False, sort_keys=True)


def _string_field(item: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_french(locale: str) -> bool:
    return locale.lower().startswith("fr")
