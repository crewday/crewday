"""Workspace export artifact builder."""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Final

from sqlalchemy import RowMapping, Table, select
from sqlalchemy.orm import Session

from app.adapters.db import audit as _audit_models  # noqa: F401
from app.adapters.db import authz as _authz_models  # noqa: F401
from app.adapters.db import availability as _availability_models  # noqa: F401
from app.adapters.db import billing as _billing_models  # noqa: F401
from app.adapters.db import expenses as _expenses_models  # noqa: F401
from app.adapters.db import holidays as _holidays_models  # noqa: F401
from app.adapters.db import identity as _identity_models  # noqa: F401
from app.adapters.db import instructions as _instructions_models  # noqa: F401
from app.adapters.db import integrations as _integrations_models  # noqa: F401
from app.adapters.db import inventory as _inventory_models  # noqa: F401
from app.adapters.db import issues as _issues_models  # noqa: F401
from app.adapters.db import llm as _llm_models  # noqa: F401
from app.adapters.db import messaging as _messaging_models  # noqa: F401
from app.adapters.db import payroll as _payroll_models  # noqa: F401
from app.adapters.db import places as _places_models  # noqa: F401
from app.adapters.db import stays as _stays_models  # noqa: F401
from app.adapters.db import tasks as _tasks_models  # noqa: F401
from app.adapters.db import time as _time_models  # noqa: F401
from app.adapters.db import workspace as _workspace_models  # noqa: F401
from app.adapters.db.base import Base
from app.adapters.db.workspace.models import Workspace
from app.adapters.storage.ports import BlobNotFound, Storage
from app.authz.owners import is_owner_member
from app.domain.workspace.settings_service import OwnersOnlyError
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock

__all__ = [
    "WORKSPACE_EXPORT_MEDIA_TYPE",
    "WORKSPACE_EXPORT_TABLES",
    "WorkspaceExportArtifact",
    "build_workspace_export",
]


WORKSPACE_EXPORT_MEDIA_TYPE: Final[str] = "application/zip"
WORKSPACE_EXPORT_SCHEMA_VERSION: Final[int] = 1
_FIXED_ZIP_TIMESTAMP: Final[tuple[int, int, int, int, int, int]] = (
    1980,
    1,
    1,
    0,
    0,
    0,
)
_HEX_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")

_WORKSPACE_TABLES: Final[tuple[str, ...]] = (
    "workspace",
    "agent_preference",
    "agent_preference_revision",
    "approval_request",
    "area",
    "asset",
    "asset_action",
    "asset_document",
    "asset_type",
    "audit_log",
    "booking",
    "budget_ledger",
    "checklist_item",
    "checklist_template_item",
    "chat_channel",
    "chat_channel_binding",
    "chat_channel_member",
    "chat_gateway_binding",
    "chat_message",
    "comment",
    "digest_record",
    "email_delivery",
    "email_opt_out",
    "evidence",
    "expense_attachment",
    "expense_claim",
    "expense_line",
    "file_extraction",
    "geofence_setting",
    "guest_link",
    "ical_feed",
    "instruction",
    "instruction_link",
    "instruction_version",
    "inventory_item",
    "inventory_movement",
    "inventory_reorder_rule",
    "inventory_stocktake",
    "inventory_stocktake_line",
    "invite",
    "issue_report",
    "leave",
    "llm_assignment",
    "llm_capability_inheritance",
    "llm_usage",
    "nl_task_preview",
    "notification",
    "notification_push_queue",
    "occurrence",
    "organization",
    "pay_period",
    "pay_period_entry",
    "pay_rule",
    "payout_destination",
    "payslip",
    "permission_group",
    "permission_group_member",
    "property",
    "property_closure",
    "property_work_role_assignment",
    "property_workspace",
    "public_holiday",
    "push_token",
    "quote",
    "rate_card",
    "reservation",
    "role_grant",
    "schedule",
    "shift",
    "stay_bundle",
    "task_approval",
    "task_completion",
    "task_template",
    "unit",
    "user_availability_override",
    "user_leave",
    "user_weekly_availability",
    "user_work_role",
    "user_workspace",
    "vendor_invoice",
    "webhook_delivery",
    "webhook_subscription",
    "work_engagement",
    "work_order",
    "work_order_shift_accrual",
    "work_role",
)
WORKSPACE_EXPORT_TABLES: Final[tuple[str, ...]] = _WORKSPACE_TABLES
_REDACTED_VALUE: Final[str] = "<redacted>"
_REDACTED_COLUMNS: Final[dict[str, frozenset[str]]] = {
    "asset": frozenset(("qr_token",)),
    "guest_link": frozenset(("token",)),
    "ical_feed": frozenset(("url",)),
    "payout_destination": frozenset(("secret_ref_id",)),
    "push_token": frozenset(("endpoint", "p256dh", "auth")),
    "webhook_subscription": frozenset(("secret_blob",)),
}
_BLOB_DIRECT_COLUMNS: Final[tuple[str, ...]] = (
    "blob_hash",
    "cover_photo_file_id",
    "evidence_blob_hash",
    "file_id",
    "pdf_blob_hash",
    "proof_blob_hash",
)
_BLOB_JSON_COLUMNS: Final[tuple[str, ...]] = (
    "attachment_file_ids_json",
    "attachments_json",
    "evidence_blob_hashes",
    "proof_of_payment_file_ids",
)
_PROPERTY_OWNED_TABLES: Final[tuple[str, ...]] = (
    "area",
    "property",
    "property_closure",
    "unit",
)


