"""Billing organization CRUD service."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy.orm import Session

from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.currency import is_valid_currency, normalise_currency
from app.util.ulid import new_ulid

__all__ = [
    "ORGANIZATION_KINDS",
    "OrganizationArtifactCounts",
    "OrganizationCreate",
    "OrganizationInvalid",
    "OrganizationNotFound",
    "OrganizationPatch",
    "OrganizationRepository",
    "OrganizationRow",
    "OrganizationService",
    "OrganizationView",
]


OrganizationKind = Literal["client", "vendor", "mixed"]
ORGANIZATION_KINDS: frozenset[str] = frozenset({"client", "vendor", "mixed"})
_MUTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "kind",
        "display_name",
        "billing_address",
        "tax_id",
        "default_currency",
        "contact_email",
        "contact_phone",
        "notes_md",
    }
)


class OrganizationInvalid(ValueError):
    """The requested organization mutation violates the billing contract."""


class OrganizationNotFound(LookupError):
    """The organization does not exist in the caller's workspace."""


@dataclass(frozen=True, slots=True)
class OrganizationArtifactCounts:
    rate_cards: int = 0
    work_orders: int = 0
    quotes: int = 0
    vendor_invoices: int = 0

    @property
    def client_artifacts(self) -> int:
        return self.rate_cards + self.work_orders + self.quotes


@dataclass(frozen=True, slots=True)
class OrganizationRow:
    id: str
    workspace_id: str
    kind: str
    display_name: str
    billing_address: Mapping[str, object]
    tax_id: str | None
    default_currency: str
    contact_email: str | None
    contact_phone: str | None
    notes_md: str | None
    created_at: datetime
    archived_at: datetime | None


@dataclass(frozen=True, slots=True)
class OrganizationView:
    id: str
    workspace_id: str
    kind: str
    display_name: str
    billing_address: Mapping[str, object]
    tax_id: str | None
    default_currency: str
    contact_email: str | None
    contact_phone: str | None
    notes_md: str | None
    created_at: datetime
    archived_at: datetime | None


@dataclass(frozen=True, slots=True)
class OrganizationCreate:
    kind: str
    display_name: str
    billing_address: Mapping[str, object] | None = None
    tax_id: str | None = None
    default_currency: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    notes_md: str | None = None


@dataclass(frozen=True, slots=True)
class OrganizationPatch:
    fields: Mapping[str, object | None]


class OrganizationRepository(Protocol):
    @property
    def session(self) -> Session: ...

    def get_workspace_default_currency(self, *, workspace_id: str) -> str | None: ...

    def insert(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        kind: str,
        display_name: str,
        billing_address: Mapping[str, object],
        tax_id: str | None,
        default_currency: str,
        contact_email: str | None,
        contact_phone: str | None,
        notes_md: str | None,
        created_at: datetime,
    ) -> OrganizationRow: ...

    def get(
        self,
        *,
        workspace_id: str,
        organization_id: str,
        include_archived: bool,
        for_update: bool = False,
    ) -> OrganizationRow | None: ...

    def get_by_display_name(
        self,
        *,
        workspace_id: str,
        display_name: str,
        exclude_id: str | None = None,
    ) -> OrganizationRow | None: ...

    def list(
        self,
        *,
        workspace_id: str,
        kind: str | None,
        search: str | None,
        include_archived: bool,
    ) -> Sequence[OrganizationRow]: ...

    def artifact_counts(
        self, *, workspace_id: str, organization_id: str
    ) -> OrganizationArtifactCounts: ...

    def update_fields(
        self,
        *,
        workspace_id: str,
        organization_id: str,
        fields: Mapping[str, object | None],
    ) -> OrganizationRow: ...

    def archive(
        self,
        *,
        workspace_id: str,
        organization_id: str,
        archived_at: datetime,
    ) -> OrganizationRow: ...


