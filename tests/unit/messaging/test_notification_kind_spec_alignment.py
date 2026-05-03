"""Coherence test: §10.1 spec table ↔ ``NotificationKind`` enum (cd-xpiz).

The §10 spec is the user-facing taxonomy and ``NotificationKind`` is
the developer-facing one. Per cd-xpiz Option B the spec is rewritten to
NAME the canonical enum: §10.1 "Routed via NotificationService" carries
one row per enum value, keyed by snake_case kind. This test parses that
table and asserts the set of kinds matches the enum exactly so the two
cannot drift silently.

A drifted state is one of:

* enum gained a value but §10.1 was not updated → a new notification
  kind ships without a documented user-facing description / recipient.
* §10.1 gained a row but the enum was not widened → the spec advertises
  a kind no service can emit.

When the test fails, fix the side that is wrong:

* If the enum is the source of truth, add the matching row to §10.1
  (and to §10.2 / §10.3 / §10.4 if it routes elsewhere).
* If the spec row reflects a feature that has actually shipped, widen
  the enum + the DB CHECK + add the templates per the
  ``NotificationKind`` docstring.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.domain.messaging.notifications import NotificationKind

_SPEC_PATH = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "specs"
    / "10-messaging-notifications.md"
)

# Anchor on the §10.1 heading and stop at the next ``####`` so a future
# §10.2 row carrying a backtick token cannot be misread as an enum kind.
_SECTION_HEADING = "#### §10.1 Routed via NotificationService"
_NEXT_SECTION_PREFIX = "#### "

# Capture the leading backtick-quoted token in each table row. The §10.1
# table's first column is ``| `kind_value` |`` — every row that names
# an enum value matches; the header row (``| kind value …``) and the
# separator row (``|---|---|``) do not.
_ROW_RE = re.compile(r"^\|\s*`([a-z_]+)`\s*\|", re.MULTILINE)


def _read_spec_section() -> str:
    text = _SPEC_PATH.read_text(encoding="utf-8")
    start = text.find(_SECTION_HEADING)
    assert start != -1, (
        f"§10.1 heading not found in {_SPEC_PATH} — did the heading "
        f"text change? Expected {_SECTION_HEADING!r}."
    )
    after_heading = text.find("\n", start) + 1
    end = text.find(_NEXT_SECTION_PREFIX, after_heading)
    if end == -1:
        end = len(text)
    return text[after_heading:end]


def test_spec_section_lists_every_enum_value() -> None:
    """§10.1 names exactly the kinds the ``NotificationKind`` enum lists.

    Set equality so the failure message names BOTH sides of the drift.
    """
    section = _read_spec_section()
    spec_kinds = frozenset(_ROW_RE.findall(section))
    enum_kinds = frozenset(k.value for k in NotificationKind)

    assert spec_kinds == enum_kinds, (
        f"§10.1 ↔ NotificationKind drift: "
        f"spec-only={sorted(spec_kinds - enum_kinds)}, "
        f"enum-only={sorted(enum_kinds - spec_kinds)}."
    )


def test_spec_section_is_non_empty() -> None:
    """Guard against the regex silently capturing nothing.

    A future edit that breaks the table format (e.g. swaps backticks
    for italics) would leave ``spec_kinds`` empty and accidentally
    pass the equality check only when the enum is also empty. This
    test pins the lower bound — the enum has at least one value, so
    the spec must too.
    """
    section = _read_spec_section()
    spec_kinds = frozenset(_ROW_RE.findall(section))
    assert spec_kinds, (
        "§10.1 table parsed to zero rows — the row pattern "
        f"({_ROW_RE.pattern!r}) likely no longer matches the spec "
        "table format. Check that the first column is still "
        "``| `<kind>` |``."
    )
