"""Stays context router.

Owns iCal feeds, reservation reads, stay task bundles, and guest
welcome links (spec §04 / §12). Manager routes are workspace-scoped
and gated by ``stays.read`` / ``stays.manage``. The public welcome
read path is intentionally anonymous: the signed guest token is the
credential, and invalid / revoked / expired tokens never render stay
payloads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import Property
from app.adapters.db.secrets.repositories import SqlAlchemySecretEnvelopeRepository
from app.adapters.db.stays.models import Reservation, StayBundle
from app.adapters.db.tasks.repositories import SqlAlchemyTasksCreateOccurrencePort
from app.adapters.db.workspace.models import Workspace
from app.adapters.ical.ports import IcalProvider
from app.adapters.ical.providers import HostProviderDetector
from app.adapters.ical.validator import HttpxIcalValidator
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.adapters.storage.ports import EnvelopeEncryptor
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.authz.dep import Permission
from app.config import Settings, get_settings
from app.domain.stays.bundle_service import (
    BundleGenerationResult,
    generate_bundles_for_stay,
)
from app.domain.stays.guest_link_service import (
    GuestAsset,
    GuestLinkGone,
    GuestLinkGoneReason,
    GuestLinkNotFound,
    SettingsResolver,
    WelcomeMergeInput,
    WelcomeResolver,
    mint_link,
    record_access,
    resolve_link,
    revoke_link,
)
from app.domain.stays.ical_service import (
    IcalFeedCreate,
    IcalFeedNotFound,
    IcalFeedUpdate,
    IcalFeedView,
    IcalProbeResult,
    IcalUrlInvalid,
    delete_feed,
    disable_feed,
    list_feeds,
    probe_feed,
    register_feed,
    update_feed,
)
from app.ports.tasks_create_occurrence import TasksCreateOccurrencePort
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock

router: APIRouter
public_router: APIRouter

__all__ = [
    "GuestLinkIssueRequest",
    "GuestLinkIssueResponse",
    "IcalFeedCreateRequest",
    "IcalFeedResponse",
    "IcalFeedUpdateRequest",
    "ReservationListResponse",
    "ReservationResponse",
    "StayBundleListResponse",
    "StayBundleResponse",
    "WelcomeResponse",
    "build_stays_public_router",
    "build_stays_router",
    "public_router",
    "router",
]

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_ID = Annotated[str, Path(min_length=1, max_length=64)]


# ---------------------------------------------------------------------------
# Adapter dependencies
# ---------------------------------------------------------------------------


def get_ical_validator() -> HttpxIcalValidator:
    return HttpxIcalValidator()


def get_provider_detector() -> HostProviderDetector:
    return HostProviderDetector()


def get_envelope(
    session: _Db,
    settings: Annotated[Settings, Depends(get_settings)],
) -> EnvelopeEncryptor:
    if settings.root_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "envelope_unavailable"},
        )
    return Aes256GcmEnvelope(
        settings.root_key,
        repository=SqlAlchemySecretEnvelopeRepository(session),
    )


def get_tasks_create_occurrence_port() -> TasksCreateOccurrencePort:
    return SqlAlchemyTasksCreateOccurrencePort()


def get_welcome_resolver() -> WelcomeResolver:
    return SqlAlchemyWelcomeResolver()


def get_guest_settings_resolver() -> SettingsResolver:
    return SqlAlchemyGuestSettingsResolver()


def get_clock() -> Clock:
    return SystemClock()


def get_app_settings() -> Settings:
    return get_settings()


_IcalValidatorDep = Annotated[HttpxIcalValidator, Depends(get_ical_validator)]
_ProviderDetectorDep = Annotated[HostProviderDetector, Depends(get_provider_detector)]
_EnvelopeDep = Annotated[EnvelopeEncryptor, Depends(get_envelope)]
_TasksPortDep = Annotated[
    TasksCreateOccurrencePort, Depends(get_tasks_create_occurrence_port)
]
_WelcomeResolverDep = Annotated[WelcomeResolver, Depends(get_welcome_resolver)]
_GuestSettingsResolverDep = Annotated[
    SettingsResolver, Depends(get_guest_settings_resolver)
]
_ClockDep = Annotated[Clock, Depends(get_clock)]
_SettingsDep = Annotated[Settings, Depends(get_app_settings)]


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class IcalFeedCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_id: str = Field(..., min_length=1, max_length=64)
    unit_id: str | None = Field(default=None, min_length=1, max_length=64)
    url: str = Field(..., min_length=10, max_length=2048)
    provider_override: IcalProvider | None = None
    poll_cadence: str | None = Field(default=None, min_length=1, max_length=128)


class IcalFeedUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, min_length=10, max_length=2048)
    provider_override: IcalProvider | None = None


class IcalFeedResponse(BaseModel):
    id: str
    workspace_id: str
    property_id: str
    unit_id: str | None
    provider: str
    provider_override: str | None
    url_preview: str
    enabled: bool
    poll_cadence: str
    last_polled_at: datetime | None
    last_etag: str | None
    last_error: str | None
    created_at: datetime


class IcalProbeResponse(BaseModel):
    feed_id: str
    ok: bool
    parseable_ics: bool
    error_code: str | None
    polled_at: datetime


class ReservationResponse(BaseModel):
    id: str
    workspace_id: str
    property_id: str
    ical_feed_id: str | None
    external_uid: str
    check_in: datetime
    check_out: datetime
    guest_name: str | None
    guest_count: int | None
    status: str
    source: str
    guest_link_id: str | None
    created_at: datetime


class ReservationListResponse(BaseModel):
    data: list[ReservationResponse]
    next_cursor: str | None = None
    has_more: bool = False


class StayBundleResponse(BaseModel):
    id: str
    workspace_id: str
    reservation_id: str
    kind: str
    tasks: list[dict[str, object]]
    created_at: datetime


class StayBundleListResponse(BaseModel):
    data: list[StayBundleResponse]
    next_cursor: str | None = None
    has_more: bool = False


class StayBundleRegenerateResponse(BaseModel):
    bundle: StayBundleResponse
    generation: dict[str, object]


class GuestLinkIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_hours: int | None = Field(default=None, ge=1, le=24 * 14)


class GuestLinkIssueResponse(BaseModel):
    id: str
    stay_id: str
    token: str
    welcome_url: str
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime


class GuestLinkRevokeResponse(BaseModel):
    id: str
    stay_id: str
    expires_at: datetime
    revoked_at: datetime | None


class ChecklistItemResponse(BaseModel):
    id: str
    label: str


class GuestAssetResponse(BaseModel):
    id: str
    name: str
    guest_instructions_md: str
    cover_photo_url: str | None


class WelcomeResponse(BaseModel):
    property_id: str
    property_name: str
    unit_id: str | None
    unit_name: str | None
    welcome: dict[str, object]
    checklist: list[ChecklistItemResponse]
    assets: list[GuestAssetResponse]
    check_in_at: datetime
    check_out_at: datetime
    guest_name: str | None


class WelcomeGoneResponse(BaseModel):
    error: Literal["welcome_link_expired", "welcome_link_revoked"]
    reason: Literal["expired", "revoked"]


# ---------------------------------------------------------------------------
# Production welcome resolver adapters
# ---------------------------------------------------------------------------


class SqlAlchemyWelcomeResolver:
    """Minimal SQL resolver for the public welcome payload."""

    def fetch(
        self,
        *,
        session: Session,
        workspace_id: str,
        stay_id: str,
    ) -> WelcomeMergeInput | None:
        row = session.get(Reservation, stay_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        prop = session.get(Property, row.property_id)
        if prop is None:
            return None
        return WelcomeMergeInput(
            property_id=prop.id,
            property_name=prop.name if prop.name is not None else prop.address,
            unit_id=None,
            unit_name=None,
            property_defaults=_object_dict(prop.welcome_defaults_json),
            unit_overrides={},
            stay_overrides={},
            stay_wifi_password_override=None,
            checklist=(),
            assets=(),
            check_in_at=_aware_utc(row.check_in),
            check_out_at=_aware_utc(row.check_out),
            guest_name=row.guest_name,
        )


class SqlAlchemyGuestSettingsResolver:
    """Workspace settings resolver for guest welcome feature flags."""

    def resolve_bool(
        self,
        *,
        session: Session,
        workspace_id: str,
        property_id: str,
        unit_id: str | None,
        key: str,
    ) -> bool:
        del property_id, unit_id
        row = session.get(Workspace, workspace_id)
        if row is None:
            return False
        value = row.settings_json.get(key)
        return value is True


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_stays_router() -> APIRouter:
    api = APIRouter(tags=["stays"])

    read_gate = Depends(Permission("stays.read", scope_kind="workspace"))
    manage_gate = Depends(Permission("stays.manage", scope_kind="workspace"))

    @api.get(
        "/ical-feeds",
        response_model=list[IcalFeedResponse],
        operation_id="stays.ical_feeds.list",
        dependencies=[read_gate],
    )
    def list_ical_feeds(
        ctx: _Ctx,
        session: _Db,
        property_id: Annotated[str | None, Query(max_length=64)] = None,
    ) -> list[IcalFeedResponse]:
        return [
            _ical_feed_response(view)
            for view in list_feeds(session, ctx, property_id=property_id)
        ]

    @api.post(
        "/ical-feeds",
        status_code=status.HTTP_201_CREATED,
        response_model=IcalFeedResponse,
        operation_id="stays.ical_feeds.create",
        dependencies=[manage_gate],
    )
    def create_ical_feed(
        body: IcalFeedCreateRequest,
        ctx: _Ctx,
        session: _Db,
        validator: _IcalValidatorDep,
        detector: _ProviderDetectorDep,
        envelope: _EnvelopeDep,
        clock: _ClockDep,
    ) -> IcalFeedResponse:
        try:
            view = register_feed(
                session,
                ctx,
                body=IcalFeedCreate(**body.model_dump()),
                validator=validator,
                detector=detector,
                envelope=envelope,
                clock=clock,
            )
        except IcalUrlInvalid as exc:
            raise _http_for_ical_url(exc) from exc
        return _ical_feed_response(view)

    @api.patch(
        "/ical-feeds/{feed_id}",
        response_model=IcalFeedResponse,
        operation_id="stays.ical_feeds.update",
        dependencies=[manage_gate],
    )
    def patch_ical_feed(
        feed_id: _ID,
        body: IcalFeedUpdateRequest,
        ctx: _Ctx,
        session: _Db,
        validator: _IcalValidatorDep,
        detector: _ProviderDetectorDep,
        envelope: _EnvelopeDep,
        clock: _ClockDep,
    ) -> IcalFeedResponse:
        try:
            view = update_feed(
                session,
                ctx,
                feed_id=feed_id,
                body=IcalFeedUpdate(**body.model_dump()),
                validator=validator,
                detector=detector,
                envelope=envelope,
                clock=clock,
            )
        except IcalFeedNotFound as exc:
            raise _not_found("ical_feed_not_found") from exc
        except IcalUrlInvalid as exc:
            raise _http_for_ical_url(exc) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "ical_feed_update_empty", "message": str(exc)},
            ) from exc
        return _ical_feed_response(view)

    @api.post(
        "/ical-feeds/{feed_id}/disable",
        response_model=IcalFeedResponse,
        operation_id="stays.ical_feeds.disable",
        dependencies=[manage_gate],
    )
    def disable_ical_feed(
        feed_id: _ID,
        ctx: _Ctx,
        session: _Db,
        clock: _ClockDep,
    ) -> IcalFeedResponse:
        try:
            view = disable_feed(session, ctx, feed_id=feed_id, clock=clock)
        except IcalFeedNotFound as exc:
            raise _not_found("ical_feed_not_found") from exc
        return _ical_feed_response(view)

    @api.delete(
        "/ical-feeds/{feed_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="stays.ical_feeds.delete",
        dependencies=[manage_gate],
    )
    def delete_ical_feed(
        feed_id: _ID,
        ctx: _Ctx,
        session: _Db,
        clock: _ClockDep,
    ) -> None:
        try:
            delete_feed(session, ctx, feed_id=feed_id, clock=clock)
        except IcalFeedNotFound as exc:
            raise _not_found("ical_feed_not_found") from exc

    @api.post(
        "/ical-feeds/{feed_id}/poll",
        response_model=IcalProbeResponse,
        operation_id="stays.ical_feeds.poll",
        dependencies=[manage_gate],
    )
    def poll_ical_feed(
        feed_id: _ID,
        ctx: _Ctx,
        session: _Db,
        validator: _IcalValidatorDep,
        envelope: _EnvelopeDep,
        clock: _ClockDep,
    ) -> IcalProbeResponse:
        try:
            result = probe_feed(
                session,
                ctx,
                feed_id=feed_id,
                validator=validator,
                envelope=envelope,
                clock=clock,
            )
        except IcalFeedNotFound as exc:
            raise _not_found("ical_feed_not_found") from exc
        return _ical_probe_response(result)

    @api.get(
        "/reservations",
        response_model=ReservationListResponse,
        operation_id="stays.reservations.list",
        dependencies=[read_gate],
    )
    def list_reservations(
        ctx: _Ctx,
        session: _Db,
        check_in_gte: Annotated[datetime | None, Query()] = None,
        status_filter: Annotated[
            str | None, Query(alias="status", max_length=32)
        ] = None,
        property_id: Annotated[str | None, Query(max_length=64)] = None,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> ReservationListResponse:
        after_id = decode_cursor(cursor)
        rows = _list_reservation_rows(
            session,
            ctx,
            after_id=after_id,
            check_in_gte=check_in_gte,
            status_filter=status_filter,
            property_id=property_id,
            limit=limit,
        )
        page = paginate(rows, limit=limit, key_getter=lambda row: row.id)
        return ReservationListResponse(
            data=[_reservation_response(row) for row in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @api.get(
        "/stay-bundles",
        response_model=StayBundleListResponse,
        operation_id="stays.stay_bundles.list",
        dependencies=[read_gate],
    )
    def list_stay_bundles(
        ctx: _Ctx,
        session: _Db,
        stay_id: Annotated[str | None, Query(max_length=64)] = None,
        reservation_id: Annotated[str | None, Query(max_length=64)] = None,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> StayBundleListResponse:
        after_id = decode_cursor(cursor)
        rows = _list_bundle_rows(
            session,
            ctx,
            after_id=after_id,
            reservation_id=reservation_id if reservation_id is not None else stay_id,
            limit=limit,
        )
        page = paginate(rows, limit=limit, key_getter=lambda row: row.id)
        return StayBundleListResponse(
            data=[_bundle_response(row) for row in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @api.get(
        "/stay-bundles/{bundle_id}",
        response_model=StayBundleResponse,
        operation_id="stays.stay_bundles.get",
        dependencies=[read_gate],
    )
    def get_stay_bundle(
        bundle_id: _ID,
        ctx: _Ctx,
        session: _Db,
    ) -> StayBundleResponse:
        return _bundle_response(_load_bundle(session, ctx, bundle_id=bundle_id))

    @api.post(
        "/stay-bundles/{bundle_id}/regenerate",
        response_model=StayBundleRegenerateResponse,
        operation_id="stays.stay_bundles.regenerate",
        dependencies=[manage_gate],
    )
    def regenerate_stay_bundle(
        bundle_id: _ID,
        ctx: _Ctx,
        session: _Db,
        port: _TasksPortDep,
    ) -> StayBundleRegenerateResponse:
        bundle = _load_bundle(session, ctx, bundle_id=bundle_id)
        result = generate_bundles_for_stay(
            session,
            ctx,
            reservation_id=bundle.reservation_id,
            port=port,
        )
        refreshed = _load_bundle(session, ctx, bundle_id=bundle_id)
        return StayBundleRegenerateResponse(
            bundle=_bundle_response(refreshed),
            generation=_generation_result(result),
        )

    @api.post(
        "/stays/{stay_id}/welcome-link",
        status_code=status.HTTP_201_CREATED,
        response_model=GuestLinkIssueResponse,
        operation_id="stays.guest_links.issue",
        dependencies=[manage_gate],
    )
    @api.post(
        "/stays/{stay_id}/welcome_link",
        status_code=status.HTTP_201_CREATED,
        response_model=GuestLinkIssueResponse,
        dependencies=[manage_gate],
        include_in_schema=False,
    )
    def issue_guest_link(
        request: Request,
        stay_id: _ID,
        body: GuestLinkIssueRequest,
        ctx: _Ctx,
        session: _Db,
        clock: _ClockDep,
        settings: _SettingsDep,
    ) -> GuestLinkIssueResponse:
        reservation = _load_reservation(session, ctx, reservation_id=stay_id)
        ttl = timedelta(hours=body.ttl_hours) if body.ttl_hours is not None else None
        link = mint_link(
            session,
            ctx,
            stay_id=stay_id,
            property_id=reservation.property_id,
            check_out_at=_aware_utc(reservation.check_out),
            ttl=ttl,
            settings=settings,
            clock=clock,
        )
        welcome_url = _welcome_url(request, ctx, token=link.token)
        return GuestLinkIssueResponse(
            id=link.id,
            stay_id=link.stay_id,
            token=link.token,
            welcome_url=welcome_url,
            expires_at=link.expires_at,
            revoked_at=link.revoked_at,
            created_at=link.created_at,
        )

    @api.delete(
        "/stays/{stay_id}/welcome-link",
        response_model=GuestLinkRevokeResponse,
        operation_id="stays.guest_links.revoke_active",
        dependencies=[manage_gate],
    )
    @api.delete(
        "/stays/{stay_id}/welcome_link",
        response_model=GuestLinkRevokeResponse,
        dependencies=[manage_gate],
        include_in_schema=False,
    )
    def revoke_active_guest_link(
        stay_id: _ID,
        ctx: _Ctx,
        session: _Db,
        clock: _ClockDep,
    ) -> GuestLinkRevokeResponse:
        reservation = _load_reservation(session, ctx, reservation_id=stay_id)
        if reservation.guest_link_id is None:
            raise _not_found("guest_link_not_found")
        return _revoke_guest_link_response(
            session,
            ctx,
            link_id=reservation.guest_link_id,
            clock=clock,
        )

    @api.delete(
        "/guest-links/{link_id}",
        response_model=GuestLinkRevokeResponse,
        operation_id="stays.guest_links.revoke",
        dependencies=[manage_gate],
    )
    def revoke_guest_link(
        link_id: _ID,
        ctx: _Ctx,
        session: _Db,
        clock: _ClockDep,
    ) -> GuestLinkRevokeResponse:
        return _revoke_guest_link_response(
            session,
            ctx,
            link_id=link_id,
            clock=clock,
        )

    return api


def build_stays_public_router() -> APIRouter:
    """Return anonymous, token-gated stays endpoints.

    Mounted on the bare ``/api/v1/stays`` tree so the tenancy
    middleware does not require a workspace session before the guest
    token resolver can run.
    """
    api = APIRouter(tags=["stays"])

    @api.get(
        "/welcome",
        response_model=WelcomeResponse,
        responses={410: {"model": WelcomeGoneResponse}},
        operation_id="stays.welcome.read_bearer",
    )
    def read_welcome_bearer(
        request: Request,
        session: _Db,
        welcome_resolver: _WelcomeResolverDep,
        settings_resolver: _GuestSettingsResolverDep,
        settings: _SettingsDep,
        clock: _ClockDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WelcomeResponse:
        token = _bearer_token(authorization)
        return _resolve_welcome_response(
            session,
            token=token,
            request=request,
            welcome_resolver=welcome_resolver,
            settings_resolver=settings_resolver,
            settings=settings,
            clock=clock,
        )

    @api.get(
        "/welcome/{token}",
        response_model=WelcomeResponse,
        responses={410: {"model": WelcomeGoneResponse}},
        operation_id="stays.welcome.read_path_token",
    )
    def read_welcome_path_token(
        token: str,
        session: _Db,
        welcome_resolver: _WelcomeResolverDep,
        settings_resolver: _GuestSettingsResolverDep,
        settings: _SettingsDep,
        clock: _ClockDep,
        request: Request,
    ) -> WelcomeResponse:
        return _resolve_welcome_response(
            session,
            token=token,
            request=request,
            welcome_resolver=welcome_resolver,
            settings_resolver=settings_resolver,
            settings=settings,
            clock=clock,
        )

    return api


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ical_feed_response(view: IcalFeedView) -> IcalFeedResponse:
    return IcalFeedResponse(
        id=view.id,
        workspace_id=view.workspace_id,
        property_id=view.property_id,
        unit_id=view.unit_id,
        provider=view.provider,
        provider_override=view.provider_override,
        url_preview=view.url_preview,
        enabled=view.enabled,
        poll_cadence=view.poll_cadence,
        last_polled_at=view.last_polled_at,
        last_etag=view.last_etag,
        last_error=view.last_error,
        created_at=view.created_at,
    )


def _ical_probe_response(result: IcalProbeResult) -> IcalProbeResponse:
    return IcalProbeResponse(
        feed_id=result.feed_id,
        ok=result.ok,
        parseable_ics=result.parseable_ics,
        error_code=result.error_code,
        polled_at=result.polled_at,
    )


def _reservation_response(row: Reservation) -> ReservationResponse:
    return ReservationResponse(
        id=row.id,
        workspace_id=row.workspace_id,
        property_id=row.property_id,
        ical_feed_id=row.ical_feed_id,
        external_uid=row.external_uid,
        check_in=_aware_utc(row.check_in),
        check_out=_aware_utc(row.check_out),
        guest_name=row.guest_name,
        guest_count=row.guest_count,
        status=row.status,
        source=row.source,
        guest_link_id=row.guest_link_id,
        created_at=_aware_utc(row.created_at),
    )


def _bundle_response(row: StayBundle) -> StayBundleResponse:
    return StayBundleResponse(
        id=row.id,
        workspace_id=row.workspace_id,
        reservation_id=row.reservation_id,
        kind=row.kind,
        tasks=[_object_dict(entry) for entry in row.tasks_json],
        created_at=_aware_utc(row.created_at),
    )


def _list_reservation_rows(
    session: Session,
    ctx: WorkspaceContext,
    *,
    after_id: str | None,
    check_in_gte: datetime | None,
    status_filter: str | None,
    property_id: str | None,
    limit: int,
) -> list[Reservation]:
    stmt = select(Reservation).where(Reservation.workspace_id == ctx.workspace_id)
    if check_in_gte is not None:
        stmt = stmt.where(Reservation.check_in >= check_in_gte)
    if status_filter is not None:
        stmt = stmt.where(Reservation.status == status_filter)
    if property_id is not None:
        stmt = stmt.where(Reservation.property_id == property_id)
    if after_id is not None:
        cursor_row = _load_reservation_or_none(session, ctx, reservation_id=after_id)
        if cursor_row is None:
            return []
        stmt = stmt.where(
            (Reservation.check_in > cursor_row.check_in)
            | (
                (Reservation.check_in == cursor_row.check_in)
                & (Reservation.id > cursor_row.id)
            )
        )
    stmt = stmt.order_by(Reservation.check_in.asc(), Reservation.id.asc()).limit(
        limit + 1
    )
    return list(session.scalars(stmt).all())


def _list_bundle_rows(
    session: Session,
    ctx: WorkspaceContext,
    *,
    after_id: str | None,
    reservation_id: str | None,
    limit: int,
) -> list[StayBundle]:
    stmt = select(StayBundle).where(StayBundle.workspace_id == ctx.workspace_id)
    if reservation_id is not None:
        stmt = stmt.where(StayBundle.reservation_id == reservation_id)
    if after_id is not None:
        cursor_row = _load_bundle_or_none(session, ctx, bundle_id=after_id)
        if cursor_row is None:
            return []
        stmt = stmt.where(
            (StayBundle.created_at > cursor_row.created_at)
            | (
                (StayBundle.created_at == cursor_row.created_at)
                & (StayBundle.id > cursor_row.id)
            )
        )
    stmt = stmt.order_by(StayBundle.created_at.asc(), StayBundle.id.asc()).limit(
        limit + 1
    )
    return list(session.scalars(stmt).all())


def _load_reservation(
    session: Session,
    ctx: WorkspaceContext,
    *,
    reservation_id: str,
) -> Reservation:
    row = _load_reservation_or_none(session, ctx, reservation_id=reservation_id)
    if row is None:
        raise _not_found("reservation_not_found")
    return row


def _load_reservation_or_none(
    session: Session,
    ctx: WorkspaceContext,
    *,
    reservation_id: str,
) -> Reservation | None:
    stmt = select(Reservation).where(
        Reservation.id == reservation_id,
        Reservation.workspace_id == ctx.workspace_id,
    )
    return session.scalars(stmt).one_or_none()


def _load_bundle(
    session: Session,
    ctx: WorkspaceContext,
    *,
    bundle_id: str,
) -> StayBundle:
    row = _load_bundle_or_none(session, ctx, bundle_id=bundle_id)
    if row is None:
        raise _not_found("stay_bundle_not_found")
    return row


def _load_bundle_or_none(
    session: Session,
    ctx: WorkspaceContext,
    *,
    bundle_id: str,
) -> StayBundle | None:
    stmt = select(StayBundle).where(
        StayBundle.id == bundle_id,
        StayBundle.workspace_id == ctx.workspace_id,
    )
    return session.scalars(stmt).one_or_none()


def _generation_result(result: BundleGenerationResult) -> dict[str, object]:
    return {
        "reservation_id": result.reservation_id,
        "skipped_reason": result.skipped_reason,
        "per_rule": [
            {
                "rule_id": outcome.rule_id,
                "bundle_id": outcome.bundle_id,
                "decision": outcome.decision,
                "occurrences": [
                    {
                        "occurrence_key": occurrence.occurrence_key,
                        "occurrence_id": occurrence.occurrence_id,
                        "port_outcome": occurrence.port_outcome,
                        "starts_at": occurrence.starts_at.isoformat(),
                        "ends_at": occurrence.ends_at.isoformat(),
                        "due_by_utc": occurrence.due_by_utc.isoformat(),
                    }
                    for occurrence in outcome.occurrences
                ],
            }
            for outcome in result.per_rule
        ],
    }


def _revoke_guest_link_response(
    session: Session,
    ctx: WorkspaceContext,
    *,
    link_id: str,
    clock: Clock,
) -> GuestLinkRevokeResponse:
    try:
        link = revoke_link(session, ctx, link_id=link_id, clock=clock)
    except GuestLinkNotFound as exc:
        raise _not_found("guest_link_not_found") from exc
    return GuestLinkRevokeResponse(
        id=link.id,
        stay_id=link.stay_id,
        expires_at=link.expires_at,
        revoked_at=link.revoked_at,
    )


def _welcome_url(request: Request, ctx: WorkspaceContext, *, token: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/w/{ctx.workspace_slug}/guest/{token}"


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "missing_bearer_token"},
        )
    scheme, sep, token = authorization.partition(" ")
    if sep != " " or scheme.lower() != "bearer" or token == "":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_bearer_token"},
        )
    return token


def _resolve_welcome_response(
    session: Session,
    *,
    token: str,
    request: Request | None,
    welcome_resolver: WelcomeResolver,
    settings_resolver: SettingsResolver,
    settings: Settings,
    clock: Clock,
) -> WelcomeResponse:
    result = resolve_link(
        session,
        token=token,
        welcome_resolver=welcome_resolver,
        settings_resolver=settings_resolver,
        settings=settings,
        clock=clock,
    )
    if result is None:
        raise _welcome_gone(GuestLinkGoneReason.EXPIRED)
    if isinstance(result, GuestLinkGone):
        _record_welcome_access(
            session,
            request=request,
            link_id=result.link_id,
            workspace_id=result.workspace_id,
            clock=clock,
        )
        raise _welcome_gone(result.reason)
    _record_welcome_access(
        session,
        request=request,
        link_id=result.link_id,
        workspace_id=result.workspace_id,
        clock=clock,
    )
    bundle = result.bundle
    return WelcomeResponse(
        property_id=bundle.property_id,
        property_name=bundle.property_name,
        unit_id=bundle.unit_id,
        unit_name=bundle.unit_name,
        welcome={key: value for key, value in bundle.welcome.items()},
        checklist=[
            ChecklistItemResponse(id=item.id, label=item.label)
            for item in bundle.checklist
        ],
        assets=[_asset_response(asset) for asset in bundle.assets],
        check_in_at=bundle.check_in_at,
        check_out_at=bundle.check_out_at,
        guest_name=bundle.guest_name,
    )


def _record_welcome_access(
    session: Session,
    *,
    request: Request | None,
    link_id: str,
    workspace_id: str,
    clock: Clock,
) -> None:
    ctx = WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="guest",
        actor_id="guest",
        actor_kind="system",
        actor_grant_role="guest",
        actor_was_owner_member=False,
        audit_correlation_id="guest-link-access",
        principal_kind="system",
    )
    ip = "0.0.0.0"
    ua = ""
    if request is not None:
        if request.client is not None:
            ip = request.client.host
        ua = request.headers.get("user-agent", "")
    record_access(session, ctx, link_id=link_id, ip=ip, user_agent=ua, clock=clock)


def _asset_response(asset: GuestAsset) -> GuestAssetResponse:
    return GuestAssetResponse(
        id=asset.id,
        name=asset.name,
        guest_instructions_md=asset.guest_instructions_md,
        cover_photo_url=asset.cover_photo_url,
    )


def _welcome_gone(reason: GuestLinkGoneReason) -> HTTPException:
    error: Literal["welcome_link_expired", "welcome_link_revoked"]
    if reason == GuestLinkGoneReason.REVOKED:
        error = "welcome_link_revoked"
    else:
        error = "welcome_link_expired"
    return HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={"error": error, "reason": reason.value},
    )


def _http_for_ical_url(exc: IcalUrlInvalid) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": exc.code, "message": str(exc)},
    )


def _not_found(error: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": error})


def _object_dict(value: dict[str, Any]) -> dict[str, object]:
    return {str(key): item for key, item in value.items()}


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


router = build_stays_router()
public_router = build_stays_public_router()
