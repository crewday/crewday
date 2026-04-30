"""Billing quote service."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Literal, Protocol

from jinja2 import Template
from sqlalchemy.orm import Session

from app.adapters.mail.ports import Mailer
from app.audit import write_audit
from app.events import EventBus, QuoteDecided
from app.events import bus as default_bus
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.currency import is_valid_currency, normalise_currency
from app.util.ulid import new_ulid

__all__ = [
    "QuoteCreate",
    "QuoteDecision",
    "QuoteInvalid",
    "QuoteNotFound",
    "QuotePatch",
    "QuoteRepository",
    "QuoteRow",
    "QuoteService",
    "QuoteTokenInvalid",
    "QuoteView",
]

_MUTABLE_FIELDS = frozenset(
    {"organization_id", "property_id", "title", "body_md", "total_cents", "currency"}
)
_TOKEN_TTL = timedelta(days=30)
_TOKEN_VERSION = 1


class QuoteInvalid(ValueError):
    """The requested quote mutation violates the billing contract."""


class QuoteNotFound(LookupError):
    """The quote does not exist in the caller's workspace."""


class QuoteTokenInvalid(ValueError):
    """The public quote decision token is expired, tampered, or mismatched."""


@dataclass(frozen=True, slots=True)
class QuoteRow:
    id: str
    workspace_id: str
    organization_id: str
    property_id: str
    title: str
    body_md: str
    total_cents: int
    currency: str
    status: str
    sent_at: datetime | None
    decided_at: datetime | None


@dataclass(frozen=True, slots=True)
class QuoteView:
    id: str
    workspace_id: str
    organization_id: str
    property_id: str
    title: str
    body_md: str
    total_cents: int
    currency: str
    status: str
    sent_at: datetime | None
    decided_at: datetime | None


@dataclass(frozen=True, slots=True)
class QuoteCreate:
    organization_id: str
    property_id: str
    title: str
    body_md: str = ""
    total_cents: int = 0
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class QuotePatch:
    fields: Mapping[str, object | None]


@dataclass(frozen=True, slots=True)
class QuoteDecision:
    decision_note_md: str | None = None


class QuoteRepository(Protocol):
    @property
    def session(self) -> Session: ...

    def get_workspace_default_currency(self, *, workspace_id: str) -> str | None: ...

    def organization_contact_email(
        self, *, workspace_id: str, organization_id: str
    ) -> str | None: ...

    def insert(
        self,
        *,
        quote_id: str,
        workspace_id: str,
        organization_id: str,
        property_id: str,
        title: str,
        body_md: str,
        total_cents: int,
        currency: str,
        status: str,
    ) -> QuoteRow: ...

    def get(
        self, *, workspace_id: str, quote_id: str, for_update: bool = False
    ) -> QuoteRow | None: ...

    def get_public(
        self, *, quote_id: str, for_update: bool = False
    ) -> QuoteRow | None: ...

    def list(
        self,
        *,
        workspace_id: str,
        organization_id: str | None,
        property_id: str | None,
        status: str | None,
    ) -> Sequence[QuoteRow]: ...

    def update_fields(
        self,
        *,
        workspace_id: str,
        quote_id: str,
        fields: Mapping[str, object | None],
    ) -> QuoteRow: ...


