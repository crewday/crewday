"""Shared peppered-hash helper for identity-layer audit rows.

``sha256(plaintext || pepper)`` returned as a 64-character hex digest.
The ``pepper`` is always an HKDF subkey derived from the per-deployment
:attr:`app.config.Settings.root_key`; keeping it separate from every
other signing surface is defence-in-depth should a future refactor
split the subkey derivation.

Extracted from :mod:`app.auth.magic_link` + :mod:`app.auth.signup`
(cd-3dc7) so every signup / magic-link / abuse-audit path hashes PII
through one seam. Callers that need the canonical email form still go
through :func:`app.adapters.db.identity.models.canonicalise_email`
before handing a string here — this helper does not normalise.
"""

from __future__ import annotations

import hashlib

__all__ = ["hash_with_pepper"]


def hash_with_pepper(plaintext: str, pepper: bytes) -> str:
    """Return ``sha256(plaintext || pepper)`` as a 64-char hex digest."""
    h = hashlib.sha256()
    h.update(plaintext.encode("utf-8"))
    h.update(pepper)
    return h.hexdigest()
