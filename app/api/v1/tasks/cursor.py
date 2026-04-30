"""Comment cursor helpers."""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException

from app.api.pagination import decode_cursor, encode_cursor


def _encode_comment_cursor(created_at: datetime, comment_id: str) -> str:
    """Encode the tuple ``(created_at, id)`` as an opaque cursor."""
    return encode_cursor(f"{created_at.isoformat()}|{comment_id}")


def _decode_comment_cursor(
    cursor: str | None,
) -> tuple[datetime | None, str | None]:
    """Decode an opaque comment cursor into ``(created_at, id)`` or the empty
    pair when ``cursor`` is ``None``."""
    if cursor is None or cursor == "":
        return None, None
    raw = decode_cursor(cursor)
    if raw is None:
        return None, None
    # "<iso>|<id>" — a missing pipe is tampered input; collapse to 422
    # via the same envelope as :func:`app.api.pagination.decode_cursor`.
    if "|" not in raw:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_cursor",
                "message": "comment cursor missing separator",
            },
        )
    iso, comment_id = raw.split("|", 1)
    try:
        created_at = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_cursor",
                "message": "comment cursor timestamp is not ISO-8601",
            },
        ) from exc
    return created_at, comment_id