@dataclass(frozen=True, slots=True)
class WorkspaceExportArtifact:
    filename: str
    content_type: str
    content: bytes
    manifest: dict[str, Any]


def build_workspace_export(
    session: Session,
    ctx: WorkspaceContext,
    *,
    storage: Storage,
    clock: Clock | None = None,
) -> WorkspaceExportArtifact:
    """Build a ZIP containing allowlisted rows and referenced upload blobs."""
    if not is_owner_member(
        session, workspace_id=ctx.workspace_id, user_id=ctx.actor_id
    ):
        raise OwnersOnlyError(
            f"actor {ctx.actor_id!r} is not an owners-group member of "
            f"workspace {ctx.workspace_id!r}"
        )

    resolved_clock = clock if clock is not None else SystemClock()
    exported_at = resolved_clock.now().astimezone(UTC)

    # justification: owner-gated workspace export reads allowlisted tables keyed by
    # explicit workspace_id=ctx.workspace_id; Workspace itself is the tenant root.
    with tenant_agnostic():
        workspace = session.get(Workspace, ctx.workspace_id)
        if workspace is None:
            raise RuntimeError(
                f"workspace {ctx.workspace_id!r} present in ctx but absent in DB"
            )
        property_ids = _workspace_owned_property_ids(
            session, workspace_id=ctx.workspace_id
        )
        data, row_counts, blob_refs = _collect_rows(
            session,
            workspace_id=ctx.workspace_id,
            property_ids=property_ids,
        )

    present_files, missing_files, file_payloads = _collect_files(storage, blob_refs)
    manifest = _manifest(
        workspace=workspace,
        exported_at=exported_at,
        row_counts=row_counts,
        present_files=present_files,
        missing_files=missing_files,
    )
    content = _zip_bytes(data=data, manifest=manifest, file_payloads=file_payloads)
    filename = f"crewday-workspace-{workspace.slug}-{exported_at:%Y%m%dT%H%M%SZ}.zip"
    return WorkspaceExportArtifact(
        filename=filename,
        content_type=WORKSPACE_EXPORT_MEDIA_TYPE,
        content=content,
        manifest=manifest,
    )


def _collect_rows(
    session: Session,
    *,
    workspace_id: str,
    property_ids: tuple[str, ...],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int], set[str]]:
    data: dict[str, list[dict[str, Any]]] = {}
    row_counts: dict[str, int] = {}
    blob_refs: set[str] = set()
    for table_name in _WORKSPACE_TABLES:
        table = Base.metadata.tables[table_name]
        rows = [
            _redact_row(table, _jsonable_row(row))
            for row in _table_rows(session, table, workspace_id, property_ids)
        ]
        data[table_name] = rows
        row_counts[table_name] = len(rows)
        for row in rows:
            blob_refs.update(_blob_hashes_from_row(table, row))
    return data, row_counts, blob_refs


