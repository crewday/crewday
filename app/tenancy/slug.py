"""Workspace slug validation, normalisation, and homoglyph guard.

Slugs appear in URLs (``/w/<slug>/...``) and must be unique across the
deployment. They are ASCII kebab-case with a strict pattern and a
reserved-word blocklist drawn from the routing surface.

The canonical reserved-list lives in :data:`RESERVED_SLUGS` below and
**must stay in sync** with the reverse-proxy routing table. The spec
(``docs/specs/03-auth-and-tokens.md`` §"Self-serve signup" →
"Reserved slugs") is the authoritative source: any future route
reserved at the bare host MUST land here first.

**Homoglyph guard.** :func:`normalise_for_collision` folds a candidate
slug down to a collision key (ASCII-fold + Punycode-normalise +
digit-substitute ``0→o``, ``1→l``, plus the deployment heuristics
``5→s`` and ``rn→m`` that go beyond §03's explicit ``0→o, 1→l``).
Two slugs that share a collision key look typographically similar and
the signup flow rejects the newcomer — see spec §03 "Homoglyph guard".

See ``docs/specs/01-architecture.md`` §"Workspace addressing" and
``docs/specs/03-auth-and-tokens.md`` §"Self-serve signup".
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

__all__ = [
    "RESERVED_SLUGS",
    "SLUG_PATTERN",
    "InvalidSlug",
    "is_homoglyph_collision",
    "is_reserved",
    "normalise_for_collision",
    "normalise_slug",
    "validate_slug",
]


# Total length: 3 .. 40 characters. First char: [a-z]. Last char: [a-z0-9].
# Interior: [a-z0-9-]. The consecutive-hyphen ban is enforced separately
# because the character class alone cannot express "no `--`".
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")


# Full spec reserved list (``docs/specs/03-auth-and-tokens.md``
# §"Self-serve signup" → "Reserved slugs"). This is the operational
# superset of §02's blocklist; extend only with a spec change — silent
# routing collisions here are hard bugs.
RESERVED_SLUGS: frozenset[str] = frozenset(
    {
        "admin",
        "api",
        "app",
        "assets",
        "auth",
        "demo",
        "docs",
        "events",
        "guest",
        "healthz",
        "login",
        "logout",
        "public",
        "readyz",
        "recover",
        "redoc",
        "select-workspace",
        "signup",
        "static",
        "status",
        "styleguide",
        "support",
        "unsupported",
        "version",
        "w",
        "webhooks",
        "ws",
        "www",
    }
)


# Homoglyph collision fold. The mapping catches common ASCII look-alikes
# that bypass the §02 regex. Kept tiny and deliberate: ``0→o`` and
# ``1→l`` are the pairs §03 spec explicitly flags; ``5→s`` is a
# deployment heuristic going beyond the spec (same visual failure mode,
# same defensive intent). Expanding this table without a spec bump
# silently rejects previously-valid slugs — don't.
_DIGIT_SUBSTITUTIONS: tuple[tuple[str, str], ...] = (
    ("0", "o"),
    ("1", "l"),
    ("5", "s"),
)

# ASCII pair substitutions applied *after* digit folding. ``rn`` → ``m``
# is a deployment heuristic beyond §03's explicit ``0→o, 1→l`` list —
# the spec's worked example (``rnicasa`` vs ``micasa``) motivates it,
# but the spec itself only pins the digit pairs. Pair-level rules
# happen after single-char digit folds so ``m1cro`` → ``mlcro`` stays
# stable.
_PAIR_SUBSTITUTIONS: tuple[tuple[str, str], ...] = (
    ("rn", "m"),
    # ``vv`` and ``w`` look similar but ``w`` is reserved and an
    # ``vv``-prefixed slug still has to pass :func:`validate_slug`
    # before it gets to the collision check, so leaving this out
    # avoids a false reject on ``vvvillas``.
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


def normalise_for_collision(slug: str) -> str:
    """Return the canonical **collision key** for ``slug``.

    The fold is deliberately defensive — the §02 regex restricts the
    character set, but the fold catches typographic look-alikes:

    1. Unicode NFKD decomposition strips combining accents (``ñ`` →
       ``n``, ``é`` → ``e``).
    2. Any residual non-ASCII byte is dropped. A future migration may
       accept IDNA-encoded slugs; when that lands, this step becomes
       "re-encode via Punycode" instead of "drop". For now the slug
       regex already forbids non-ASCII so the drop is a safety net.
    3. Case-fold to lower (``strip`` too, for defensive callers).
    4. Digit substitutions: ``0→o`` and ``1→l`` come from §03 spec;
       ``5→s`` is a deployment heuristic going beyond §03.
    5. Pair substitutions applied last — ``rn→m`` is another
       deployment heuristic beyond §03 (motivated by the spec's
       worked example, but not spelled out in its explicit list).

    Two slugs whose collision keys are equal are deemed typographically
    similar; :func:`is_homoglyph_collision` uses this to reject a
    newcomer whose key collides with an existing active slug.

    Empty input returns the empty string — the caller decides whether
    that counts as a collision (generally it does not, because an empty
    slug is already rejected by :func:`validate_slug`).
    """
    if not isinstance(slug, str):
        raise InvalidSlug(f"slug must be str, got {type(slug).__name__}")

    # Strip accents via NFKD then drop remaining combining marks.
    decomposed = unicodedata.normalize("NFKD", slug)
    ascii_only = "".join(
        ch for ch in decomposed if not unicodedata.combining(ch) and ord(ch) < 128
    )
    folded = ascii_only.strip().lower()

    # Digit substitutions first, then pair-level ASCII substitutions.
    for digit, letter in _DIGIT_SUBSTITUTIONS:
        folded = folded.replace(digit, letter)
    for pair, replacement in _PAIR_SUBSTITUTIONS:
        folded = folded.replace(pair, replacement)
    return folded


def is_homoglyph_collision(candidate: str, existing_slugs: Iterable[str]) -> str | None:
    """Return the colliding existing slug, or ``None`` if none collide.

    Pure function — does not touch the DB. The caller supplies
    ``existing_slugs`` (typically the ``workspace.slug`` values that
    are currently active).

    Exact-string matches are **skipped** here: an exact slug match is
    a ``slug_taken`` error (handled separately); this check is for the
    typographic look-alike family. Returning the first matching
    existing slug is stable so the error body can point the user at
    ``{"colliding_slug": <existing>}``.

    ``candidate`` is folded once; existing slugs are folded lazily on
    iteration so callers passing a generator pay only until a match.
    """
    candidate_key = normalise_for_collision(candidate)
    if not candidate_key:
        return None
    for existing in existing_slugs:
        if existing == candidate:
            # An exact match is the "slug_taken" path, not a homoglyph
            # collision — the caller handles it separately.
            continue
        if normalise_for_collision(existing) == candidate_key:
            return existing
    return None
