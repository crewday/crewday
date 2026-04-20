"""Recovery email template — existing account branch.

Sent by :func:`app.auth.recovery.request_recovery` when the submitted
email matches an existing :class:`~app.adapters.db.identity.models.User`.
Carries the magic link that walks the user through the
:func:`app.auth.passkey.register_start` ceremony, replacing every
existing passkey for the account.

Placeholders:

* ``{display_name}`` — the user's ``display_name`` (never the
  plaintext email — the recipient already knows their own address).
* ``{url}`` — the ``https://<host>/recover/enroll?token=<token>``
  URL the SPA lands the user on.
* ``{ttl_minutes}`` — integer-as-string TTL ("15") the template
  echoes so the reader knows the window.

The text deliberately calls out the destructive side-effect
(every existing passkey revoked, every other session signed out) so
the user understands they are using the "last-resort" door.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service lost-device
recovery".
"""

from __future__ import annotations

__all__ = ["BODY_TEXT", "SUBJECT"]


SUBJECT = "crew.day — recover your account"


# Plain text only — matches :mod:`app.mail.templates.magic_link`.
# Jinja + HTML variants land when the full template system arrives;
# a one-off string here would ossify formatting in the wrong place.
BODY_TEXT = """\
Hi {display_name},

You (or someone using your email) asked to recover access to your
crew.day account. To enrol a fresh passkey, open the link below
within the next {ttl_minutes} minutes:

{url}

Important: completing recovery revokes every existing passkey on
your account and signs you out of every other active session. Only
use this link on the device you want to keep.

If you didn't request this, ignore this message — the link expires
on its own and your account stays untouched.

— crew.day
"""
