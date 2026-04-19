"""Pilot API test.

Asserts ``app.api.v1`` imports cleanly. Real API contract tests
(schemathesis, auth matrix) land on top of this harness in cd-3j25
and follow-ups.

See ``docs/specs/17-testing-quality.md`` §"API contract".
"""

from __future__ import annotations


def test_api_v1_imports() -> None:
    import app.api.v1

    assert app.api.v1.__name__ == "app.api.v1"
