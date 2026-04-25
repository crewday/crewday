"""Self-service email-change — revert link to the **old** address.

Sent by :func:`app.domain.identity.email_change.verify_change` to
the address that just lost authority over the account. Carries the
72-hour revert magic link the spec §03 "Self-service email change"
"Revert window" pins — redemption rolls ``users.email`` back to the
previous value and burns the revert nonce.

The revert link is the only flow that consumes a magic link
against the old address after the swap; it is not an authentication
primitive, only an undo button.

Placeholders:

* ``{display_name}`` — the user's ``display_name``.
* ``{masked_new_email}`` — the address that just took over (e.g.
  ``a***@attacker.example``); the rightful owner needs this signal
  to decide whether to redeem the revert link.
* ``{url}`` — the ``https://<host>/auth/email/revert?token=<token>``
  URL the SPA / static page lands the recipient on. Single-use.
* ``{ttl_hours}`` — integer-as-string revert TTL ("72") echoed to
  the recipient so they know the deadline.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service email change".
"""

from __future__ import annotations

__all__ = ["BODY_TEXT", "SUBJECT"]


SUBJECT = "crew.day — revert the email change on your account"


BODY_TEXT = """\
Hi {display_name},

The email on your crew.day account was just changed away from this
address — it now points at {masked_new_email}.

If this was NOT you, you have {ttl_hours} hours to revert the
change by following the link below. Redemption restores this
address as your account email and signs you back in via passkey
recovery if you lose access:

{url}

If this was you, ignore this message — the link expires on its
own and the change stays.

— crew.day
"""
