"""PNG QR rendering via segno."""

from __future__ import annotations

from io import BytesIO

import segno

__all__ = ["render_qr"]


def render_qr(data: str, *, size: int = 512, label: str | None = None) -> bytes:
    """Render ``data`` as PNG bytes.

    ``label`` is accepted for the print/PDF seam but not drawn into the
    bitmap; printed sheets compose labels around this PNG separately.
    """
    if not data:
        raise ValueError("data is required")
    if size < 64:
        raise ValueError("size must be >= 64")
    qr = segno.make(data, error="m")
    stream = BytesIO()
    scale = max(1, size // 64)
    qr.save(stream, kind="png", scale=scale, border=4)
    return stream.getvalue()
