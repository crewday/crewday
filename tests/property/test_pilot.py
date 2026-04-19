"""Pilot property-based test.

Confirms :mod:`hypothesis` is wired into the suite. Real property
tests for money math, RRULE generation, and policy resolution land
in follow-up tasks (``docs/specs/17-testing-quality.md`` §"Unit").
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st


@given(st.integers())
def test_zero_is_additive_identity(x: int) -> None:
    assert x + 0 == x
