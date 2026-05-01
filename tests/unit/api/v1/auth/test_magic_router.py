"""Router-level tests for :mod:`app.api.v1.auth.magic`.

Currently narrow: pins the ``min_length=1, max_length=4096``
defence-in-depth bounds on :class:`MagicConsumeBody.token` (cd-jt0v).
itsdangerous fails fast on garbage tokens (~1 ms for 1 MB), so these
constraints are belt-and-braces — moving the rejection of empty and
pathologically large bodies into Pydantic where it costs nothing
rather than walking the domain layer. The bounds match the
convention the other itsdangerous-signed token bodies in the app
already advertise (``EmailVerifyBody``, ``EmailRevertBody``,
``RegisterMobilePushBody``).

The end-to-end consume happy/error paths are covered by
:mod:`tests.integration.auth.test_magic_link_mailpit`; this file owns
only the schema-level surface so the cd-jt0v acceptance criterion maps
to a dedicated, fast-running unit.

See ``docs/specs/03-auth-and-tokens.md`` §"Magic link format".
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1.auth.magic import MagicConsumeBody


class TestMagicConsumeBodyTokenLength:
    """``token`` rejects empty and oversize bodies before the service runs."""

    def test_typical_token_accepted(self) -> None:
        """A real magic-link token round-trips at ~150 chars; the bound is roomy."""
        body = MagicConsumeBody(token="x" * 200, purpose="signup_verify")
        assert body.token == "x" * 200

    def test_max_length_4096_accepts_boundary(self) -> None:
        """Exactly 4096 characters is still valid (inclusive upper bound)."""
        body = MagicConsumeBody(token="x" * 4096, purpose="signup_verify")
        assert len(body.token) == 4096

    def test_token_above_4096_raises_validation_error(self) -> None:
        """4097 chars trips Pydantic before the magic-link service is called.

        cd-jt0v: defence-in-depth so a multi-MB nuisance body never
        reaches :func:`app.auth.magic_link.consume_link`. Pydantic's
        ``string_too_long`` is what FastAPI renders as ``422`` upstream.
        """
        with pytest.raises(ValidationError) as excinfo:
            MagicConsumeBody(token="x" * 4097, purpose="signup_verify")
        # Pin the offending field + the error category so a future
        # rename of the constraint surfaces here, not at runtime.
        errors = excinfo.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("token",)
        assert errors[0]["type"] == "string_too_long"

    def test_empty_token_raises_validation_error(self) -> None:
        """An empty string trips Pydantic — itsdangerous would always reject it
        at the domain layer; catching it here costs nothing.
        """
        with pytest.raises(ValidationError) as excinfo:
            MagicConsumeBody(token="", purpose="signup_verify")
        errors = excinfo.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("token",)
        assert errors[0]["type"] == "string_too_short"
