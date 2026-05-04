"""Billing quote HTTP routes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.adapters.db.billing.repositories import SqlAlchemyQuoteRepository
from app.adapters.mail.ports import Mailer
from app.api.deps import current_workspace_context, db_session
from app.auth.keys import KeyDerivationError, derive_subkey
from app.authz.dep import Permission
from app.config import Settings, get_settings
from app.domain.billing.quotes import (
    QuoteCreate,
    QuoteDecision,
    QuoteInvalid,
    QuoteNotFound,
    QuotePatch,
    QuoteService,
    QuoteTokenInvalid,
    QuoteView,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "QuoteCreateRequest",
    "QuoteDecisionActionRequest",
    "QuoteDecisionRequest",
    "QuoteListResponse",
    "QuotePatchRequest",
    "QuoteResponse",
    "build_quotes_public_router",
    "build_quotes_router",
]

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_Settings = Annotated[Settings, Depends(get_settings)]


class QuoteLinePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    quantity: int | float | str
    unit: str = Field(min_length=1, max_length=64)
    unit_price_cents: int = Field(ge=0)
    total_cents: int = Field(ge=0)


class QuoteLinesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    lines: list[QuoteLinePayload] = Field(min_length=1)


class QuoteResponse(BaseModel):
    id: str
    workspace_id: str
    organization_id: str
    property_id: str
    title: str
    body_md: str
    lines_json: QuoteLinesPayload
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    currency: str
    status: str
    superseded_by_quote_id: str | None
    sent_at: datetime | None
    decided_at: datetime | None

    @classmethod
    def from_view(cls, view: QuoteView) -> QuoteResponse:
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            organization_id=view.organization_id,
            property_id=view.property_id,
            title=view.title,
            body_md=view.body_md,
            lines_json=QuoteLinesPayload.model_validate(view.lines_json),
            subtotal_cents=view.subtotal_cents,
            tax_cents=view.tax_cents,
            total_cents=view.total_cents,
            currency=view.currency,
            status=view.status,
            superseded_by_quote_id=view.superseded_by_quote_id,
            sent_at=view.sent_at,
            decided_at=view.decided_at,
        )


class QuoteListResponse(BaseModel):
    data: list[QuoteResponse]


class QuoteCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: str = Field(min_length=1, max_length=64)
    property_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    body_md: str = ""
    lines_json: QuoteLinesPayload | None = None
    subtotal_cents: int | None = Field(default=None, ge=0)
    tax_cents: int = Field(default=0, ge=0)
    total_cents: int = Field(ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    def to_domain(self) -> QuoteCreate:
        return QuoteCreate(
            organization_id=self.organization_id,
            property_id=self.property_id,
            title=self.title,
            body_md=self.body_md,
            lines_json=(
                self.lines_json.model_dump(mode="json")
                if self.lines_json is not None
                else None
            ),
            subtotal_cents=self.subtotal_cents,
            tax_cents=self.tax_cents,
            total_cents=self.total_cents,
            currency=self.currency,
        )


class QuotePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: str | None = Field(default=None, min_length=1, max_length=64)
    property_id: str | None = Field(default=None, min_length=1, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body_md: str | None = None
    lines_json: QuoteLinesPayload | None = None
    subtotal_cents: int | None = Field(default=None, ge=0)
    tax_cents: int | None = Field(default=None, ge=0)
    total_cents: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    @model_validator(mode="after")
    def _has_mutation(self) -> QuotePatchRequest:
        if not self.model_fields_set:
            raise ValueError("PATCH body must include at least one field")
        return self

    def to_domain(self) -> QuotePatch:
        fields: dict[str, object | None] = {}
        for field in self.model_fields_set:
            value = getattr(self, field)
            if isinstance(value, QuoteLinesPayload):
                fields[field] = value.model_dump(mode="json")
            else:
                fields[field] = value
        return QuotePatch(fields=fields)


class QuoteDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_note_md: str | None = None

    def to_domain(self) -> QuoteDecision:
        return QuoteDecision(decision_note_md=self.decision_note_md)


class QuoteDecisionActionRequest(QuoteDecisionRequest):
    decision: Literal["accepted", "rejected"]


class QuoteSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None


def _http_for_quote_error(exc: Exception) -> HTTPException:
    if isinstance(exc, QuoteNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "quote_not_found", "message": str(exc)},
        )
    if isinstance(exc, QuoteTokenInvalid):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "quote_token_invalid", "message": str(exc)},
        )
    if isinstance(exc, QuoteInvalid):
        return HTTPException(
            status_code=422,
            detail={"error": "quote_invalid", "message": str(exc)},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


def _signing_key(settings: Settings) -> bytes:
    try:
        return derive_subkey(settings.root_key, purpose="billing-quote-token")
    except KeyDerivationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "quote_signing_unavailable"},
        ) from exc


def _mailer(request: Request) -> Mailer:
    mailer: Mailer | None = getattr(request.app.state, "mailer", None)
    if mailer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "mailer_unavailable"},
        )
    return mailer


def build_quotes_router() -> APIRouter:
    router = APIRouter(prefix="/quotes", tags=["billing", "quotes"])

    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))
    submit_gate = Depends(Permission("quotes.submit", scope_kind="workspace"))
    accept_gate = Depends(Permission("quotes.accept", scope_kind="workspace"))

    @router.get(
        "",
        response_model=QuoteListResponse,
        operation_id="billing.quotes.list",
        dependencies=[view_gate],
    )
    def list_quotes(
        ctx: _Ctx,
        session: _Db,
        organization_id: str | None = None,
        property_id: str | None = None,
        status: Literal["draft", "sent", "accepted", "rejected", "expired"]
        | None = None,
    ) -> QuoteListResponse:
        views = QuoteService(ctx).list(
            SqlAlchemyQuoteRepository(session),
            organization_id=organization_id,
            property_id=property_id,
            status=status,
        )
        return QuoteListResponse(data=[QuoteResponse.from_view(view) for view in views])

    @router.post(
        "",
        response_model=QuoteResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="billing.quotes.create",
        dependencies=[submit_gate],
    )
    def create_quote(
        body: QuoteCreateRequest, ctx: _Ctx, session: _Db
    ) -> QuoteResponse:
        try:
            view = QuoteService(ctx).create(
                SqlAlchemyQuoteRepository(session), body.to_domain()
            )
        except QuoteInvalid as exc:
            raise _http_for_quote_error(exc) from exc
        return QuoteResponse.from_view(view)

    @router.get(
        "/{quote_id}",
        response_model=QuoteResponse,
        operation_id="billing.quotes.get",
        dependencies=[view_gate],
    )
    def get_quote(quote_id: str, ctx: _Ctx, session: _Db) -> QuoteResponse:
        try:
            view = QuoteService(ctx).get(SqlAlchemyQuoteRepository(session), quote_id)
        except QuoteNotFound as exc:
            raise _http_for_quote_error(exc) from exc
        return QuoteResponse.from_view(view)

    @router.patch(
        "/{quote_id}",
        response_model=QuoteResponse,
        operation_id="billing.quotes.update",
        dependencies=[submit_gate],
    )
    def patch_quote(
        quote_id: str,
        body: QuotePatchRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> QuoteResponse:
        try:
            view = QuoteService(ctx).update(
                SqlAlchemyQuoteRepository(session), quote_id, body.to_domain()
            )
        except (QuoteInvalid, QuoteNotFound) as exc:
            raise _http_for_quote_error(exc) from exc
        return QuoteResponse.from_view(view)

    @router.post(
        "/{quote_id}/send",
        response_model=QuoteResponse,
        operation_id="billing.quotes.send",
        dependencies=[submit_gate],
    )
    def send_quote(
        quote_id: str,
        body: QuoteSendRequest,
        ctx: _Ctx,
        session: _Db,
        settings: _Settings,
        request: Request,
    ) -> QuoteResponse:
        base_url = body.base_url if body.base_url is not None else settings.public_url
        if base_url is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "public_url_unavailable"},
            )
        try:
            view = QuoteService(ctx, signing_key=_signing_key(settings)).send(
                SqlAlchemyQuoteRepository(session),
                quote_id,
                mailer=_mailer(request),
                base_url=base_url,
            )
        except (QuoteInvalid, QuoteNotFound) as exc:
            raise _http_for_quote_error(exc) from exc
        return QuoteResponse.from_view(view)

    @router.post(
        "/{quote_id}/accept",
        response_model=QuoteResponse,
        operation_id="billing.quotes.accept",
        dependencies=[accept_gate],
    )
    def accept_quote(quote_id: str, ctx: _Ctx, session: _Db) -> QuoteResponse:
        try:
            view = QuoteService(ctx).accept(
                SqlAlchemyQuoteRepository(session), quote_id
            )
        except (QuoteInvalid, QuoteNotFound) as exc:
            raise _http_for_quote_error(exc) from exc
        return QuoteResponse.from_view(view)

    @router.post(
        "/{quote_id}/reject",
        response_model=QuoteResponse,
        operation_id="billing.quotes.reject",
        dependencies=[accept_gate],
    )
    def reject_quote(
        quote_id: str,
        ctx: _Ctx,
        session: _Db,
        body: QuoteDecisionRequest | None = None,
    ) -> QuoteResponse:
        try:
            view = QuoteService(ctx).reject(
                SqlAlchemyQuoteRepository(session),
                quote_id,
                body.to_domain() if body is not None else None,
            )
        except (QuoteInvalid, QuoteNotFound) as exc:
            raise _http_for_quote_error(exc) from exc
        return QuoteResponse.from_view(view)

    @router.post(
        "/{quote_id}/decision",
        response_model=QuoteResponse,
        operation_id="billing.quotes.decision",
        dependencies=[accept_gate],
    )
    def decide_quote(
        quote_id: str,
        body: QuoteDecisionActionRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> QuoteResponse:
        try:
            service = QuoteService(ctx)
            repo = SqlAlchemyQuoteRepository(session)
            if body.decision == "accepted":
                view = service.accept(repo, quote_id)
            else:
                view = service.reject(repo, quote_id, body.to_domain())
        except (QuoteInvalid, QuoteNotFound) as exc:
            raise _http_for_quote_error(exc) from exc
        return QuoteResponse.from_view(view)

    @router.post(
        "/{quote_id}/supersede",
        response_model=QuoteResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="billing.quotes.supersede",
        dependencies=[submit_gate],
    )
    def supersede_quote(
        quote_id: str,
        ctx: _Ctx,
        session: _Db,
        body: QuotePatchRequest | None = None,
    ) -> QuoteResponse:
        try:
            view = QuoteService(ctx).supersede(
                SqlAlchemyQuoteRepository(session),
                quote_id,
                body.to_domain() if body is not None else None,
            )
        except (QuoteInvalid, QuoteNotFound) as exc:
            raise _http_for_quote_error(exc) from exc
        return QuoteResponse.from_view(view)

    return router


def build_quotes_public_router() -> APIRouter:
    router = APIRouter(prefix="/q", tags=["billing", "quotes"])

    @router.get(
        "/{quote_id}",
        response_model=QuoteResponse,
        operation_id="billing.quotes.public_get",
    )
    def public_get_quote(
        quote_id: str,
        token: str,
        session: _Db,
        settings: _Settings,
    ) -> QuoteResponse:
        try:
            view = QuoteService(
                _public_service_ctx(), signing_key=_signing_key(settings)
            ).public_get(
                SqlAlchemyQuoteRepository(session), quote_id=quote_id, token=token
            )
        except (QuoteInvalid, QuoteNotFound, QuoteTokenInvalid) as exc:
            raise _http_for_quote_error(exc) from exc
        return QuoteResponse.from_view(view)

    @router.post(
        "/{quote_id}/accept",
        response_model=QuoteResponse,
        operation_id="billing.quotes.public_accept",
    )
    def public_accept_quote(
        quote_id: str,
        token: str,
        session: _Db,
        settings: _Settings,
    ) -> QuoteResponse:
        try:
            view = QuoteService(
                _public_service_ctx(), signing_key=_signing_key(settings)
            ).public_accept(
                SqlAlchemyQuoteRepository(session), quote_id=quote_id, token=token
            )
        except (QuoteInvalid, QuoteNotFound, QuoteTokenInvalid) as exc:
            raise _http_for_quote_error(exc) from exc
        return QuoteResponse.from_view(view)

    @router.post(
        "/{quote_id}/reject",
        response_model=QuoteResponse,
        operation_id="billing.quotes.public_reject",
    )
    def public_reject_quote(
        quote_id: str,
        token: str,
        session: _Db,
        settings: _Settings,
        body: QuoteDecisionRequest | None = None,
    ) -> QuoteResponse:
        try:
            view = QuoteService(
                _public_service_ctx(), signing_key=_signing_key(settings)
            ).public_reject(
                SqlAlchemyQuoteRepository(session),
                quote_id=quote_id,
                token=token,
                decision=body.to_domain() if body is not None else None,
            )
        except (QuoteInvalid, QuoteNotFound, QuoteTokenInvalid) as exc:
            raise _http_for_quote_error(exc) from exc
        return QuoteResponse.from_view(view)

    return router


def _public_service_ctx() -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id="00000000000000000000PUBLIC",
        workspace_slug="",
        actor_id="public",
        actor_kind="system",
        actor_grant_role="guest",
        actor_was_owner_member=False,
        audit_correlation_id="00000000000000000000PUBLIC",
        principal_kind="system",
    )