class OrganizationService:
    """Workspace-scoped billing organization use cases."""

    def __init__(self, ctx: WorkspaceContext, *, clock: Clock | None = None) -> None:
        self._ctx = ctx
        self._clock = clock if clock is not None else SystemClock()

    def create(
        self, repo: OrganizationRepository, body: OrganizationCreate
    ) -> OrganizationView:
        currency = self._currency_or_workspace_default(repo, body.default_currency)
        display_name = _clean_required(body.display_name, field="display_name")
        self._reject_duplicate_display_name(repo, display_name=display_name)
        row = repo.insert(
            organization_id=new_ulid(),
            workspace_id=self._ctx.workspace_id,
            kind=_validate_kind(body.kind),
            display_name=display_name,
            billing_address=_clean_billing_address(body.billing_address),
            tax_id=_clean_optional(body.tax_id),
            default_currency=currency,
            contact_email=_clean_optional(body.contact_email),
            contact_phone=_clean_optional(body.contact_phone),
            notes_md=_clean_optional(body.notes_md),
            created_at=self._clock.now(),
        )
        view = _to_view(row)
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="organization",
            entity_id=view.id,
            action="billing.organization.created",
            diff={"after": _audit_shape(view)},
            clock=self._clock,
        )
        return view

    def get(
        self,
        repo: OrganizationRepository,
        organization_id: str,
        *,
        include_archived: bool = False,
    ) -> OrganizationView:
        row = repo.get(
            workspace_id=self._ctx.workspace_id,
            organization_id=organization_id,
            include_archived=include_archived,
        )
        if row is None:
            raise OrganizationNotFound("organization not found")
        return _to_view(row)

    def list(
        self,
        repo: OrganizationRepository,
        *,
        kind: str | None = None,
        search: str | None = None,
        include_archived: bool = False,
    ) -> list[OrganizationView]:
        clean_kind = _validate_kind(kind) if kind is not None else None
        clean_search = search.strip() if search is not None else None
        if clean_search == "":
            clean_search = None
        rows = repo.list(
            workspace_id=self._ctx.workspace_id,
            kind=clean_kind,
            search=clean_search,
            include_archived=include_archived,
        )
        return [_to_view(row) for row in rows]

    def update(
        self,
        repo: OrganizationRepository,
        organization_id: str,
        patch: OrganizationPatch,
    ) -> OrganizationView:
        if not patch.fields:
            raise OrganizationInvalid("PATCH body must include at least one field")
        unknown = sorted(set(patch.fields) - _MUTABLE_FIELDS)
        if unknown:
            raise OrganizationInvalid(
                f"unknown organization fields: {', '.join(unknown)}"
            )

        current = repo.get(
            workspace_id=self._ctx.workspace_id,
            organization_id=organization_id,
            include_archived=False,
            for_update=True,
        )
        if current is None:
            raise OrganizationNotFound("organization not found")

        fields = self._normalize_patch(repo, current, patch)
        changed = {
            key: value
            for key, value in fields.items()
            if getattr(current, key) != value
        }
        if not changed:
            return _to_view(current)

        updated = repo.update_fields(
            workspace_id=self._ctx.workspace_id,
            organization_id=organization_id,
            fields=changed,
        )
        before = _audit_shape(_to_view(current))
        after = _audit_shape(_to_view(updated))
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="organization",
            entity_id=updated.id,
            action="billing.organization.updated",
            diff={
                "changed": sorted(changed),
                "before": before,
                "after": after,
            },
            clock=self._clock,
        )
        return _to_view(updated)

    def archive(
        self, repo: OrganizationRepository, organization_id: str
    ) -> OrganizationView:
        current = repo.get(
            workspace_id=self._ctx.workspace_id,
            organization_id=organization_id,
            include_archived=True,
            for_update=True,
        )
        if current is None:
            raise OrganizationNotFound("organization not found")
        if current.archived_at is not None:
            return _to_view(current)

        archived = repo.archive(
            workspace_id=self._ctx.workspace_id,
            organization_id=organization_id,
            archived_at=self._clock.now(),
        )
        if archived.archived_at is None:
            raise RuntimeError("organization archive did not set archived_at")
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="organization",
            entity_id=archived.id,
            action="billing.organization.archived",
            diff={"archived_at": archived.archived_at.isoformat()},
            clock=self._clock,
        )
        return _to_view(archived)

    def _currency_or_workspace_default(
        self, repo: OrganizationRepository, currency: str | None
    ) -> str:
        value = currency
        if value is None:
            value = repo.get_workspace_default_currency(
                workspace_id=self._ctx.workspace_id
            )
        if value is None:
            raise OrganizationInvalid("workspace default currency is not configured")
        return _validate_currency(value)

    def _normalize_patch(
        self,
        repo: OrganizationRepository,
        current: OrganizationRow,
        patch: OrganizationPatch,
    ) -> dict[str, object | None]:
        fields: dict[str, object | None] = {}
        for key, value in patch.fields.items():
            if key == "kind":
                if not isinstance(value, str):
                    raise OrganizationInvalid("kind must be a string")
                target = _validate_kind(value)
                self._validate_kind_transition(repo, current, target)
                fields[key] = target
            elif key == "display_name":
                if not isinstance(value, str):
                    raise OrganizationInvalid("display_name must be a string")
                display_name = _clean_required(value, field="display_name")
                if display_name != current.display_name:
                    self._reject_duplicate_display_name(
                        repo,
                        display_name=display_name,
                        exclude_id=current.id,
                    )
                fields[key] = display_name
            elif key == "billing_address":
                if value is None:
                    raise OrganizationInvalid("billing_address cannot be null")
                if not isinstance(value, Mapping):
                    raise OrganizationInvalid("billing_address must be an object")
                fields[key] = _clean_billing_address(value)
            elif key == "default_currency":
                if not isinstance(value, str):
                    raise OrganizationInvalid("default_currency must be a string")
                fields[key] = _validate_currency(value)
            elif key in {"tax_id", "contact_email", "contact_phone", "notes_md"}:
                if value is not None and not isinstance(value, str):
                    raise OrganizationInvalid(f"{key} must be a string or null")
                fields[key] = _clean_optional(value)
        return fields

    def _validate_kind_transition(
        self,
        repo: OrganizationRepository,
        current: OrganizationRow,
        target_kind: str,
    ) -> None:
        if target_kind == current.kind:
            return
        counts = repo.artifact_counts(
            workspace_id=self._ctx.workspace_id,
            organization_id=current.id,
        )
        if target_kind == "vendor" and counts.client_artifacts > 0:
            raise OrganizationInvalid(
                "organization has client-facing artifacts and cannot become vendor"
            )
        if target_kind == "client" and counts.vendor_invoices > 0:
            raise OrganizationInvalid(
                "organization has vendor invoices and cannot become client"
            )

    def _reject_duplicate_display_name(
        self,
        repo: OrganizationRepository,
        *,
        display_name: str,
        exclude_id: str | None = None,
    ) -> None:
        existing = repo.get_by_display_name(
            workspace_id=self._ctx.workspace_id,
            display_name=display_name,
            exclude_id=exclude_id,
        )
        if existing is not None:
            raise OrganizationInvalid(
                f"organization named {display_name!r} already exists"
            )


