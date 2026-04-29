"""Signed demo cookie helpers.

Demo cookies are intentionally separate from the normal session-cookie
surface: the signed payload names one seeded workspace and one seeded
persona, but it never creates or validates a ``session`` row.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Final

from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import SecretStr

DEMO_COOKIE_MAX_AGE_SECONDS: Final[int] = 2_592_000
_COOKIE_PREFIX: Final[str] = "__Host-crewday_demo_"
_SALT: Final[str] = "crewday-demo-cookie-v1"


@dataclass(frozen=True, slots=True)
class DemoCookieBinding:
    """Validated contents of one demo scenario cookie."""

    scenario_key: str
    workspace_id: str
    persona_user_id: str
    issued_at: int


def scenario_cookie_id(scenario_key: str) -> str:
    """Return the stable scenario id used in the cookie name."""
    return scenario_key.replace("-", "_")


def demo_cookie_name(scenario_key: str) -> str:
    """Return ``__Host-crewday_demo_<scenario_id>`` for ``scenario_key``."""
    return f"{_COOKIE_PREFIX}{scenario_cookie_id(scenario_key)}"


def binding_digest(
    *, scenario_key: str, workspace_id: str, persona_user_id: str
) -> str:
    """Return a stable digest for the demo workspace's cookie binding."""
    body = f"{scenario_key}\n{workspace_id}\n{persona_user_id}".encode()
    return hashlib.sha256(body).hexdigest()


def mint_demo_cookie(
    secret: SecretStr,
    *,
    scenario_key: str,
    workspace_id: str,
    persona_user_id: str,
    issued_at: int | None = None,
) -> str:
    """Return a signed demo-cookie value."""
    iat = int(time.time()) if issued_at is None else issued_at
    serializer = _serializer(secret)
    return serializer.dumps(
        {
            "v": 1,
            "scenario": scenario_key,
            "binding": {
                "workspace_id": workspace_id,
                "persona_user_id": persona_user_id,
            },
            "iat": iat,
        }
    )


def load_demo_cookie(
    secret: SecretStr,
    *,
    scenario_key: str,
    value: str | None,
) -> DemoCookieBinding | None:
    """Validate and decode a demo cookie.

    Bad signatures, stale signing keys, malformed payloads, and
    cross-scenario payloads are all treated as absent cookies.
    """
    if not value:
        return None
    try:
        loaded = _serializer(secret).loads(value)
    except BadSignature:
        return None
    if not isinstance(loaded, dict):
        return None
    if loaded.get("v") != 1 or loaded.get("scenario") != scenario_key:
        return None
    binding = loaded.get("binding")
    if not isinstance(binding, dict):
        return None
    workspace_id = binding.get("workspace_id")
    persona_user_id = binding.get("persona_user_id")
    issued_at = loaded.get("iat")
    if (
        not isinstance(workspace_id, str)
        or not isinstance(persona_user_id, str)
        or not isinstance(issued_at, int)
    ):
        return None
    return DemoCookieBinding(
        scenario_key=scenario_key,
        workspace_id=workspace_id,
        persona_user_id=persona_user_id,
        issued_at=issued_at,
    )


def build_demo_cookie_header(scenario_key: str, value: str) -> str:
    """Return the Set-Cookie header value with the spec-pinned flags."""
    return (
        f"{demo_cookie_name(scenario_key)}={value}; "
        f"Max-Age={DEMO_COOKIE_MAX_AGE_SECONDS}; "
        "Path=/; Secure; HttpOnly; SameSite=None; Partitioned"
    )


def _serializer(secret: SecretStr) -> URLSafeSerializer:
    return URLSafeSerializer(secret.get_secret_value(), salt=_SALT)
