"""ISO-8601 serialization helpers for date/time values.

Dedupes the optional-stringify idiom that recurs across audit and API
payload builders. Byte-identical to the inline expression:
:func:`iso_or_none` emits exactly ``value.isoformat()`` or ``None``, and
:func:`iso` emits ``value.isoformat()``. These do no timezone
normalisation — a caller that also needs a ``+00:00``/``Z`` rewrite must
keep its own helper.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["SupportsIsoformat", "iso", "iso_or_none"]


@runtime_checkable
class SupportsIsoformat(Protocol):
    """Anything that stringifies via a no-arg ``isoformat()``.

    Satisfied by :class:`datetime.datetime`, :class:`datetime.date`, and
    :class:`datetime.time`.
    """

    def isoformat(self) -> str: ...


def iso(value: SupportsIsoformat) -> str:
    """Return the ISO-8601 string for a required date/time value."""
    return value.isoformat()


def iso_or_none(value: SupportsIsoformat | None) -> str | None:
    """Return ``value.isoformat()``, or ``None`` when ``value`` is ``None``."""
    if value is None:
        return None
    return value.isoformat()
