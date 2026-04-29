"""Short human-safe token helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets

__all__ = ["CROCKFORD_BASE32_ALPHABET", "short_token"]


CROCKFORD_BASE32_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def short_token(
    workspace_id: str | None = None,
    asset_id: str | None = None,
    *,
    nonce: bytes | None = None,
    length: int = 12,
) -> str:
    """Return a Crockford-base32 token using unambiguous uppercase chars."""
    if length < 1:
        raise ValueError("length must be >= 1")
    salt = nonce if nonce is not None else secrets.token_bytes(16)
    message = f"{workspace_id or ''}:{asset_id or ''}".encode()
    digest = hmac.new(salt, message, hashlib.sha256).digest()
    value = int.from_bytes(digest, "big")
    chars: list[str] = []
    for _ in range(length):
        value, index = divmod(value, len(CROCKFORD_BASE32_ALPHABET))
        chars.append(CROCKFORD_BASE32_ALPHABET[index])
    return "".join(chars)
