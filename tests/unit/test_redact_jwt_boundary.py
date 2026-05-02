"""Boundary tests for the JWT credential regex in :mod:`app.util.redact`.

The regex previously accepted any three dot-separated alphanumeric
segments, so legitimate structured-logging event names like
``worker.tick.start`` and ``ops.readyz.degraded`` were rewritten to
``<redacted:credential>`` before downstream consumers (log
assertions, observability) could see them — see Beads ``cd-pzr1``
and the follow-up ``cd-udts``.

The fix uses an **alternation form**: at least one of the three
segments must be ≥ 10 characters for the JWT rule to fire. Real
RFC 7519 JWTs always satisfy that floor on at least one segment
(payload ≈ 50+, signature ≥ 43 for HS256), while the bulk of
operator-emitted dotted event markers (``worker.tick.start``,
``ops.readyz.degraded``, ``a.b.c``) keep every segment well under
10 chars and therefore survive the regex untouched.

The alternation form deliberately allows shorter segments on two
of the three positions because real-world JWT headers can be
short (a stripped-down ``{"typ":"JWT"}`` is fewer than 10 chars
once base64url-encoded), so requiring ≥ 10 on EVERY segment
would under-redact. See the comment block at ``_JWT_RE`` for the
full rationale.

These tests pin the boundary so future regex changes can't silently
re-introduce the false-positive class on plain dotted identifiers,
or under-redact short-header JWTs on the way back the other way.

See ``docs/specs/15-security-privacy.md`` §"Logging and redaction".
"""

from __future__ import annotations

import pytest

from app.util.redact import redact, scrub_string

_TAG_CREDENTIAL = "<redacted:credential>"


# A canonical RFC 7519 JWT: 36-char header, 51-char payload, 43-char
# signature. Used to verify that real-shape tokens still get scrubbed.
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

    def test_short_header_jwt_still_redacted(self) -> None:
        # Header below 10 chars but payload + signature comfortably
        # above. The alternation form catches this; a uniform
        # ``{10,}\.{10,}\.{10,}`` would have under-redacted.
        token = (
            "eyJhbGci"  # 8 chars — below floor
            ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"  # 27 chars
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"  # 43 chars
        )
        out = scrub_string(f"x {token} y")
        assert token not in out
        assert _TAG_CREDENTIAL in out


# ---------------------------------------------------------------------------
# Dotted event names survive
# ---------------------------------------------------------------------------


class TestDottedEventNamesPreserved:
    """Structured-log event names with three dot-separated segments
    used to be eaten by the old regex. The new alternation form
    keeps any name whose segments are all under 10 chars intact.

    Each example here is a name actually used in the codebase or a
    naming pattern explicitly documented in the specs. Markers with
    a long segment (``idempotency.sweep.tick``,
    ``chat_gateway.sweep.tick``) are NOT covered here — those rely
    on the ``event=`` extra-key exemption in
    :class:`app.util.logging.RedactionFilter` to survive, not on
    the regex itself. See ``cd-udts`` for the trade-off.
    """

    @pytest.mark.parametrize(
        "event_name",
        [
            "worker.tick.start",
            "worker.tick.end",
            "worker.tick.error",
            "worker.scheduler.started",
            "worker.scheduler.stopped",
            "ops.readyz.degraded",
            "ops.readyz.db_error",
            "task.completed",
            "magic.link.sent",
            "audit.row.written",
            "session.cookie.rotate",
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
            "ops.readyz.degraded",
            "worker.scheduler.started",
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
    """Pin the alternation-form 10-char floor.

    With three segments each of length < 10 the JWT rule must NOT
    fire — that is the false-positive surface from ``cd-pzr1`` and
    ``cd-udts``. As soon as ANY segment hits 10 chars the shape
    becomes credential-shaped and the rule fires. This asymmetry
    is deliberate so short-header real JWTs are still caught.
    """

    def test_three_nine_char_segments_not_redacted(self) -> None:
        # Every segment 9 chars — all below the 10-char floor.
        below = "abcdefghi.jklmnopqr.stuvwxyz1"
        assert scrub_string(below) == below
        assert _TAG_CREDENTIAL not in redact(f"x {below} y", scope="log")

    def test_one_ten_char_segment_is_redacted(self) -> None:
        # Single 10-char segment among shorter siblings: any one
        # alternative branch fires, so the whole shape is scrubbed.
        # This is the trade-off the alternation form deliberately
        # accepts so short-header JWTs remain caught.
        for shape in (
            "abcdefghij.b.c",  # ≥10 first
            "a.bcdefghijk.c",  # ≥10 middle
            "a.b.cdefghijkl",  # ≥10 last
        ):
            out = scrub_string(shape)
            assert shape not in out
            assert _TAG_CREDENTIAL in out

    def test_short_long_short_with_long_middle_redacts(self) -> None:
        # A single long middle segment between short bookends —
        # the old regex required EVERY segment ≥ 16 chars and
        # therefore left this alone, but the new alternation form
        # fires. Markers in this shape (e.g. anything with one
        # ≥10-char component) must rely on the ``event`` extra-key
        # exemption in the logging filter to survive, not on the
        # regex itself.
        shape = "a.bbbbbbbbbbbbbbbb.c"  # 1.16.1
        out = scrub_string(shape)
        assert _TAG_CREDENTIAL in out


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
            "job_id=worker.scheduler.started deleted=1"
        )
        out = redact(msg, scope="log")
        assert REAL_JWT not in out
        assert _TAG_CREDENTIAL in out
        # Event-name surfaces (all segments < 10 chars) are preserved
        # character-for-character so downstream log assertions match.
        assert "worker.tick.end" in out
        assert "worker.scheduler.started" in out
