"""Self-service email-change — informational notice to the old address.

Sent by :func:`app.domain.identity.email_change.request_change` to
the **old** address at the moment a passkey-session caller asks to
swap their email. Carries no claimable link — the magic link goes
to the new address only (§03 "Self-service email change") so an
attacker who hijacked the session via a stolen recovery link cannot
grab the rightful owner's mailbox by also reading the new-address
inbox they typed into the form.

Placeholders:

* ``{display_name}`` — the user's ``display_name`` (the recipient
  already knows their own address so we don't echo it back).
* ``{masked_new_email}`` — the new address with the local part
  collapsed to one character + ``***`` (e.g. ``j***@example.com``).
  Mirrors the §03 "Owner-initiated worker passkey reset" notice
  copy: enough signal for the rightful owner to recognise the
  attempt without surfacing the full address mid-attack.
* ``{ip_prefix}`` — caller IP truncated to ``/24`` (IPv4) or
  ``/64`` (IPv6) per §15 "PII-minimisation" — gives the recipient
  geographic provenance without persisting the full address.
* ``{ttl_minutes}`` — integer-as-string TTL ("15") of the new-
  address magic link so the recipient knows the window during
  which the swap is still abortable by inaction.

The text deliberately calls out the recovery path ("contact your
manager") rather than offering an inline action — the old-address
recipient may have lost session access entirely, and the manager-
mediated path is the spec's "kill the request before it lands"
seam.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service email change"
and ``docs/specs/15-security-privacy.md`` §"Self-service lost-device
& email-change abuse mitigations".
"""

from __future__ import annotations

__all__ = ["BODY_TEXT", "SUBJECT"]


SUBJECT = "crew.day — email change requested on your account"


# Plain text only — matches every other template in this package.
# The recipient cannot accidentally consume anything from this body;
# it carries information, not action.
BODY_TEXT = """\
Hi {display_name},

Someone requested changing the email on your crew.day account to
{masked_new_email}. The request came from a session signed in
from IP {ip_prefix}.

If this was you, you can ignore this notice — clicking the link in
the email we sent to your new address within the next {ttl_minutes}
minutes will complete the change.

If this was NOT you, do nothing and the request will lapse on its
own. If you've lost access to your account, contact a workspace
manager — they can reset your passkey on your behalf.

— crew.day
"""
