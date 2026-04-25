"""Boundary tests for the JWT credential regex in :mod:`app.util.redact`.

The regex previously accepted any three dot-separated alphanumeric
segments, so legitimate structured-logging event names like
``worker.tick.start`` and ``idempotency.sweep`` were rewritten to
``<redacted:credential>`` before downstream consumers (log
assertions, observability) could see them — see Beads ``cd-pzr1``.

The fix raises the per-segment floor to 16 characters. Real
RFC 7519 JWT segments are comfortably above that (header ≈ 30+,
payload ≈ 50+, signature ≥ 43 for HS256), while every realistic
dotted identifier — event names, module paths, OpenTelemetry span
attribute keys — is well below it.

These tests pin the boundary so future regex changes can't silently
re-introduce the false-positive class.

See ``docs/specs/15-security-privacy.md`` §"Logging and redaction".
"""

from __future__ import annotations

import pytest

from app.util.redact import redact, scrub_string

_TAG_CREDENTIAL = "<redacted:credential>"


# A canonical RFC 7519 JWT: 36-char header, 51-char payload, 43-char
# signature. Used to verify that real-shape tokens still get scrubbed
# under the tightened regex.
REAL_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)


# ---------------------------------------------------------------------------
# Real JWTs still get redacted
# ---------------------------------------------------------------------------


class TestRealJwtRedacted:
    def test_canonical_rfc7519_jwt_redacted(self) -> None:
        out = redact(f"auth header value {REAL_JWT} more text", scope="log")
        assert REAL_JWT not in out
        assert _TAG_CREDENTIAL in out

    def test_jwt_inside_dict_value(self) -> None:
        out = redact({"raw": f"got token {REAL_JWT} here"}, scope="log")
        assert isinstance(out, dict)
        raw = out["raw"]
        assert isinstance(raw, str)
        assert REAL_JWT not in raw
        assert _TAG_CREDENTIAL in raw

    def test_scrub_string_redacts_real_jwt(self) -> None:
        # The logging filter goes through ``scrub_string`` directly,
        # so cover that seam too.
        out = scrub_string(f"prefix {REAL_JWT} suffix")
        assert REAL_JWT not in out
        assert _TAG_CREDENTIAL in out


# ---------------------------------------------------------------------------
# Dotted event names survive
# ---------------------------------------------------------------------------


class TestDottedEventNamesPreserved:
    """Structured-log event names with three dot-separated segments
    used to be eaten by the old regex. The 16-char-per-segment floor
    keeps them intact.

    Each example here is a name actually used in the codebase or a
    naming pattern explicitly documented in the specs.
    """

    @pytest.mark.parametrize(
        "event_name",
        [
            "worker.tick.start",
            "worker.tick.end",
            "idempotency.sweep",
            "app.expense.created",
            "task.completed",
            "app.worker.scheduler",
            "magic.link.sent",
            "session.cookie.rotated",
            "audit.row.written",
        ],
    )
    def test_event_name_alone(self, event_name: str) -> None:
        # ``scrub_string`` is the lowest layer; if the event survives
        # here it survives every higher-level call too.
        assert scrub_string(event_name) == event_name

    @pytest.mark.parametrize(
        "event_name",
        [
            "worker.tick.start",
            "worker.tick.end",
            "idempotency.sweep",
            "app.expense.created",
        ],
    )
    def test_event_name_in_message(self, event_name: str) -> None:
        msg = f"emitted event={event_name} for tenant=42"
        out = redact(msg, scope="log")
        assert event_name in out
        assert _TAG_CREDENTIAL not in out

    def test_short_faux_jwt_preserved(self) -> None:
        # ``a.b.c`` is the canonical "smallest possible JWT shape".
        # Single-char segments are nowhere near a real token; they
        # must stay intact.
        assert scrub_string("a.b.c") == "a.b.c"
        assert redact("hello a.b.c world", scope="log") == "hello a.b.c world"


