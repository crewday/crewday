"""Shared NDJSON audit-tail transport helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from time import sleep
from typing import Final

from pydantic import BaseModel

NDJSON_MEDIA_TYPE: Final[str] = "application/x-ndjson"


@dataclass(frozen=True)
class AuditTailCursor:
    """Stable keyset cursor for audit-tail follow polling."""

    created_at: datetime
    row_id: str


def ndjson_lines(rows: Iterable[BaseModel]) -> Iterator[bytes]:
    """Encode pydantic response rows as one NDJSON line each."""
    for row in rows:
        yield row.model_dump_json().encode("utf-8") + b"\n"


def audit_tail_chunks[RowT](
    *,
    fetch_initial: Callable[[], Sequence[RowT]],
    fetch_next: Callable[[AuditTailCursor | None], Sequence[RowT]],
    project_row: Callable[[RowT], BaseModel],
    cursor_for: Callable[[RowT], AuditTailCursor],
    follow: bool,
    poll_interval_seconds: float,
    empty_keepalive: bool = True,
    max_empty_polls: int | None = None,
) -> Iterator[bytes]:
    """Yield NDJSON audit rows from an initial SELECT plus follow polling.

    ``fetch_initial`` returns the bounded one-shot page in the route's
    existing order. For the current audit feeds that is newest-first, so
    the first initial row becomes the high-water cursor.

    ``fetch_next`` returns rows strictly newer than that cursor. Its rows
    must be ordered oldest-first so the cursor can advance to the final
    row after each poll without skipping rows that share a timestamp.
    """
    initial_rows = fetch_initial()
    yielded = False
    cursor: AuditTailCursor | None = None
    for chunk in ndjson_lines(project_row(row) for row in initial_rows):
        yielded = True
        yield chunk
    if initial_rows:
        cursor = cursor_for(initial_rows[0])

    if not follow:
        if not yielded and empty_keepalive:
            yield b"\n"
        return

    empty_polls = 0
    while True:
        rows = fetch_next(cursor)
        if rows:
            empty_polls = 0
            for chunk in ndjson_lines(project_row(row) for row in rows):
                yielded = True
                yield chunk
            cursor = cursor_for(rows[-1])
            continue

        if not yielded and empty_keepalive:
            yielded = True
            yield b"\n"
            continue

        if max_empty_polls is not None:
            empty_polls += 1
            if empty_polls > max_empty_polls:
                return
        sleep(poll_interval_seconds)
