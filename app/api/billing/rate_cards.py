"""Billing rate-card HTTP routes."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy.orm import Session

from app.adapters.db.billing.repositories import SqlAlchemyRateCardRepository
from app.api.deps import current_workspace_context, db_session
from app.authz.dep import Permission
from app.domain.billing.rate_cards import (
    RateCardCreate,
    RateCardInvalid,
    RateCardNotFound,
    RateCardPatch,
    RateCardService,
    RateCardView,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "RateCardCreateRequest",
    "RateCardListResponse",
    "RateCardPatchRequest",
    "RateCardResponse",
    "build_rate_cards_router",
]

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_RateCents = StrictInt


class RateCardResponse(BaseModel):
    id: str
    workspace_id: str
    organization_id: str
    label: str
    currency: str
    rates: dict[str, int]
    active_from: date
    active_to: date | None

    @classmethod
    def from_view(cls, view: RateCardView) -> RateCardResponse:
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            organization_id=view.organization_id,
            label=view.label,
            currency=view.currency,
            rates=dict(view.rates),
            active_from=view.active_from,
            active_to=view.active_to,
        )


class RateCardListResponse(BaseModel):
    data: list[RateCardResponse]


class RateCardCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=200)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    rates: dict[str, _RateCents] = Field(min_length=1)
    active_from: date
    active_to: date | None = None

    def to_domain(self) -> RateCardCreate:
        return RateCardCreate(
            label=self.label,
            currency=self.currency,
            rates=self.rates,
            active_from=self.active_from,
            active_to=self.active_to,
        )


class RateCardPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=200)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    rates: dict[str, _RateCents] | None = Field(default=None, min_length=1)
    active_from: date | None = None
    active_to: date | None = None

    @model_validator(mode="after")
    def _has_mutation(self) -> RateCardPatchRequest:
        if not self.model_fields_set:
            raise ValueError("PATCH body must include at least one field")
        return self

    def to_domain(self) -> RateCardPatch:
        fields: dict[str, object | None] = {}
        for field in self.model_fields_set:
            fields[field] = getattr(self, field)
        return RateCardPatch(fields=fields)


def _http_for_rate_card_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RateCardNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "rate_card_not_found", "message": str(exc)},
        )
    if isinstance(exc, RateCardInvalid):
        return HTTPException(
            status_code=422,
            detail={"error": "rate_card_invalid", "message": str(exc)},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


def build_rate_cards_router() -> APIRouter:
    router = APIRouter(
        prefix="/organizations/{organization_id}/rate-cards",
        tags=["billing", "rate-cards"],
    )

    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))
    edit_gate = Depends(Permission("organizations.edit", scope_kind="workspace"))

    @router.get(
        "",
        response_model=RateCardListResponse,
        operation_id="billing.rate_cards.list",
        dependencies=[view_gate],
        summary="List billing rate cards for an organization",
    )
    def list_rate_cards(
        organization_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> RateCardListResponse:
        try:
            views = RateCardService(ctx).list(
                SqlAlchemyRateCardRepository(session),
                organization_id,
            )
        except (RateCardInvalid, RateCardNotFound) as exc:
            raise _http_for_rate_card_error(exc) from exc
        return RateCardListResponse(
            data=[RateCardResponse.from_view(view) for view in views]
        )

    @router.post(
        "",
        response_model=RateCardResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="billing.rate_cards.create",
        dependencies=[edit_gate],
        summary="Create a billing rate card",
    )
    def create_rate_card(
        organization_id: str,
        body: RateCardCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> RateCardResponse:
        try:
            view = RateCardService(ctx).create(
                SqlAlchemyRateCardRepository(session),
                organization_id,
                body.to_domain(),
            )
        except (RateCardInvalid, RateCardNotFound) as exc:
            raise _http_for_rate_card_error(exc) from exc
        return RateCardResponse.from_view(view)

    @router.get(
        "/{rate_card_id}",
        response_model=RateCardResponse,
        operation_id="billing.rate_cards.get",
        dependencies=[view_gate],
        summary="Get a billing rate card",
    )
    def get_rate_card(
        organization_id: str,
        rate_card_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> RateCardResponse:
        try:
            view = RateCardService(ctx).get(
                SqlAlchemyRateCardRepository(session),
                organization_id,
                rate_card_id,
            )
        except (RateCardInvalid, RateCardNotFound) as exc:
            raise _http_for_rate_card_error(exc) from exc
        return RateCardResponse.from_view(view)

    @router.patch(
        "/{rate_card_id}",
        response_model=RateCardResponse,
        operation_id="billing.rate_cards.update",
        dependencies=[edit_gate],
        summary="Update a billing rate card",
    )
    def patch_rate_card(
        organization_id: str,
        rate_card_id: str,
        body: RateCardPatchRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> RateCardResponse:
        try:
            view = RateCardService(ctx).update(
                SqlAlchemyRateCardRepository(session),
                organization_id,
                rate_card_id,
                body.to_domain(),
            )
        except (RateCardInvalid, RateCardNotFound) as exc:
            raise _http_for_rate_card_error(exc) from exc
        return RateCardResponse.from_view(view)

    return router
