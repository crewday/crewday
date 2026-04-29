"""Cursor-based pagination helpers shared across v1 routers.

Spec ``docs/specs/12-rest-api.md`` §"Pagination" and §"Request/response
shape" pin the collection envelope:

* ``GET /<resource>?cursor=<opaque>&limit=<int>``
* Response body: ``{"data": [...], "next_cursor": "…", "has_more": …}``
* ``limit`` default 50, max 500.
* No offset pagination.

The helpers here give paginated routers one source of truth for limit
bounds, signed opaque cursors, ``limit + 1`` ``has_more`` detection, and
the optional ``total_estimate`` collection envelope.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, NoReturn

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, asc, desc, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import ColumnElement, Select

from app.auth.keys import derive_subkey
from app.config import get_settings

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "Cursor",
    "CursorPage",
    "CursorScalar",
    "LimitQuery",
    "Page",
    "PageCursorQuery",
    "SortSpec",
    "decode_cursor",
    "decode_page_cursor",
    "encode_cursor",
    "encode_page_cursor",
    "paginate",
    "paginate_query",
    "validate_limit",
]


# Spec §12 "Pagination" — verbatim. Centralised here so a bounds bump
# lands in one place.
DEFAULT_LIMIT: int = 50
MAX_LIMIT: int = 500
_CURSOR_HKDF_PURPOSE = "api-pagination-cursor"
_CURSOR_VERSION = 1
_SIGNATURE_BYTES = 32
# Dev/test contexts often exercise routers without a root key. Keep
# cursors signed in-process there; production deployments should set
# CREWDAY_ROOT_KEY for restart-stable cursors.
_EPHEMERAL_CURSOR_KEY = secrets.token_bytes(_SIGNATURE_BYTES)

type CursorScalar = str | int | float | bool | None
type SortDirection = Literal["asc", "desc"]


# Reusable FastAPI query-param dependencies so every paginated router
# shares the same bounds + description without re-declaring the
# ``ge``/``le`` guards.
LimitQuery = Annotated[
    int,
    Query(
        ge=1,
        le=MAX_LIMIT,
        description=(
            "Maximum rows to return. Default 50, cap 500 per spec §12 "
            "'Pagination'. Rejected with 422 outside ``[1, 500]``."
        ),
    ),
]
PageCursorQuery = Annotated[
    str | None,
    Query(
        max_length=256,
        description=(
            "Opaque forward cursor from the previous page's "
            "``next_cursor``. Omitted on the first call. Bounded to "
            "256 chars to keep the URL below reverse-proxy header "
            "limits."
        ),
    ),
]


@dataclass(frozen=True, slots=True)
class CursorPage[T]:
    """Result of a cursor-paginated query.

    ``items`` is the rows the caller should surface (already trimmed
    to the requested limit). ``next_cursor`` is the opaque string the
    client passes back to fetch the next page, or ``None`` when
    ``has_more`` is ``False``.

    Slots keep the object cheap on the hot path; ``frozen`` means the
    router cannot accidentally stash mutable state on the return
    value between the domain service and the Pydantic projection.
    """

    items: tuple[T, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class Cursor:
    """Structured cursor boundary for SQL-backed pagination."""

    last_sort_value: CursorScalar
    last_id_ulid: str


def _is_none(value: object) -> bool:
    return value is None


class Page[T](BaseModel):
    """Standard collection envelope."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    data: tuple[T, ...]
    next_cursor: str | None
    has_more: bool
    total_estimate: int | None = Field(default=None, exclude_if=_is_none)


@dataclass(frozen=True, slots=True)
class SortSpec[T, S: CursorScalar]:
    """Sort column plus row projection for SQLAlchemy pagination."""

    column: ColumnElement[S]
    value_getter: Callable[[T], S]
    parse_value: Callable[[CursorScalar], S]
    direction: SortDirection = "asc"
    serialize_value: Callable[[S], CursorScalar] | None = None


def validate_limit(limit: int) -> int:
    """Return ``limit`` if it satisfies §12 bounds, else raise 422."""
    if 1 <= limit <= MAX_LIMIT:
        return limit
    raise HTTPException(
        status_code=422,
        detail={
            "error": "validation",
            "message": f"limit must be between 1 and {MAX_LIMIT}",
        },
    )


