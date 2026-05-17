"""Shared helpers for HTTP LLM provider adapters."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from typing import Final

import httpx

from app.util.redact import ConsentSet, redact, scrub_string

_ERROR_DETAIL_DATA_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"data:[^\s\"')]+;base64,[A-Za-z0-9+/=_-]*",
    re.IGNORECASE,
)
_ERROR_DETAIL_BASE64_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/]{128,}={0,2}(?![A-Za-z0-9+/=_-])"
)
_ERROR_DETAIL_MAX_CHARS: Final[int] = 200
_ERROR_DETAIL_SCRUB_CHARS: Final[int] = 2_000


def build_data_url(payload: bytes, *, mime_type: str) -> str:
    """Return a ``data:<mime>;base64,<payload>`` URL for vision requests."""
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def redact_body(
    body: Mapping[str, object], consents: ConsentSet | None
) -> dict[str, object]:
    """Run an outbound provider request body through the LLM redaction seam."""
    effective = consents if consents is not None else ConsentSet.none()
    redacted = redact(dict(body), scope="llm", consents=effective)
    if not isinstance(redacted, dict):  # pragma: no cover - defensive
        raise TypeError("redact() must preserve dict shape on outbound body")
    return redacted


def safe_error_detail(response: httpx.Response) -> str:
    """Return a short, log-safe summary of an error response body."""
    try:
        body = response.json()
    except json.JSONDecodeError:
        return safe_error_detail_text(response.text)
    if isinstance(body, str):
        return safe_error_detail_text(body)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            for key in ("message", "detail"):
                message = error.get(key)
                if isinstance(message, str):
                    return safe_error_detail_text(message)
        if isinstance(error, str):
            return safe_error_detail_text(error)
        for key in ("message", "detail"):
            message = body.get(key)
            if isinstance(message, str):
                return safe_error_detail_text(message)
    return ""


def safe_error_detail_text(value: str) -> str:
    bounded = value[:_ERROR_DETAIL_SCRUB_CHARS]
    without_media = _ERROR_DETAIL_DATA_URL_RE.sub("<redacted:media>", bounded)
    without_media = _ERROR_DETAIL_BASE64_RE.sub("<redacted:media>", without_media)
    return scrub_string(without_media)[:_ERROR_DETAIL_MAX_CHARS]
