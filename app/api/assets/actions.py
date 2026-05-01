"""Asset action endpoints.

Provides :func:`build_asset_actions_subrouter` — the asset-action sub-
router mounted under the core asset router prefix. Owns
``/{asset_id}/actions``, ``/{asset_id}/actions/{action_id}/complete``,
``/{asset_id}/actions/next_due``, and the workspace-level
``/actions/{action_id}`` PATCH/DELETE.

Default-action seam helpers (``_completion_action_spec``,
``_stamp_completion_metadata``, default-action id encoding) live here
because they are unique to the action-recording flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.assets.models import AssetAction as AssetActionRow
from app.api.assets._shared import (
    Ctx,
    Db,
    http_for_action_error,
    http_for_asset_error,
    storage_from_request,
)
from app.api.assets.schemas import (
    AssetActionCreateRequest,
    AssetActionListResponse,
    AssetActionResponse,
    AssetActionUpdateRequest,
    AssetDetailActionResponse,
    AssetNextDueResponse,
)
from app.authz.dep import Permission
from app.domain.assets.actions import (
    AssetActionAccessDenied,
    AssetActionNotFound,
    AssetActionValidationError,
    AssetActionView,
    delete_action,
    list_actions,
    next_due,
    record_action,
    update_action,
)
from app.domain.assets.assets import (
    AssetNotFound,
    AssetView,
    get_asset,
)
from app.domain.assets.types import AssetTypeView
from app.tenancy import WorkspaceContext, tenant_agnostic

__all__ = [
    "DEFAULT_ACTION_PREFIX",
    "asset_detail_actions",
    "build_asset_actions_subrouter",
    "default_action_id",
    "default_action_tracking_keys",
]


DEFAULT_ACTION_PREFIX = "default__"


@dataclass(frozen=True, slots=True)
class _CompletionActionSpec:
    kind: str
    tracking_key: str | None
    label: str | None


def asset_detail_actions(
    asset: AssetView,
    asset_type: AssetTypeView | None,
    action_views: list[AssetActionView],
) -> list[AssetDetailActionResponse]:
    if asset_type is None:
        return [_detail_action_from_view(asset, action) for action in action_views]
    default_keys = default_action_tracking_keys(asset_type)
    actions = [
        _detail_action_from_view(asset, action)
        for action in action_views
        if action.key not in default_keys
    ]
    return _default_detail_actions(asset, asset_type, action_views) + actions


def _detail_action_from_view(
    asset: AssetView,
    action: AssetActionView,
) -> AssetDetailActionResponse:
    return AssetDetailActionResponse(
        id=action.id,
        asset_id=action.asset_id,
        key=action.key,
        kind=action.kind,
        label=action.label,
        interval_days=action.interval_days,
        last_performed_at=action.last_performed_at,
        next_due_on=_next_due_date(
            asset,
            action.last_performed_at,
            action.interval_days,
        ),
        linked_task_id=None,
        linked_schedule_id=None,
        description=action.description_md,
        estimated_duration_minutes=None,
    )


def _default_detail_actions(
    asset: AssetView,
    asset_type: AssetTypeView,
    action_views: list[AssetActionView],
) -> list[AssetDetailActionResponse]:
    actions: list[AssetDetailActionResponse] = []
    for index, item in enumerate(asset_type.default_actions):
        interval_days = _positive_int(item.get("interval_days"))
        kind = item.get("kind")
        if interval_days is None or not isinstance(kind, str):
            continue
        key = item.get("key")
        key_str = key if isinstance(key, str) and key else None
        label = item.get("label")
        label_str = label.strip() if isinstance(label, str) and label.strip() else kind
        tracking_key = _default_action_tracking_key(
            index=index,
            key=key_str,
            kind=kind,
        )
        last = _latest_action(action_views, key=tracking_key, kind=kind)
        actions.append(
            AssetDetailActionResponse(
                id=default_action_id(index=index, key=key_str, kind=kind),
                asset_id=asset.id,
                key=key_str,
                kind=kind,
                label=label_str,
                interval_days=interval_days,
                last_performed_at=last.last_performed_at if last is not None else None,
                next_due_on=_next_due_date(
                    asset,
                    last.last_performed_at if last is not None else None,
                    interval_days,
                ),
                linked_task_id=None,
                linked_schedule_id=None,
                description=None,
                estimated_duration_minutes=_positive_int(
                    item.get("estimated_duration_minutes")
                ),
            )
        )
    return actions


def default_action_tracking_keys(asset_type: AssetTypeView) -> set[str]:
    keys: set[str] = set()
    for index, item in enumerate(asset_type.default_actions):
        kind = item.get("kind")
        if not isinstance(kind, str):
            continue
        key = item.get("key")
        key_str = key if isinstance(key, str) and key else None
        keys.add(_default_action_tracking_key(index=index, key=key_str, kind=kind))
    return keys


def _latest_action(
    actions: list[AssetActionView],
    *,
    key: str | None,
    kind: str,
) -> AssetActionView | None:
    if key is not None:
        matches = [
            action
            for action in actions
            if action.last_performed_at is not None and action.key == key
        ]
        if not matches:
            return None
        return max(matches, key=lambda action: _as_utc(action.last_performed_at))
    matches = [
        action
        for action in actions
        if action.last_performed_at is not None and action.kind == kind
    ]
    if not matches:
        return None
    return max(matches, key=lambda action: _as_utc(action.last_performed_at))


def _next_due_date(
    asset: AssetView,
    last_performed_at: datetime | None,
    interval_days: int | None,
) -> date | None:
    if interval_days is None:
        return None
    base = (
        _as_utc(last_performed_at)
        if last_performed_at is not None
        else _asset_start(asset)
    )
    return (base + timedelta(days=interval_days)).date()


def _asset_start(asset: AssetView) -> datetime:
    start = asset.installed_on or asset.purchased_on
    if start is not None:
        return datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    return _as_utc(asset.created_at)


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("datetime value is required")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def default_action_id(*, index: int, key: str | None, kind: str) -> str:
    return f"{DEFAULT_ACTION_PREFIX}{index}__{key or kind}"


def _default_action_tracking_key(*, index: int, key: str | None, kind: str) -> str:
    return key or default_action_id(index=index, key=None, kind=kind)


def _completion_action_spec(
    session: Session,
    ctx: WorkspaceContext,
    asset: AssetView,
    action_id: str,
    *,
    asset_type: AssetTypeView | None,
) -> _CompletionActionSpec:
    if action_id.startswith(DEFAULT_ACTION_PREFIX):
        if asset_type is None:
            raise AssetActionNotFound()
        for index, item in enumerate(asset_type.default_actions):
            kind = item.get("kind")
            key = item.get("key")
            key_str = key if isinstance(key, str) and key else None
            label = item.get("label")
            label_str = (
                label.strip() if isinstance(label, str) and label.strip() else None
            )
            if (
                isinstance(kind, str)
                and default_action_id(
                    index=index,
                    key=key_str,
                    kind=kind,
                )
                == action_id
            ):
                return _CompletionActionSpec(
                    kind=kind,
                    tracking_key=_default_action_tracking_key(
                        index=index,
                        key=key_str,
                        kind=kind,
                    ),
                    label=label_str,
                )
        raise AssetActionNotFound()

    with tenant_agnostic():
        row = session.scalar(
            select(AssetActionRow).where(
                AssetActionRow.id == action_id,
                AssetActionRow.workspace_id == ctx.workspace_id,
                AssetActionRow.asset_id == asset.id,
                AssetActionRow.deleted_at.is_(None),
            )
        )
    if row is None:
        raise AssetActionNotFound()
    return _CompletionActionSpec(kind=row.kind, tracking_key=row.key, label=row.label)


def _stamp_completion_metadata(
    session: Session,
    view: AssetActionView,
    spec: _CompletionActionSpec,
) -> AssetActionView:
    if spec.tracking_key is None and spec.label is None:
        return view
    with tenant_agnostic():
        row = session.get(AssetActionRow, view.id)
    if row is None:
        return view
    if spec.tracking_key is not None:
        row.key = spec.tracking_key
    if spec.label is not None:
        row.label = spec.label
    session.flush()
    return AssetActionView(
        id=row.id,
        workspace_id=row.workspace_id,
        asset_id=row.asset_id,
        key=row.key,
        kind=row.kind,
        label=row.label,
        description_md=row.description_md,
        interval_days=row.interval_days,
        last_performed_at=row.last_performed_at,
        performed_at=row.performed_at,
        performed_by=row.performed_by,
        notes_md=row.notes_md,
        meter_reading=row.meter_reading,
        evidence_blob_hash=row.evidence_blob_hash,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def build_asset_actions_subrouter() -> APIRouter:
    """Sub-router mounted under the core asset router prefix.

    Owns asset-action endpoints. Paths are unchanged when this is
    included into the asset router; operation IDs preserve the
    ``assets.actions.*`` namespace.
    """

    # Parent router already carries ``tags=["assets"]``; not setting it
    # here keeps the per-route tag list byte-identical to the
    # pre-split layout.
    api = APIRouter()
    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))
    edit_gate = Depends(Permission("assets.edit", scope_kind="workspace"))

    @api.get(
        "/{asset_id}/actions",
        response_model=AssetActionListResponse,
        operation_id="assets.actions.list",
        summary="List asset actions",
        dependencies=[view_gate],
    )
    def actions(
        asset_id: str,
        ctx: Ctx,
        session: Db,
        kind: Annotated[
            Literal["service", "repair", "replace", "inspect", "read"] | None, Query()
        ] = None,
        since: Annotated[datetime | None, Query()] = None,
        until: Annotated[datetime | None, Query()] = None,
    ) -> AssetActionListResponse:
        try:
            views = list_actions(
                session,
                ctx,
                asset_id,
                kind=kind,
                since=since,
                until=until,
            )
        except (AssetNotFound, AssetActionValidationError) as exc:
            raise http_for_action_error(exc) from exc
        return AssetActionListResponse(
            data=[AssetActionResponse.from_view(view) for view in views]
        )

    @api.post(
        "/{asset_id}/actions",
        response_model=AssetActionResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="assets.actions.record",
        summary="Record an asset action",
    )
    def record(
        asset_id: str,
        body: AssetActionCreateRequest,
        ctx: Ctx,
        session: Db,
        request: Request,
    ) -> AssetActionResponse:
        try:
            view = record_action(
                session,
                ctx,
                asset_id,
                kind=body.kind,
                performed_at=body.performed_at,
                notes_md=body.notes_md,
                meter_reading=body.meter_reading,
                evidence_blob_hash=body.evidence_blob_hash,
                storage=(
                    storage_from_request(request)
                    if body.evidence_blob_hash is not None
                    else None
                ),
            )
        except (
            AssetNotFound,
            AssetActionAccessDenied,
            AssetActionValidationError,
        ) as exc:
            raise http_for_action_error(exc) from exc
        return AssetActionResponse.from_view(view)

    @api.post(
        "/{asset_id}/actions/{action_id}/complete",
        response_model=AssetActionResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="assets.actions.complete",
        summary="Mark an asset action as done",
    )
    def complete_action(
        asset_id: str,
        action_id: str,
        ctx: Ctx,
        session: Db,
    ) -> AssetActionResponse:
        from app.api.assets.detail import asset_type_for

        try:
            asset = get_asset(session, ctx, asset_id=asset_id)
            asset_type = asset_type_for(session, ctx, asset)
            spec = _completion_action_spec(
                session,
                ctx,
                asset,
                action_id,
                asset_type=asset_type,
            )
            view = record_action(session, ctx, asset_id, kind=spec.kind)
            view = _stamp_completion_metadata(session, view, spec)
        except (
            AssetNotFound,
            AssetActionAccessDenied,
            AssetActionNotFound,
            AssetActionValidationError,
        ) as exc:
            raise http_for_action_error(exc) from exc
        return AssetActionResponse.from_view(view)

    @api.get(
        "/{asset_id}/actions/next_due",
        response_model=AssetNextDueResponse | None,
        operation_id="assets.actions.next_due",
        summary="Return the next due asset action",
        dependencies=[view_gate],
    )
    def next_due_action(
        asset_id: str,
        ctx: Ctx,
        session: Db,
    ) -> AssetNextDueResponse | None:
        try:
            view = next_due(session, ctx, asset_id)
        except AssetNotFound as exc:
            raise http_for_asset_error(exc) from exc
        return AssetNextDueResponse.from_view(view) if view is not None else None

    @api.patch(
        "/actions/{action_id}",
        response_model=AssetActionResponse,
        operation_id="assets.actions.update",
        summary="Update an asset action",
        dependencies=[edit_gate],
    )
    def patch_action(
        action_id: str,
        body: AssetActionUpdateRequest,
        ctx: Ctx,
        session: Db,
        request: Request,
    ) -> AssetActionResponse:
        try:
            view = update_action(
                session,
                ctx,
                action_id,
                label=body.label,
                performed_at=body.performed_at,
                notes_md=body.notes_md,
                meter_reading=body.meter_reading,
                evidence_blob_hash=body.evidence_blob_hash,
                storage=(
                    storage_from_request(request)
                    if body.evidence_blob_hash is not None
                    else None
                ),
            )
        except (AssetNotFound, AssetActionNotFound, AssetActionValidationError) as exc:
            raise http_for_action_error(exc) from exc
        return AssetActionResponse.from_view(view)

    @api.delete(
        "/actions/{action_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="assets.actions.delete",
        summary="Delete an asset action",
        dependencies=[edit_gate],
    )
    def delete_recorded_action(
        action_id: str,
        ctx: Ctx,
        session: Db,
    ) -> Response:
        try:
            delete_action(session, ctx, action_id)
        except (AssetNotFound, AssetActionNotFound) as exc:
            raise http_for_action_error(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return api