def _invalid_cursor(message: str) -> NoReturn:
    raise HTTPException(
        status_code=422,
        detail={"error": "invalid_cursor", "message": message},
    )


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str, *, label: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except ValueError, binascii.Error:
        _invalid_cursor(f"cursor {label} is malformed")


def _signing_key() -> bytes:
    settings = get_settings()
    if settings.root_key is None:
        return _EPHEMERAL_CURSOR_KEY
    return derive_subkey(settings.root_key, purpose=_CURSOR_HKDF_PURPOSE)


def _sign(raw: bytes) -> str:
    return _b64encode(hmac.new(_signing_key(), raw, hashlib.sha256).digest())


def _encode_payload(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"{_b64encode(raw)}.{_sign(raw)}"


def _decode_payload(cursor: str) -> dict[str, object]:
    body_part, separator, signature_part = cursor.partition(".")
    if separator == "" or body_part == "" or signature_part == "":
        _invalid_cursor("cursor is malformed")

    body = _b64decode(body_part, label="payload")
    supplied_signature = _b64decode(signature_part, label="signature")
    if len(supplied_signature) != _SIGNATURE_BYTES:
        _invalid_cursor("cursor signature is malformed")
    expected_signature = hmac.new(_signing_key(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        _invalid_cursor("cursor signature is invalid")

    try:
        decoded: object = json.loads(body.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        _invalid_cursor("cursor payload is malformed")
    if not isinstance(decoded, dict):
        _invalid_cursor("cursor payload must be an object")
    payload: dict[str, object] = {}
    for key, value in decoded.items():
        if not isinstance(key, str):
            _invalid_cursor("cursor payload key is malformed")
        payload[key] = value
    if payload.get("v") != _CURSOR_VERSION:
        _invalid_cursor("cursor version is unsupported")
    return payload


def _cursor_scalar(value: object) -> CursorScalar:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    _invalid_cursor("cursor sort value is malformed")


def encode_page_cursor(cursor: Cursor) -> str:
    """Encode a structured SQL cursor as a signed opaque token."""
    cursor.last_id_ulid.encode("ascii")
    return _encode_payload(
        {
            "v": _CURSOR_VERSION,
            "cursor": [cursor.last_sort_value, cursor.last_id_ulid],
        }
    )


def decode_page_cursor(cursor: str | None) -> Cursor | None:
    """Decode a structured SQL cursor, or return ``None`` for first page."""
    if cursor is None or cursor == "":
        return None
    payload = _decode_payload(cursor)
    raw_cursor = payload.get("cursor")
    if isinstance(raw_cursor, list) and len(raw_cursor) == 2:
        sort_value = _cursor_scalar(raw_cursor[0])
        last_id = raw_cursor[1]
        if not isinstance(last_id, str):
            _invalid_cursor("cursor id is malformed")
        return Cursor(last_sort_value=sort_value, last_id_ulid=last_id)

    raw_key = payload.get("key")
    if isinstance(raw_key, str):
        return Cursor(last_sort_value=raw_key, last_id_ulid=raw_key)
    _invalid_cursor("cursor payload is malformed")


def encode_cursor(key: str) -> str:
    """Encode a row key as a signed opaque cursor."""
    key.encode("ascii")
    return _encode_payload({"v": _CURSOR_VERSION, "key": key})


def decode_cursor(cursor: str | None) -> str | None:
    """Return the underlying row key, or ``None`` if ``cursor`` is ``None``.

    A malformed or tampered cursor raises :class:`HTTPException` 422
    rather than silently resetting to the first page. Existing router
    tests assert this ``invalid_cursor`` shape, so the helper keeps 422
    even though older Beads text mentioned 400.
    """
    if cursor is None or cursor == "":
        return None
    payload = _decode_payload(cursor)
    raw_key = payload.get("key")
    if isinstance(raw_key, str):
        return raw_key
    structured = decode_page_cursor(cursor)
    if structured is None:  # pragma: no cover - guarded by non-empty cursor
        return None
    return structured.last_id_ulid


def paginate[T](
    rows: Sequence[T],
    *,
    limit: int,
    key: str | None = None,
    key_getter: Callable[[T], str] | None = None,
) -> CursorPage[T]:
    """Trim ``rows`` to ``limit`` + build the forward-cursor envelope.

    The caller's query fetches ``limit + 1`` rows and passes the full
    slice here; if the extra row is present we trim it off and
    encode the last-returned row's key into ``next_cursor``.

    Either ``key`` (for the simple case where the caller already knows
    the last-returned row's key) or ``key_getter`` (reads it off the
    row object) must be provided when ``len(rows) > limit``. When
    there is no overflow, both are ignored.
    """
    limit = validate_limit(limit)
    has_more = len(rows) > limit
    items = tuple(rows[:limit])
    if not has_more:
        return CursorPage(items=items, next_cursor=None, has_more=False)

    # The row we encode is the last row IN the returned page — passing
    # it back as the cursor means "give me rows strictly after this
    # key". ``items`` is non-empty here because ``has_more`` implies
    # ``len(rows) > limit >= 1``.
    last = items[-1]
    if key_getter is not None:
        cursor_key = key_getter(last)
    elif key is not None:
        cursor_key = key
    else:
        raise ValueError(
            "paginate(rows, limit) requires key or key_getter when has_more"
        )
    return CursorPage(
        items=items,
        next_cursor=encode_cursor(cursor_key),
        has_more=True,
    )


def _seek_condition[T, S: CursorScalar](
    *,
    sort: SortSpec[T, S],
    id_column: ColumnElement[str],
    cursor: Cursor,
) -> ColumnElement[bool]:
    try:
        sort_value = sort.parse_value(cursor.last_sort_value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_cursor",
                "message": "cursor sort value is invalid for this resource",
            },
        ) from exc
    id_after = id_column > cursor.last_id_ulid
    id_before = id_column < cursor.last_id_ulid
    if sort_value is None:
        return and_(
            sort.column.is_(None),
            id_after if sort.direction == "asc" else id_before,
        )
    if sort.direction == "asc":
        return or_(
            sort.column > sort_value,
            and_(sort.column == sort_value, id_column > cursor.last_id_ulid),
            sort.column.is_(None),
        )
    return or_(
        sort.column < sort_value,
        and_(sort.column == sort_value, id_column < cursor.last_id_ulid),
        sort.column.is_(None),
    )


def _order_by[T, S: CursorScalar](
    *, sort: SortSpec[T, S], id_column: ColumnElement[str]
) -> tuple[ColumnElement[S], ColumnElement[str]]:
    if sort.direction == "asc":
        return asc(sort.column).nulls_last(), asc(id_column)
    return desc(sort.column).nulls_last(), desc(id_column)


def paginate_query[T, S: CursorScalar](
    session: Session,
    statement: Select[tuple[T]],
    *,
    sort: SortSpec[T, S],
    id_column: ColumnElement[str],
    id_getter: Callable[[T], str],
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    include_total: bool = False,
) -> Page[T]:
    """Execute a SQLAlchemy keyset-paginated query."""
    limit = validate_limit(limit)
    decoded_cursor = decode_page_cursor(cursor)
    page_statement = statement
    if decoded_cursor is not None:
        page_statement = page_statement.where(
            _seek_condition(sort=sort, id_column=id_column, cursor=decoded_cursor)
        )
    sort_order, id_order = _order_by(sort=sort, id_column=id_column)
    rows = tuple(
        session.scalars(
            page_statement.order_by(sort_order, id_order).limit(limit + 1)
        ).all()
    )
    has_more = len(rows) > limit
    data = rows[:limit]
    next_cursor: str | None = None
    if has_more and data:
        last = data[-1]
        raw_sort_value = sort.value_getter(last)
        sort_value = (
            sort.serialize_value(raw_sort_value)
            if sort.serialize_value is not None
            else raw_sort_value
        )
        next_cursor = encode_page_cursor(
            Cursor(last_sort_value=sort_value, last_id_ulid=id_getter(last))
        )

    total_estimate = None
    if include_total:
        total_statement = select(func.count()).select_from(
            statement.order_by(None).limit(None).offset(None).subquery()
        )
        total_estimate = session.scalar(total_statement) or 0
    return Page(
        data=data,
        next_cursor=next_cursor,
        has_more=has_more,
        total_estimate=total_estimate,
    )