def _table_rows(
    session: Session,
    table: Table,
    workspace_id: str,
    property_ids: tuple[str, ...],
) -> list[RowMapping]:
    if table.name == "workspace":
        stmt = select(table).where(table.c.id == workspace_id)
    elif table.name in _PROPERTY_OWNED_TABLES:
        if not property_ids:
            return []
        if table.name == "property":
            stmt = select(table).where(table.c.id.in_(property_ids))
        else:
            stmt = select(table).where(table.c.property_id.in_(property_ids))
    else:
        stmt = select(table).where(table.c.workspace_id == workspace_id)
    return list(session.execute(stmt.order_by(*_order_columns(table))).mappings().all())


def _workspace_owned_property_ids(
    session: Session, *, workspace_id: str
) -> tuple[str, ...]:
    table = Base.metadata.tables["property_workspace"]
    stmt = (
        select(table.c.property_id)
        .where(table.c.workspace_id == workspace_id)
        .where(table.c.membership_role == "owner_workspace")
        .order_by(table.c.property_id)
    )
    return tuple(session.scalars(stmt).all())


def _order_columns(table: Table) -> tuple[Any, ...]:
    primary_key = tuple(table.primary_key.columns)
    if primary_key:
        return primary_key
    return tuple(table.columns)


def _jsonable_row(row: RowMapping) -> dict[str, Any]:
    return {key: _jsonable_value(value) for key, value in row.items()}


def _redact_row(table: Table, row: dict[str, Any]) -> dict[str, Any]:
    redacted_columns = _REDACTED_COLUMNS.get(table.name, frozenset())
    if not redacted_columns:
        return row
    return {
        key: _REDACTED_VALUE if key in redacted_columns and value is not None else value
        for key, value in row.items()
    }


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _blob_hashes_from_row(table: Table, row: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for column in _BLOB_DIRECT_COLUMNS:
        if column in table.c:
            refs.update(_hashes_from_value(row.get(column)))
    for column in _BLOB_JSON_COLUMNS:
        if column in table.c:
            refs.update(_hashes_from_value(row.get(column)))
    return refs


def _hashes_from_value(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        if _HEX_SHA256_RE.fullmatch(value):
            found.add(value)
    elif isinstance(value, list):
        for item in value:
            found.update(_hashes_from_value(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.update(_hashes_from_value(item))
    return found


def _collect_files(
    storage: Storage, blob_refs: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, bytes]]:
    present: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    payloads: dict[str, bytes] = {}
    for content_hash in sorted(blob_refs):
        try:
            with storage.get(content_hash) as handle:
                payload = handle.read()
        except BlobNotFound:
            missing.append({"content_hash": content_hash})
            continue
        archive_path = f"files/{content_hash[:2]}/{content_hash}"
        payloads[archive_path] = payload
        present.append(
            {
                "content_hash": content_hash,
                "path": archive_path,
                "size_bytes": len(payload),
            }
        )
    return present, missing, payloads


def _manifest(
    *,
    workspace: Workspace,
    exported_at: datetime,
    row_counts: dict[str, int],
    present_files: list[dict[str, Any]],
    missing_files: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": WORKSPACE_EXPORT_SCHEMA_VERSION,
        "app_version": _app_version(),
        "exported_at": exported_at.isoformat().replace("+00:00", "Z"),
        "workspace": {
            "id": workspace.id,
            "slug": workspace.slug,
            "name": workspace.name,
        },
        "tables": [
            {
                "name": table_name,
                "path": f"data/{table_name}.json",
                "row_count": row_counts[table_name],
            }
            for table_name in sorted(row_counts)
        ],
        "files": present_files,
        "missing_files": missing_files,
    }


def _app_version() -> str:
    try:
        return version("crewday")
    except PackageNotFoundError:
        return "0.0.1"


def _zip_bytes(
    *,
    data: dict[str, list[dict[str, Any]]],
    manifest: dict[str, Any],
    file_payloads: dict[str, bytes],
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_json(archive, "manifest.json", manifest)
        for table_name in sorted(data):
            _write_json(archive, f"data/{table_name}.json", data[table_name])
        for path in sorted(file_payloads):
            _write_bytes(archive, path, file_payloads[path])
    return buffer.getvalue()


def _write_json(archive: zipfile.ZipFile, path: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode(
        "utf-8"
    )
    _write_bytes(archive, path, payload)


def _write_bytes(archive: zipfile.ZipFile, path: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(path, date_time=_FIXED_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, payload)
