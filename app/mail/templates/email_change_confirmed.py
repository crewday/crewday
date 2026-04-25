"""Self-service email-change — confirmation notice to the new address.

Sent by :func:`app.domain.identity.email_change.verify_change` to
the **new** address after the swap has landed. Confirms the change
took effect; carries no claimable link.

Per spec §03 "Self-service email change" "Confirmation" the verify
step also notifies the **old** address with the revert link
(template :mod:`app.mail.templates.email_change_revert`). This
template covers the new-address half — informational only.

Placeholders:

* ``{display_name}`` — the user's ``display_name``.
* ``{masked_old_email}`` — the previous address with the local
  part collapsed (e.g. ``j***@example.com``); echoes the address
  that lost authority so the recipient can correlate with their
  own intent.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service email change".
"""

from __future__ import annotations

__all__ = ["BODY_TEXT", "SUBJECT"]


SUBJECT = "crew.day — your email address was changed"


BODY_TEXT = """\
Hi {display_name},

Your crew.day email was just updated. From now on, every magic
link, recovery notice, and password-less sign-in pointer will land
in this inbox instead of {masked_old_email}.

If you didn't request this, contact a workspace manager
immediately — they can reset your passkey and your account access
on your behalf.

— crew.day
"""
