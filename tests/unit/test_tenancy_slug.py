"""Tests for workspace slug validation and normalisation.

See docs/specs/01-architecture.md §"Workspace addressing".
"""

from __future__ import annotations

import pytest

from app.tenancy.slug import (
    RESERVED_SLUGS,
    SLUG_PATTERN,
    InvalidSlug,
    is_reserved,
    normalise_slug,
    validate_slug,
)

# ---------------------------------------------------------------------------
# Happy path — values that must pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    [
        "abc",  # minimum length (3) — start alpha, end alnum
        "a1b",  # digit in interior
        "villa-sud",  # typical kebab
        "villa-sud-nord",  # two separators
        "villa1-sud2",  # digits + hyphen
        "a" + "b" * 38 + "c",  # 40 chars — maximum allowed
    ],
)
def test_validate_slug_accepts_valid_slugs(slug: str) -> None:
    validate_slug(slug)  # must not raise


def test_pattern_matches_accepted_slugs() -> None:
    # Sanity-check the regex itself.
    assert SLUG_PATTERN.fullmatch("villa-sud") is not None
    assert SLUG_PATTERN.fullmatch("abc") is not None


# ---------------------------------------------------------------------------
# Rejected slugs — each with a specific reason surfaced in the message
# ---------------------------------------------------------------------------


def test_rejects_reserved_short_slug_w() -> None:
    # 'w' is both too short (<3) AND reserved. Pattern fails first and
    # that's what we assert on — the point is that it doesn't slip
    # through.
    with pytest.raises(InvalidSlug) as excinfo:
        validate_slug("w")
    assert "pattern" in str(excinfo.value).lower()


def test_rejects_uppercase() -> None:
    with pytest.raises(InvalidSlug, match="pattern"):
        validate_slug("API")


def test_rejects_bad_leading_char_underscore() -> None:
    with pytest.raises(InvalidSlug, match="pattern"):
        validate_slug("_foo")


def test_rejects_bad_leading_char_digit() -> None:
    with pytest.raises(InvalidSlug, match="pattern"):
        validate_slug("1abc")


def test_rejects_bad_leading_char_hyphen() -> None:
    with pytest.raises(InvalidSlug, match="pattern"):
        validate_slug("-abc")


def test_rejects_trailing_hyphen() -> None:
    with pytest.raises(InvalidSlug, match="pattern"):
        validate_slug("villa-")


def test_rejects_consecutive_hyphens_with_specific_reason() -> None:
    # The regex allows `villa--sud` because `-` is in the character
    # class; the explicit rule is what catches it. The message must
    # name the rule so users understand *why* it fails.
    with pytest.raises(InvalidSlug, match="consecutive hyphens"):
        validate_slug("villa--sud")


def test_rejects_too_long() -> None:
    too_long = "a" + "b" * 39 + "c"  # 41 chars
    assert len(too_long) == 41
    with pytest.raises(InvalidSlug, match="pattern"):
        validate_slug(too_long)


def test_rejects_too_short() -> None:
    with pytest.raises(InvalidSlug, match="pattern"):
        validate_slug("ab")


def test_rejects_non_ascii() -> None:
    with pytest.raises(InvalidSlug, match="pattern"):
        validate_slug("villa-sûd")


@pytest.mark.parametrize(
    "slug",
    ["admin", "signup", "healthz", "static", "assets", "api", "login"],
)
def test_rejects_reserved_slugs(slug: str) -> None:
    # The slug passes the pattern (it's valid kebab) but is reserved.
    with pytest.raises(InvalidSlug, match="reserved"):
        validate_slug(slug)


# ---------------------------------------------------------------------------
# is_reserved
# ---------------------------------------------------------------------------


def test_is_reserved_true_for_known_reserved() -> None:
    assert is_reserved("admin") is True
    assert is_reserved("select-workspace") is True


def test_is_reserved_false_for_regular_slug() -> None:
    assert is_reserved("villa-sud") is False


def test_reserved_slugs_contains_full_spec_list() -> None:
    # Lock the exact list from §01 so a silent edit surfaces here.
    expected = {
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
    assert expected == RESERVED_SLUGS


# ---------------------------------------------------------------------------
# normalise_slug
# ---------------------------------------------------------------------------


def test_normalise_slug_strips_and_lowercases() -> None:
    assert normalise_slug("  VILLA-SUD\n") == "villa-sud"


def test_normalise_slug_rejects_consecutive_hyphens_after_lowering() -> None:
    with pytest.raises(InvalidSlug, match="consecutive hyphens"):
        normalise_slug("VILLA--SUD")


def test_normalise_slug_rejects_reserved_after_lowering() -> None:
    with pytest.raises(InvalidSlug, match="reserved"):
        normalise_slug("  ADMIN  ")


def test_normalise_slug_rejects_bad_pattern_after_lowering() -> None:
    with pytest.raises(InvalidSlug, match="pattern"):
        normalise_slug("  _FOO  ")


def test_normalise_slug_rejects_empty_string() -> None:
    # Empty string strips to empty; pattern rejects it.
    with pytest.raises(InvalidSlug, match="pattern"):
        normalise_slug("")


def test_normalise_slug_rejects_whitespace_only() -> None:
    with pytest.raises(InvalidSlug, match="pattern"):
        normalise_slug("   \n\t ")


# ---------------------------------------------------------------------------
# Defensive non-str inputs — the validator must surface InvalidSlug, not a
# bare TypeError, so callers can treat "bad slug" uniformly.
# ---------------------------------------------------------------------------


def test_validate_slug_rejects_non_str_none() -> None:
    with pytest.raises(InvalidSlug, match="must be str"):
        validate_slug(None)  # type: ignore[arg-type]


def test_normalise_slug_rejects_non_str_bytes() -> None:
    with pytest.raises(InvalidSlug, match="must be str"):
        normalise_slug(b"villa-sud")  # type: ignore[arg-type]
