"""Installed-package version lookup with a caller-chosen fallback.

A handful of call sites — the ASGI factory's OpenAPI document, the
``/version`` ops handler — need the same one-liner: ask
:mod:`importlib.metadata` for the installed ``crewday`` version, and
fall back to a sentinel when running from a non-installed source
checkout (e.g. under pytest without ``pip install -e .``).

The fallback differs by call site:

* The factory wants a parseable PEP-440 string (``"0.0.0+unknown"``)
  because FastAPI stamps it onto the OpenAPI ``info.version`` field
  and downstream tooling parses that as a version.
* The ``/version`` handler wants a human-readable token (``"unknown"``)
  matching the other shape-completing fields it returns
  (``git_sha``, ``build_at``, ``image_digest``).

Both call sites pass their own sentinel rather than this module
choosing one.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Final

__all__ = ["resolve_package_version"]

# Matches ``pyproject.toml`` ``[project].name``. Kept here so a rename
# lands in one place across every caller of this helper.
_PACKAGE_NAME: Final[str] = "crewday"


def resolve_package_version(fallback: str) -> str:
    """Return the installed crewday package version, or ``fallback``.

    Falls back narrowly on :class:`PackageNotFoundError` only — any
    other :mod:`importlib.metadata` failure is a bug we want to see.
    """
    try:
        return _pkg_version(_PACKAGE_NAME)
    except PackageNotFoundError:
        return fallback
