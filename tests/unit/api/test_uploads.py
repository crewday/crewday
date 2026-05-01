from __future__ import annotations

import io

import pytest
from fastapi import HTTPException, UploadFile, status
from starlette.datastructures import Headers

from app.api.uploads import (
    read_upload_capped,
    read_upload_file_capped,
    require_allowed_upload_content_type,
    require_upload_content_type,
    sniff_allowed_upload_mime,
)


class _StaticSniffer:
    def __init__(self, verdict: str | None) -> None:
        self._verdict = verdict

    def sniff(self, payload: bytes, *, hint: str | None = None) -> str | None:
        return self._verdict


class _TrackingUpload(UploadFile):
    def __init__(self, payload: bytes, content_type: str | None = "text/plain") -> None:
        super().__init__(
            file=io.BytesIO(payload),
            filename="upload.bin",
            headers=Headers(
                {} if content_type is None else {"content-type": content_type}
            ),
        )
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return await super().read(size)


class _TrackingBytesIO(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


def _upload(payload: bytes, content_type: str | None = "text/plain") -> UploadFile:
    headers = Headers({} if content_type is None else {"content-type": content_type})
    return UploadFile(file=io.BytesIO(payload), filename="upload.bin", headers=headers)


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail={"error": "too_large"},
    )


def _missing_type() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        detail={"error": "missing_type"},
    )


def _rejected_type(content_type: str | None) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        detail={"error": "rejected_type", "content_type": content_type},
    )


@pytest.mark.asyncio
async def test_read_upload_capped_accepts_exact_limit() -> None:
    upload = _upload(b"abcd")

    assert (
        await read_upload_capped(upload, max_bytes=4, too_large=_too_large) == b"abcd"
    )


@pytest.mark.asyncio
async def test_read_upload_capped_rejects_over_limit() -> None:
    upload = _upload(b"abcde")

    with pytest.raises(HTTPException) as exc:
        await read_upload_capped(
            upload,
            max_bytes=4,
            too_large=_too_large,
            chunk_size=2,
        )

    assert exc.value.status_code == status.HTTP_413_CONTENT_TOO_LARGE
    assert exc.value.detail == {"error": "too_large"}


@pytest.mark.asyncio
async def test_read_upload_capped_reads_at_most_one_byte_past_limit() -> None:
    upload = _TrackingUpload(b"abcde")

    with pytest.raises(HTTPException):
        await read_upload_capped(upload, max_bytes=4, too_large=_too_large)

    assert upload.read_sizes == [5]


@pytest.mark.asyncio
async def test_read_upload_capped_returns_empty_body() -> None:
    upload = _upload(b"")

    assert await read_upload_capped(upload, max_bytes=4, too_large=_too_large) == b""


def test_read_upload_file_capped_rejects_over_limit_sync_upload() -> None:
    upload = _upload(b"abcde")

    with pytest.raises(HTTPException) as exc:
        read_upload_file_capped(
            upload,
            max_bytes=4,
            too_large=_too_large,
            chunk_size=2,
        )

    assert exc.value.status_code == status.HTTP_413_CONTENT_TOO_LARGE
    assert exc.value.detail == {"error": "too_large"}


def test_read_upload_file_capped_reads_at_most_one_byte_past_limit() -> None:
    stream = _TrackingBytesIO(b"abcde")
    upload = UploadFile(file=stream, filename="upload.bin")

    with pytest.raises(HTTPException):
        read_upload_file_capped(upload, max_bytes=4, too_large=_too_large)

    assert stream.read_sizes == [5]


def test_require_upload_content_type_rejects_missing_header() -> None:
    upload = _upload(b"payload", content_type=None)

    with pytest.raises(HTTPException) as exc:
        require_upload_content_type(upload, missing=_missing_type)

    assert exc.value.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    assert exc.value.detail == {"error": "missing_type"}


def test_require_allowed_upload_content_type_accepts_allowlisted_header() -> None:
    upload = _upload(b"payload", content_type="image/png")

    assert (
        require_allowed_upload_content_type(
            upload,
            allowed={"image/png"},
            rejected=_rejected_type,
        )
        == "image/png"
    )


def test_require_allowed_upload_content_type_rejects_disallowed_header() -> None:
    upload = _upload(b"payload", content_type="image/gif")

    with pytest.raises(HTTPException) as exc:
        require_allowed_upload_content_type(
            upload,
            allowed={"image/png"},
            rejected=_rejected_type,
        )

    assert exc.value.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    assert exc.value.detail == {
        "error": "rejected_type",
        "content_type": "image/gif",
    }


def test_sniff_allowed_upload_mime_rejects_disallowed_verdict() -> None:
    with pytest.raises(HTTPException) as exc:
        sniff_allowed_upload_mime(
            _StaticSniffer("application/x-msdownload"),
            b"MZ",
            declared_type="image/png",
            allowed={"image/png"},
            rejected=_rejected_type,
        )

    assert exc.value.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    assert exc.value.detail == {
        "error": "rejected_type",
        "content_type": "application/x-msdownload",
    }


def test_sniff_allowed_upload_mime_accepts_fallback_verdict() -> None:
    verdict = sniff_allowed_upload_mime(
        _StaticSniffer(None),
        b"hello",
        declared_type="text/plain",
        allowed={"text/plain"},
        rejected=_rejected_type,
        fallback=lambda _payload, _declared_type: "text/plain",
    )

    assert verdict == "text/plain"