class QuoteService:
    """Workspace-scoped quote use cases."""

    def __init__(
        self,
        ctx: WorkspaceContext,
        *,
        clock: Clock | None = None,
        signing_key: bytes | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._ctx = ctx
        self._clock = clock if clock is not None else SystemClock()
        self._signing_key = signing_key
        self._bus = event_bus if event_bus is not None else default_bus

    def create(self, repo: QuoteRepository, body: QuoteCreate) -> QuoteView:
        currency = self._currency_or_workspace_default(repo, body.currency)
        row = repo.insert(
            quote_id=new_ulid(),
            workspace_id=self._ctx.workspace_id,
            organization_id=_clean_required(
                body.organization_id, field="organization_id"
            ),
            property_id=_clean_required(body.property_id, field="property_id"),
            title=_clean_required(body.title, field="title"),
            body_md=_clean_optional(body.body_md) or "",
            total_cents=_clean_total(body.total_cents),
            currency=currency,
            status="draft",
        )
        view = _to_view(row)
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="quote",
            entity_id=view.id,
            action="billing.quote.created",
            diff={"after": _audit_shape(view)},
            clock=self._clock,
        )
        return view

    def list(
        self,
        repo: QuoteRepository,
        *,
        organization_id: str | None = None,
        property_id: str | None = None,
        status: str | None = None,
    ) -> list[QuoteView]:
        clean_status = _validate_status(status) if status is not None else None
        rows = repo.list(
            workspace_id=self._ctx.workspace_id,
            organization_id=_clean_optional(organization_id),
            property_id=_clean_optional(property_id),
            status=clean_status,
        )
        return [_to_view(row) for row in rows]

    def get(self, repo: QuoteRepository, quote_id: str) -> QuoteView:
        row = self._get(repo, quote_id)
        return _to_view(row)

    def update(
        self, repo: QuoteRepository, quote_id: str, patch: QuotePatch
    ) -> QuoteView:
        if not patch.fields:
            raise QuoteInvalid("PATCH body must include at least one field")
        unknown = sorted(set(patch.fields) - _MUTABLE_FIELDS)
        if unknown:
            raise QuoteInvalid(f"unknown quote fields: {', '.join(unknown)}")
        current = self._get(repo, quote_id, for_update=True)
        if current.status != "draft":
            raise QuoteInvalid("sent quotes are locked; supersede instead")
        fields = self._normalize_patch(repo, patch)
        changed = {
            key: value
            for key, value in fields.items()
            if getattr(current, key) != value
        }
        if not changed:
            return _to_view(current)
        updated = repo.update_fields(
            workspace_id=self._ctx.workspace_id,
            quote_id=quote_id,
            fields=changed,
        )
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="quote",
            entity_id=updated.id,
            action="billing.quote.updated",
            diff={
                "changed": sorted(changed),
                "before": _audit_shape(_to_view(current)),
                "after": _audit_shape(_to_view(updated)),
            },
            clock=self._clock,
        )
        return _to_view(updated)

    def send(
        self,
        repo: QuoteRepository,
        quote_id: str,
        *,
        mailer: Mailer,
        base_url: str,
    ) -> QuoteView:
        current = self._get(repo, quote_id, for_update=True)
        if current.status not in {"draft", "sent"}:
            raise QuoteInvalid("only draft or sent quotes can be sent")
        recipient = repo.organization_contact_email(
            workspace_id=self._ctx.workspace_id,
            organization_id=current.organization_id,
        )
        if recipient is None:
            raise QuoteInvalid("quote organization has no contact_email")
        now = self._clock.now()
        token = self.sign_token(current, expires_at=now + _TOKEN_TTL)
        url = f"{base_url.rstrip('/')}/q/{current.id}?token={token}"
        message = _render_send_message(current, url=url)
        updated = repo.update_fields(
            workspace_id=self._ctx.workspace_id,
            quote_id=current.id,
            fields={"status": "sent", "sent_at": now},
        )
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="quote",
            entity_id=updated.id,
            action="billing.quote.sent",
            diff={"recipient": recipient, "status": updated.status},
            clock=self._clock,
        )
        repo.session.flush()
        mailer.send(
            to=[recipient],
            subject=message["subject"],
            body_text=message["text"],
            body_html=message["html"],
            headers={"X-Crewday-Quote-ID": current.id},
        )
        return _to_view(updated)

    def accept(self, repo: QuoteRepository, quote_id: str) -> QuoteView:
        return self._decide(repo, quote_id, status="accepted", note=None)

    def reject(
        self,
        repo: QuoteRepository,
        quote_id: str,
        decision: QuoteDecision | None = None,
    ) -> QuoteView:
        note = decision.decision_note_md if decision is not None else None
        return self._decide(repo, quote_id, status="rejected", note=note)

    def supersede(
        self, repo: QuoteRepository, quote_id: str, patch: QuotePatch | None = None
    ) -> QuoteView:
        current = self._get(repo, quote_id, for_update=True)
        fields = self._normalize_patch(repo, patch or QuotePatch(fields={}))
        total_cents = fields.get("total_cents", current.total_cents)
        if not isinstance(total_cents, int):
            raise QuoteInvalid("total_cents must be an integer")
        clone = repo.insert(
            quote_id=new_ulid(),
            workspace_id=self._ctx.workspace_id,
            organization_id=str(fields.get("organization_id", current.organization_id)),
            property_id=str(fields.get("property_id", current.property_id)),
            title=str(fields.get("title", current.title)),
            body_md=str(fields.get("body_md", current.body_md)),
            total_cents=total_cents,
            currency=str(fields.get("currency", current.currency)),
            status="draft",
        )
        repo.update_fields(
            workspace_id=self._ctx.workspace_id,
            quote_id=current.id,
            fields={"status": "expired"},
        )
        view = _to_view(clone)
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="quote",
            entity_id=current.id,
            action="billing.quote.superseded",
            diff={
                "previous_status": current.status,
                "replacement_status": "expired",
                "superseded_by_quote_id": view.id,
            },
            clock=self._clock,
        )
        return view

    def sign_token(self, row: QuoteRow, *, expires_at: datetime) -> str:
        payload = {
            "v": _TOKEN_VERSION,
            "qid": row.id,
            "wid": row.workspace_id,
            "exp": int(expires_at.timestamp()),
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        body = _b64(raw)
        sig = _b64(hmac.new(self._key(), body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def verify_token(self, token: str, *, quote_id: str) -> dict[str, object]:
        try:
            body, sig = token.split(".", maxsplit=1)
        except ValueError as exc:
            raise QuoteTokenInvalid("invalid quote token") from exc
        expected = _b64(hmac.new(self._key(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise QuoteTokenInvalid("invalid quote token")
        try:
            payload = json.loads(_unb64(body))
        except (ValueError, json.JSONDecodeError) as exc:
            raise QuoteTokenInvalid("invalid quote token") from exc
        if not isinstance(payload, dict):
            raise QuoteTokenInvalid("invalid quote token")
        if payload.get("v") != _TOKEN_VERSION or payload.get("qid") != quote_id:
            raise QuoteTokenInvalid("invalid quote token")
        exp = payload.get("exp")
        if not isinstance(exp, int):
            raise QuoteTokenInvalid("invalid quote token")
        if exp < int(self._clock.now().timestamp()):
            raise QuoteTokenInvalid("quote token expired")
        return payload

    def public_accept(
        self, repo: QuoteRepository, *, quote_id: str, token: str
    ) -> QuoteView:
        return self._public_decide(
            repo, quote_id=quote_id, token=token, status="accepted"
        )

    def public_get(
        self, repo: QuoteRepository, *, quote_id: str, token: str
    ) -> QuoteView:
        payload = self.verify_token(token, quote_id=quote_id)
        row = repo.get_public(quote_id=quote_id)
        if row is None:
            raise QuoteNotFound("quote not found")
        if payload.get("wid") != row.workspace_id:
            raise QuoteTokenInvalid("invalid quote token")
        return _to_view(row)

    def public_reject(
        self,
        repo: QuoteRepository,
        *,
        quote_id: str,
        token: str,
        decision: QuoteDecision | None = None,
    ) -> QuoteView:
        note = decision.decision_note_md if decision is not None else None
        return self._public_decide(
            repo, quote_id=quote_id, token=token, status="rejected", note=note
        )

    def _public_decide(
        self,
        repo: QuoteRepository,
        *,
        quote_id: str,
        token: str,
        status: Literal["accepted", "rejected"],
        note: str | None = None,
    ) -> QuoteView:
        payload = self.verify_token(token, quote_id=quote_id)
        row = repo.get_public(quote_id=quote_id, for_update=True)
        if row is None:
            raise QuoteNotFound("quote not found")
        if payload.get("wid") != row.workspace_id:
            raise QuoteTokenInvalid("invalid quote token")
        guest_ctx = WorkspaceContext(
            workspace_id=row.workspace_id,
            workspace_slug="",
            actor_id=f"quote-token:{quote_id}",
            actor_kind="system",
            actor_grant_role="guest",
            actor_was_owner_member=False,
            audit_correlation_id=new_ulid(),
            principal_kind="system",
        )
        return self._decide_row(
            repo,
            row,
            status=status,
            note=note,
            audit_ctx=guest_ctx,
            actor_hint="guest_token",
        )

    def _decide(
        self,
        repo: QuoteRepository,
        quote_id: str,
        *,
        status: Literal["accepted", "rejected"],
        note: str | None,
    ) -> QuoteView:
        row = self._get(repo, quote_id, for_update=True)
        return self._decide_row(
            repo, row, status=status, note=note, audit_ctx=self._ctx
        )

    def _decide_row(
        self,
        repo: QuoteRepository,
        row: QuoteRow,
        *,
        status: Literal["accepted", "rejected"],
        note: str | None,
        audit_ctx: WorkspaceContext,
        actor_hint: str | None = None,
    ) -> QuoteView:
        if row.status == status:
            return _to_view(row)
        if row.status not in {"sent", "accepted", "rejected"}:
            raise QuoteInvalid("only sent quotes can be accepted or rejected")
        if row.status in {"accepted", "rejected"}:
            raise QuoteInvalid("quote has already been decided")
        decided_at = self._clock.now()
        updated = repo.update_fields(
            workspace_id=row.workspace_id,
            quote_id=row.id,
            fields={"status": status, "decided_at": decided_at},
        )
        diff: dict[str, object] = {"status": status}
        if note is not None:
            diff["decision_note_md"] = note
        if actor_hint is not None:
            diff["actor_kind"] = actor_hint
        write_audit(
            repo.session,
            audit_ctx,
            entity_kind="quote",
            entity_id=row.id,
            action=f"billing.quote.{status}",
            diff=diff,
            clock=self._clock,
        )
        self._bus.publish(
            QuoteDecided(
                workspace_id=row.workspace_id,
                actor_id=audit_ctx.actor_id,
                correlation_id=audit_ctx.audit_correlation_id,
                occurred_at=decided_at,
                quote_id=row.id,
                organization_id=row.organization_id,
                property_id=row.property_id,
                decision=status,
                decided_at=decided_at,
            )
        )
        return _to_view(updated)

    def _get(
        self, repo: QuoteRepository, quote_id: str, *, for_update: bool = False
    ) -> QuoteRow:
        row = repo.get(
            workspace_id=self._ctx.workspace_id,
            quote_id=quote_id,
            for_update=for_update,
        )
        if row is None:
            raise QuoteNotFound("quote not found")
        return row

    def _normalize_patch(
        self, repo: QuoteRepository, patch: QuotePatch
    ) -> dict[str, object]:
        unknown = sorted(set(patch.fields) - _MUTABLE_FIELDS)
        if unknown:
            raise QuoteInvalid(f"unknown quote fields: {', '.join(unknown)}")
        fields: dict[str, object] = {}
        for key, value in patch.fields.items():
            if key in {"organization_id", "property_id", "title"}:
                if not isinstance(value, str):
                    raise QuoteInvalid(f"{key} must be a string")
                fields[key] = _clean_required(value, field=key)
            elif key == "body_md":
                if value is not None and not isinstance(value, str):
                    raise QuoteInvalid("body_md must be a string or null")
                fields[key] = _clean_optional(value) or ""
            elif key == "total_cents":
                if not isinstance(value, int):
                    raise QuoteInvalid("total_cents must be an integer")
                fields[key] = _clean_total(value)
            elif key == "currency":
                if not isinstance(value, str):
                    raise QuoteInvalid("currency must be a string")
                fields[key] = _validate_currency(value)
        return fields

    def _currency_or_workspace_default(
        self, repo: QuoteRepository, currency: str | None
    ) -> str:
        value = currency
        if value is None:
            value = repo.get_workspace_default_currency(
                workspace_id=self._ctx.workspace_id
            )
        if value is None:
            raise QuoteInvalid("workspace default currency is not configured")
        return _validate_currency(value)

    def _key(self) -> bytes:
        if self._signing_key is None:
            raise QuoteInvalid("quote signing key is not configured")
        return self._signing_key


def _render_send_message(row: QuoteRow, *, url: str) -> dict[str, str]:
    amount = f"{row.currency} {row.total_cents / 100:.2f}"
    subject = Template("crew.day quote: {{ title }}").render(title=row.title)
    text = Template(
        """\
You have a quote to review.

{{ title }}
Total: {{ amount }}

{{ body }}

Open the quote:
{{ url }}
"""
    ).render(title=row.title, amount=amount, body=row.body_md, url=url)
    html = Template(
        """\
<!doctype html>
<html><body>
<p>You have a quote to review.</p>
<h1>{{ title }}</h1>
<p><strong>Total:</strong> {{ amount }}</p>
<p>{{ body }}</p>
<p><a href="{{ url }}">Open the quote</a></p>
</body></html>
"""
    ).render(
        title=escape(row.title),
        amount=escape(amount),
        body=escape(row.body_md).replace("\n", "<br>"),
        url=escape(url, quote=True),
    )
    return {"subject": subject, "text": text, "html": html}


def _validate_status(value: str) -> str:
    if value not in {"draft", "sent", "accepted", "rejected", "expired"}:
        raise QuoteInvalid(
            "status must be one of draft, sent, accepted, rejected, expired"
        )
    return value


def _validate_currency(value: str) -> str:
    currency = normalise_currency(value)
    if not is_valid_currency(currency):
        raise QuoteInvalid(f"currency {value!r} is not a valid ISO-4217 code")
    return currency


def _clean_required(value: str, *, field: str) -> str:
    clean = value.strip()
    if not clean:
        raise QuoteInvalid(f"{field} is required")
    return clean


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    clean = value.strip()
    return clean or None


def _clean_total(value: int) -> int:
    if value < 0:
        raise QuoteInvalid("total_cents must be non-negative")
    return value


def _to_view(row: QuoteRow) -> QuoteView:
    return QuoteView(
        id=row.id,
        workspace_id=row.workspace_id,
        organization_id=row.organization_id,
        property_id=row.property_id,
        title=row.title,
        body_md=row.body_md,
        total_cents=row.total_cents,
        currency=row.currency,
        status=row.status,
        sent_at=row.sent_at,
        decided_at=row.decided_at,
    )


def _audit_shape(view: QuoteView) -> dict[str, object]:
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "organization_id": view.organization_id,
        "property_id": view.property_id,
        "title": view.title,
        "total_cents": view.total_cents,
        "currency": view.currency,
        "status": view.status,
        "sent_at": view.sent_at.isoformat() if view.sent_at is not None else None,
        "decided_at": (
            view.decided_at.isoformat() if view.decided_at is not None else None
        ),
    }


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
