"""Subkey derivation from ``settings.root_key``.

Every signing / HMAC / hashing surface in the auth stack needs its own
key so an accidental oracle on one path can never forge a token on
another. Rather than demanding a fresh env var per purpose, we carry a
single :attr:`app.config.Settings.root_key` (a :class:`pydantic.SecretStr`)
and derive a per-purpose 32-byte subkey via HKDF-SHA-256 (RFC 5869).

Why HKDF: it's a standard KDF with a formal proof of security for
"extract then expand" against a low-entropy but uniformly-distributed
input. We pass ``root_key`` bytes as the input keying material (IKM),
a fixed module-level ``salt`` (``b"crewday.auth.keys.v1"``) for
domain separation from any future non-auth subkeys, and the ``purpose``
string as the expand ``info`` parameter — ``info`` is exactly what
HKDF defines for this "one master key, many derived keys" pattern.

``hashlib.pbkdf2_hmac`` would also work, but HKDF is the right tool
here (no password-stretching needed — the IKM is already high-entropy)
and is a single stdlib call in Python 3.12 via
:func:`hashlib.hkdf_extract` / :func:`hashlib.hkdf_expand`. Those are
*not* stdlib symbols; we use :mod:`hmac` + :mod:`hashlib` directly,
which is the canonical 20-line HKDF implementation.

The derived subkey is **not cached** — callers should derive once at
service construction (`SMTPMailer`-style) and reuse the bytes
themselves. Caching a :class:`SecretStr` here would leak through
``repr`` chains more readily than keeping it on the call stack.

See ``docs/specs/03-auth-and-tokens.md`` §"Magic link format" (the
"signed with the workspace's magic-link key" clause — today there's
one signing key per deployment; a future `ring_key` table lets
workspaces own their own — and ``docs/specs/15-security-privacy.md``
§"Key management".
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

from pydantic import SecretStr

__all__ = ["KeyDerivationError", "derive_subkey"]


# Fixed salt for HKDF-Extract. Changing this value invalidates every
# subkey ever derived (and, by extension, every signed magic-link in
# flight) — treat it like a schema migration.
_SALT: Final[bytes] = b"crewday.auth.keys.v1"
_OUT_LEN: Final[int] = 32  # 256 bits — matches itsdangerous' default digest.


class KeyDerivationError(RuntimeError):
    """Raised when ``root_key`` is unset or empty.

    The caller should fail the request (or boot) with a clear
    operator-facing message: "CREWDAY_ROOT_KEY is not set". This is a
    config error, not a runtime surprise — the service refuses to
    start rather than silently mint unsigned tokens.
    """


def derive_subkey(root_key: SecretStr | None, *, purpose: str) -> bytes:
    """Return a 32-byte subkey for ``purpose`` derived from ``root_key``.

    ``purpose`` is a short ASCII label (``"magic-link"``,
    ``"session-cookie"``, ``"guest-signing"``, ...). Different
    ``purpose`` values MUST produce unrelated key material — that's
    the whole point of the expand step. Passing the same ``purpose``
    repeatedly is deterministic; that's how two processes on the same
    deployment agree on the same signing key without coordination.
    """
    if root_key is None:
        raise KeyDerivationError(
            "settings.root_key is not set; cannot derive auth subkeys. "
            "Set CREWDAY_ROOT_KEY to a long random value."
        )
    ikm = root_key.get_secret_value().encode("utf-8")
    if not ikm:
        raise KeyDerivationError(
            "settings.root_key is empty; cannot derive auth subkeys. "
            "Set CREWDAY_ROOT_KEY to a long random value."
        )

    # HKDF-Extract: PRK = HMAC(salt, IKM)
    prk = hmac.new(_SALT, ikm, hashlib.sha256).digest()
    # HKDF-Expand: OKM = HMAC(PRK, info || 0x01) truncated to _OUT_LEN.
    # One round is enough because _OUT_LEN <= SHA-256's 32-byte output.
    info = purpose.encode("utf-8") + b"\x01"
    okm = hmac.new(prk, info, hashlib.sha256).digest()
    return okm[:_OUT_LEN]
