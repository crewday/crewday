"""``secret_envelope`` adapter — persists §15 envelope rows.

The cipher seam (:mod:`app.adapters.storage.envelope`) writes a row
here on every encrypt and resolves a row here on every decrypt of a
``0x02`` pointer-tagged blob. The rotation worker (cd-rotate-root-key)
walks rows by ``key_fp`` and re-encrypts under the active key.

The table is **not** workspace-scoped — rows reference owner entities
that themselves carry the tenant filter where applicable
(``ical_feed``, ``property``, ...). Deployment-wide secrets
(``smtp_password``, ``openrouter_api_key``, ...) carry no owning
workspace at all. Skipping
:func:`app.tenancy.registry.register` is therefore intentional: same
posture as :class:`~app.adapters.db.identity.models.User` /
:class:`~app.adapters.db.identity.models.MagicLinkNonce` /
:class:`~app.adapters.db.identity.models.WebAuthnChallenge`.

See ``docs/specs/02-domain-model.md`` §"secret_envelope" and
``docs/specs/15-security-privacy.md`` §"Secret envelope".
"""

from __future__ import annotations

from app.adapters.db.secrets.models import SecretEnvelope

__all__ = [
    "SecretEnvelope",
]
