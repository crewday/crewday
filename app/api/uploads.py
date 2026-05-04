"""Shared upload helpers for API multipart routes."""

from __future__ import annotations

from collections.abc import Callable, Container
from typing import Final

from fastapi import UploadFile

from app.adapters.storage.ports import MimeSniffer

__all__ = [
    "DEFAULT_UPLOAD_CHUNK_SIZE",
    "read_upload_capped",
    "read_upload_file_capped",
    "require_allowed_upload_content_type",
    "require_upload_content_type",
    "sniff_allowed_upload_mime",
]


DEFAULT_UPLOAD_CHUNK_SIZE: Final[int] = 64 * 1024

_TooLargeFactory = Callable[[], Exception]
_MissingContentTypeFactory = Callable[[], Exception]
_RejectedContentTypeFactory = Callable[[str | None], Exception]
_MimeFallback = Callable[[bytes, str], str | None]


async def read_upload_capped(
    upload: UploadFile,
    *,
    max_bytes: int,
    too_large: _TooLargeFactory,
    chunk_size: int = DEFAULT_UPLOAD_CHUNK_SIZE,
) -> bytes:
    """Read an async ``UploadFile`` without buffering more than one byte past cap."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    total = 0
    pieces: list[bytes] = []
    while True:
        read_size = min(chunk_size, max_bytes + 1 - total)
        chunk = await upload.read(read_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            await upload.close()
            raise too_large()
        pieces.append(chunk)
    await upload.close()
    return b"".join(pieces)


def read_upload_file_capped(
    upload: UploadFile,
    *,
    max_bytes: int,
    too_large: _TooLargeFactory,
    chunk_size: int = DEFAULT_UPLOAD_CHUNK_SIZE,
) -> bytes:
    """Read a sync ``UploadFile.file`` without buffering more than one byte past cap."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    total = 0
    pieces: list[bytes] = []
    while True:
        read_size = min(chunk_size, max_bytes + 1 - total)
        chunk = upload.file.read(read_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise too_large()
        pieces.append(chunk)
    return b"".join(pieces)


def require_upload_content_type(
    upload: UploadFile,
    *,
    missing: _MissingContentTypeFactory,
) -> str:
    """Return the declared multipart content type or raise route error."""
    declared_type = upload.content_type
    if declared_type is None or declared_type == "":
        raise missing()
    return declared_type


def require_allowed_upload_content_type(
    upload: UploadFile,
    *,
    allowed: Container[str],
    rejected: _RejectedContentTypeFactory,
) -> str:
    """Return the declared content type if it is in ``allowed``."""
    declared_type = upload.content_type
    if declared_type is None or declared_type not in allowed:
        raise rejected(declared_type)
    return declared_type


def sniff_allowed_upload_mime(
    mime_sniffer: MimeSniffer,
    payload: bytes,
    *,
    declared_type: str,
    allowed: Container[str],
    rejected: _RejectedContentTypeFactory,
    fallback: _MimeFallback | None = None,
) -> str:
    """Sniff upload bytes and enforce a caller-owned MIME allow-list."""
    sniffed = mime_sniffer.sniff(payload, hint=declared_type)
    if sniffed is None and fallback is not None:
        sniffed = fallback(payload, declared_type)
    if sniffed is None or sniffed not in allowed:
        raise rejected(sniffed)
    return sniffed
