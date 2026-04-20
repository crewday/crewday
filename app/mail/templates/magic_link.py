"""Magic-link email template.

One template covers every purpose (``signup_verify``,
``recover_passkey``, ``email_change_confirm``, ``grant_invite``) —
the caller hands in a ``purpose_label`` that reads naturally inside
the subject and body ("verify your email", "recover your account",
...). Keeping the variants in one file avoids a per-purpose
proliferation while the template surface is still minimal; swapping
in Jinja later is a whole-file rewrite, not a fan-out.

Placeholders:

* ``{purpose_label}`` — human-readable action ("verify your email").
* ``{url}`` — the ``https://<host>/auth/magic/<token>`` URL.
* ``{ttl_minutes}`` — integer-as-string TTL ("10", "15") the
  template echoes to the recipient.

See ``docs/specs/03-auth-and-tokens.md`` §"Magic link format",
§"Self-serve signup".
"""

from __future__ import annotations

__all__ = ["BODY_TEXT", "SUBJECT", "purpose_label"]


SUBJECT = "crew.day — {purpose_label}"


# Deliberately plain text. A matching HTML variant lands alongside
# the full Jinja-based template system; a one-off HTML string here
# would ossify the formatting and make the Jinja migration harder.
BODY_TEXT = """\
Hi,

To {purpose_label}, follow this link within the next {ttl_minutes}
minutes:

{url}

If you didn't request this, ignore this message — the link expires
on its own and leaves your account untouched.

— crew.day
"""


_PURPOSE_LABELS: dict[str, str] = {
    "signup_verify": "verify your email and finish signing up",
    "recover_passkey": "recover your account and enrol a new passkey",
    "email_change_confirm": "confirm your new email address",
    "grant_invite": "accept the invite to join a workspace",
}


def purpose_label(purpose: str) -> str:
    """Return the human-readable action text for ``purpose``.

    Unknown purposes fall back to a generic phrase rather than raising
    so a future purpose added without updating the label map still
    produces a sane email. Callers already validate ``purpose`` at
    the domain layer; a typo there lands elsewhere.
    """
    return _PURPOSE_LABELS.get(purpose, "complete your crew.day action")
