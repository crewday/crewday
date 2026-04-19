"""In-memory :class:`~app.adapters.storage.ports.Storage` fake.

Dict-backed implementation used by every context's unit tests. Satisfies
the ``Storage`` protocol structurally — bytes-in / bytes-out, raises
:class:`~app.adapters.storage.ports.BlobNotFound` on missing hashes,
``delete`` is idempotent.

Similar in shape to the ``_InMemoryStorage`` in
``tests/unit/test_adapter_ports.py``. That file's stub is kept as-is
(it pins the port-contract surface); this module is the shared
implementation unit/integration/api/llm tests consume.

See ``docs/specs/17-testing-quality.md`` §"Unit".
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import IO

from app.adapters.storage.ports import Blob, BlobNotFound

__all__ = ["InMemoryStorage"]


class InMemoryStorage:
    """Dict-backed :class:`~app.adapters.storage.ports.Storage`.

    Keeps the bytes in memory; ``sign_url`` returns a ``memory://``
    URL with the TTL baked into the query string so tests can assert
    on it without reading the body.
    """

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._content_types: dict[str, str | None] = {}

    def put(
        self,
        content_hash: str,
        data: IO[bytes],
        *,
        content_type: str | None = None,
    ) -> Blob:
        payload = data.read()
        self._blobs[content_hash] = payload
        self._content_types[content_hash] = content_type
        return Blob(
            content_hash=content_hash,
            size_bytes=len(payload),
            content_type=content_type,
            created_at=datetime.now(UTC),
        )

    def get(self, content_hash: str) -> IO[bytes]:
        if content_hash not in self._blobs:
            raise BlobNotFound(content_hash)
        return io.BytesIO(self._blobs[content_hash])

    def exists(self, content_hash: str) -> bool:
        return content_hash in self._blobs

    def sign_url(self, content_hash: str, *, ttl_seconds: int) -> str:
        return f"memory://{content_hash}?ttl={ttl_seconds}"

    def delete(self, content_hash: str) -> None:
        self._blobs.pop(content_hash, None)
        self._content_types.pop(content_hash, None)
