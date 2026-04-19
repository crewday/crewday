"""Workspace slug validation and normalisation.

Slugs appear in URLs (``/w/<slug>/...``) and must be unique across the
deployment. They are ASCII kebab-case with a strict pattern and a
reserved-word blocklist drawn from the routing surface.

See ``docs/specs/01-architecture.md`` §"Workspace addressing".
"""

from __future__ import annotations

import re

__all__ = [
    "RESERVED_SLUGS",
    "SLUG_PATTERN",
    "InvalidSlug",
    "is_reserved",
    "normalise_slug",
    "validate_slug",
]


# Total length: 3 .. 40 characters. First char: [a-z]. Last char: [a-z0-9].
# Interior: [a-z0-9-]. The consecutive-hyphen ban is enforced separately
# because the character class alone cannot express "no `--`".
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")


# Exact list from §01 "Workspace addressing". Extend only with a spec
# change — routing collisions here are silent bugs.
RESERVED_SLUGS: frozenset[str] = frozenset(
    {
        "w",
        "api",
        "admin",
        "signup",
        "login",
        "recover",
        "select-workspace",
        "healthz",
        "readyz",
        "version",
        "docs",
        "redoc",
        "styleguide",
        "unsupported",
        "static",
        "assets",
    }
)


class InvalidSlug(ValueError):
    """Raised when a slug fails validation.

    The message carries the specific reason (pattern / reserved /
    consecutive-hyphen) so callers can surface actionable errors.
    """


def is_reserved(slug: str) -> bool:
    """Return ``True`` if ``slug`` collides with a reserved URL segment."""
    return slug in RESERVED_SLUGS


def validate_slug(slug: str) -> None:
    """Validate ``slug`` against the pattern, reserved list, and hyphen rule.

    Raises :class:`InvalidSlug` with a message identifying the offending
    rule. Returns ``None`` on success.
    """
    if not isinstance(slug, str):  # defensive: callers may pass None/bytes
        raise InvalidSlug(f"slug must be str, got {type(slug).__name__}")
    if not SLUG_PATTERN.fullmatch(slug):
        raise InvalidSlug(
            f"slug {slug!r} does not match pattern {SLUG_PATTERN.pattern}"
        )
    # The regex allows `villa--sud` because `[a-z0-9-]` includes `-`.
    # The spec implies single-hyphen separators, so reject consecutive
    # hyphens explicitly.
    if "--" in slug:
        raise InvalidSlug(f"slug {slug!r} contains consecutive hyphens; use single `-`")
    if is_reserved(slug):
        raise InvalidSlug(f"slug {slug!r} is reserved")


def normalise_slug(raw: str) -> str:
    """Lowercase + strip ``raw`` and validate; return the canonical form.

    Raises :class:`InvalidSlug` if the normalised form fails
    :func:`validate_slug`.
    """
    if not isinstance(raw, str):
        raise InvalidSlug(f"slug must be str, got {type(raw).__name__}")
    normalised = raw.strip().lower()
    validate_slug(normalised)
    return normalised
