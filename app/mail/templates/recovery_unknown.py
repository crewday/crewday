"""Recovery email template — unknown account branch.

Sent by :func:`app.auth.recovery.request_recovery` when the submitted
email does **not** match any :class:`~app.adapters.db.identity.models.User`.
Spec §15 "Self-service lost-device & email-change abuse mitigations"
requires constant-time responses — the server must not let a hostile
caller learn which emails are registered by diffing response latency
or mail cadence. A no-link "we didn't find an account" message keeps
the mail cadence identical between the hit and miss branches while
giving a legitimate owner who typo'd their address a useful signal.

Placeholders: none. This template is intentionally parameter-free so
the caller pays the same render cost (``str.format_map`` on a flat
string) as the existing-account template without re-introducing the
user's address — which, on the miss branch, we have no reason to
echo back (the recipient already knows what they typed).

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service lost-device
recovery" and ``docs/specs/15-security-privacy.md`` §"Self-service
lost-device & email-change abuse mitigations".
"""

from __future__ import annotations

__all__ = ["BODY_TEXT", "SUBJECT"]


SUBJECT = "crew.day — recovery request"


# Plain text only — matches the existing-account template so both
# branches share the same cadence + content shape on the wire.
BODY_TEXT = """\
Hi,

Someone — possibly you — asked to recover a crew.day account
registered to this email address. We didn't find a matching
account.

If you meant to use a different email, try again with that
address. If you didn't request this, you can safely ignore this
message; no account was created or changed.

— crew.day
"""