def _validate_kind(kind: str) -> OrganizationKind:
    match kind:
        case "client":
            return "client"
        case "vendor":
            return "vendor"
        case "mixed":
            return "mixed"
        case _:
            raise OrganizationInvalid("kind must be one of client, vendor, mixed")


def _validate_currency(value: str) -> str:
    currency = normalise_currency(value)
    if not is_valid_currency(currency):
        raise OrganizationInvalid(f"currency {value!r} is not a valid ISO-4217 code")
    return currency


def _clean_required(value: str, *, field: str) -> str:
    clean = value.strip()
    if not clean:
        raise OrganizationInvalid(f"{field} is required")
    return clean


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    clean = value.strip()
    return clean or None


def _clean_billing_address(
    value: Mapping[str, object] | None,
) -> dict[str, object]:
    if value is None:
        return {}
    return dict(value)


def _to_view(row: OrganizationRow) -> OrganizationView:
    return OrganizationView(
        id=row.id,
        workspace_id=row.workspace_id,
        kind=row.kind,
        display_name=row.display_name,
        billing_address=dict(row.billing_address),
        tax_id=row.tax_id,
        default_currency=row.default_currency,
        contact_email=row.contact_email,
        contact_phone=row.contact_phone,
        notes_md=row.notes_md,
        created_at=row.created_at,
        archived_at=row.archived_at,
    )


def _audit_shape(view: OrganizationView) -> dict[str, object]:
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "kind": view.kind,
        "display_name": view.display_name,
        "billing_address": dict(view.billing_address),
        "tax_id": view.tax_id,
        "default_currency": view.default_currency,
        "contact_email": view.contact_email,
        "contact_phone": view.contact_phone,
        "notes_md": view.notes_md,
        "created_at": view.created_at.isoformat(),
        "archived_at": (
            view.archived_at.isoformat() if view.archived_at is not None else None
        ),
    }