# ---------------------------------------------------------------------------
# Per-segment length boundary
# ---------------------------------------------------------------------------


class TestSegmentLengthBoundary:
    """Pin the 16-char floor on every segment.

    Just below the floor — a 15-char run — must NOT trigger the JWT
    pattern (otherwise the false-positive class returns). At the floor
    or above — 16+ chars — three such segments form a credential
    shape and MUST be scrubbed.
    """

    def test_fifteen_char_segments_not_redacted(self) -> None:
        # Three 15-char segments — one below the floor.
        below = "abcdefghijklmno.abcdefghijklmno.abcdefghijklmno"
        assert scrub_string(below) == below
        assert _TAG_CREDENTIAL not in redact(f"x {below} y", scope="log")

    def test_sixteen_char_segments_redacted(self) -> None:
        # Three 16-char segments — exactly at the floor.
        at_floor = "abcdefghijklmnop.abcdefghijklmnop.abcdefghijklmnop"
        out = scrub_string(at_floor)
        assert at_floor not in out
        assert _TAG_CREDENTIAL in out

    def test_long_short_long_disqualifies_match(self) -> None:
        # Two long bookends with a 15-char short middle segment: every
        # segment must clear the floor for the JWT rule to fire, so
        # this shape must survive.
        #
        # Each bookend is capped at 30 chars — below the 40-char
        # base64url floor — so the *only* rule that could possibly
        # fire on this string is the JWT rule. That makes the
        # ``scrub_string(...) == ...`` equality assertion a clean
        # contract on JWT behaviour alone.
        long_short_long = (
            "abcdefghijklmnopqrstuvwxyz1234"  # 30 chars
            ".abcdefghijklmno"  # 15 chars — below floor
            ".ABCDEFGHIJKLMNOPQRSTUVWXYZ5678"  # 30 chars (distinct)
        )
        assert scrub_string(long_short_long) == long_short_long

    def test_short_long_short_disqualifies_match(self) -> None:
        # Inverse shape: a single long middle segment between two
        # short bookends — the false-positive surface from
        # ``cd-pzr1`` (e.g. ``worker.aaaaaaaaaaaaaaaa.end``). All
        # three segments must clear the 16-char floor, so the JWT
        # rule must NOT fire on this shape either.
        #
        # Two literal forms because the bug report specifically called
        # out both the abstract ``a.bbbbbbbbbbbbbbbb.c`` shape and the
        # concrete ``worker.aaaaaaaaaaaaaaaa.end`` event-name shape.
        for short_long_short in (
            "a.bbbbbbbbbbbbbbbb.c",  # 1.16.1 — single-char bookends
            "worker.aaaaaaaaaaaaaaaa.end",  # 6.16.3 — event-name shape
            "id.cccccccccccccccccccccccc.v",  # 2.24.1 — 24-char middle
        ):
            assert scrub_string(short_long_short) == short_long_short
            out = redact(f"prefix {short_long_short} suffix", scope="log")
            assert _TAG_CREDENTIAL not in out
            assert short_long_short in out


# ---------------------------------------------------------------------------
# Mixed real-world payload
# ---------------------------------------------------------------------------


class TestMixedPayload:
    def test_jwt_and_event_name_together(self) -> None:
        """A paragraph carrying a real JWT alongside an event name —
        the JWT must go, the event name must stay. This is the
        regression scenario from ``cd-pzr1`` (the integration sweep
        test asserts on ``event="worker.tick.end"``).
        """
        msg = (
            f"event=worker.tick.end ok=True bearer_header={REAL_JWT} "
            "job_id=idempotency.sweep deleted=1"
        )
        out = redact(msg, scope="log")
        assert REAL_JWT not in out
        assert _TAG_CREDENTIAL in out
        # Event-name surface is preserved character-for-character so
        # downstream log assertions match.
        assert "worker.tick.end" in out
        assert "idempotency.sweep" in out
