"""Identity context router scaffold.

The identity context owns users, passkeys, sessions, API tokens,
role grants, permission groups, and invites (spec §01 "Context map",
§03). This scaffold is empty on purpose: it is the reserved seat
the app factory registers under ``/w/<slug>/api/v1/identity`` so
downstream work (cd-rpxd) lands routes without having to re-wire
the factory.

Existing identity-adjacent routers (``auth/tokens``, ``users``,
``auth/passkey`` register) remain wired directly by the factory —
cd-rpxd will reshape them under this router when it fills in the
v1 identity surface.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1._problem_json import IDENTITY_PROBLEM_RESPONSES

router = APIRouter(tags=["identity"], responses=IDENTITY_PROBLEM_RESPONSES)

__all__ = ["router"]
