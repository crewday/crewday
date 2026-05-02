"""Identity context — repository seam for native-app push tokens (cd-nq9s).

Defines the seam :mod:`app.domain.identity.push_tokens` uses to read
and write :class:`~app.adapters.db.identity.models.UserPushToken`
without importing the SQLAlchemy model directly.

Spec: ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 —
each context defines its own repository port in its public surface
and a SQLAlchemy adapter under ``app/adapters/db/<context>/``. The
SA-backed concretion lives in
:class:`app.adapters.db.identity.repositories.SqlAlchemyUserPushTokenRepository`.
Tests substitute fakes.

This is the **identity-scoped** native-app push token surface
(``/api/v1/me/push-tokens``), distinct from the workspace-scoped
web-push surface (:class:`app.domain.messaging.ports.PushTokenRepository`).
The two share neither the table nor the route prefix:

* native (this seam): bare host, self-only, ``platform`` +
  ``token`` row, no workspace pin.
* web-push (sibling): ``/w/<slug>/...``, browser
  ``PushSubscription.endpoint`` + encryption material, workspace pin.

Protocols are deliberately **not** ``runtime_checkable``: structural
compatibility is checked statically by mypy. Runtime ``isinstance``
against these protocols would mask typos and invite duck-typing
shortcuts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

__all__ = [
    "UserPushTokenRepository",
    "UserPushTokenRow",
]


@dataclass(frozen=True, slots=True)
class UserPushTokenRow:
    """Immutable projection of a ``user_push_token`` row.

    Mirrors :class:`app.adapters.db.identity.models.UserPushToken`
    column-for-column. The ``token`` column is INCLUDED on the row
    because the future delivery worker (out of scope for cd-nq9s)
    needs it to ship the push payload to FCM/APNS. The audit seam
    is responsible for never logging this field — see
    :mod:`app.domain.identity.push_tokens` for the discipline.
    """

    id: str
    user_id: str
    platform: str
    token: str
    device_label: str | None
    app_version: str | None
    created_at: datetime
    last_seen_at: datetime
    disabled_at: datetime | None


class UserPushTokenRepository(Protocol):
    """Read + write seam for the identity-scoped ``user_push_token`` table.

    Hides every direct ORM read from
    :mod:`app.domain.identity.push_tokens`'s import surface so the
    cd-7qxh ``push_tokens -> app.adapters.db.identity.models``
    ignore_imports does not re-appear. The SA-backed concretion in
    :mod:`app.adapters.db.identity.repositories` walks the single
    :class:`~app.adapters.db.identity.models.UserPushToken` ORM class.

    The repo carries an open SQLAlchemy ``Session`` so domain callers
    that also need :func:`app.audit.write_audit` (which still takes a
    concrete ``Session`` today) can thread the same UoW without
    holding a second seam. The accessor drops once the audit writer
    gains its own Protocol port.

    Native push tokens are identity-scoped — every read and write
    runs under :func:`app.tenancy.tenant_agnostic` because the row
    has no ``workspace_id`` column. The SA concretion handles the
    tenant-agnostic context wrapping internally so the domain service
    does not have to.

    The repo never commits — the caller's UoW owns the transaction
    boundary (§01 "Key runtime invariants" #3). Mutating methods
    flush so the caller's next read (and the audit writer's FK
    reference to ``entity_id``) sees the new row.
    """

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session.

        Exposed for callers that need to thread the same UoW through
        :func:`app.audit.write_audit` (which still takes a concrete
        ``Session`` today). Drops when the audit writer gains its own
        Protocol port.
        """
        ...

    # -- Reads -----------------------------------------------------------

    def list_for_user(self, *, user_id: str) -> Sequence[UserPushTokenRow]:
        """Return every push-token row for ``user_id`` (active + disabled).

        Ordered by ``created_at`` ascending with ``id`` ascending as a
        stable tiebreaker. The domain service decides whether to surface
        disabled rows on a given call (LIST currently surfaces all rows
        and projects ``disabled_at`` into the view so the SPA can
        tombstone them; the delivery worker filters disabled rows out
        in its own seam).
        """
        ...

    def find_by_id(self, *, user_id: str, token_id: str) -> UserPushTokenRow | None:
        """Return the row owned by ``user_id`` with id ``token_id``, or ``None``.

        The (``user_id``, ``token_id``) pin is part of the seam so
        cross-user PUT/DELETE collapses to ``None`` here — the domain
        service then maps the miss to ``404 push_token_not_found``
        without leaking whether the id exists under a different user.
        """
        ...

    def find_by_user_platform_token(
        self, *, user_id: str, platform: str, token: str
    ) -> UserPushTokenRow | None:
        """Return the row matching ``(user_id, platform, token)`` or ``None``.

        Used by :func:`register` to detect idempotent re-registration
        (same user, same device) — that path returns the existing row
        and refreshes ``last_seen_at`` without writing an audit row.
        """
        ...

    def find_by_platform_token(
        self, *, platform: str, token: str
    ) -> UserPushTokenRow | None:
        """Return the row matching ``(platform, token)`` or ``None``.

        Used by :func:`register` to detect cross-user collision
        (``409 token_claimed``) — a token surfacing on two user
        accounts (device hand-off without a sign-out) per §02
        "user_push_token".
        """
        ...

    # -- Writes ----------------------------------------------------------

    def insert(
        self,
        *,
        token_id: str,
        user_id: str,
        platform: str,
        token: str,
        device_label: str | None,
        app_version: str | None,
        created_at: datetime,
    ) -> UserPushTokenRow:
        """Insert a fresh push-token row and return its projection.

        ``last_seen_at`` is seeded to ``created_at`` — a freshly
        registered device counts as just-seen. Flushes so the audit
        writer's FK reference to ``entity_id`` sees the new row.
        """
        ...

    def update_last_seen(
        self,
        *,
        user_id: str,
        token_id: str,
        last_seen_at: datetime,
    ) -> UserPushTokenRow:
        """Bump ``last_seen_at`` on the named row.

        Returns the refreshed projection. Caller has already verified
        ``token_id`` belongs to ``user_id`` via :meth:`find_by_id`.
        Flushes so a peer read in the same UoW sees the new value.
        """
        ...

    def update_token(
        self,
        *,
        user_id: str,
        token_id: str,
        token: str,
        last_seen_at: datetime,
    ) -> UserPushTokenRow:
        """Swap ``token`` (OS rotation) and bump ``last_seen_at``.

        Returns the refreshed projection. Caller has already verified
        ``token_id`` belongs to ``user_id``. Flushes so a peer read in
        the same UoW sees the rotated value.
        """
        ...

    def delete(self, *, user_id: str, token_id: str) -> bool:
        """Hard-delete the row owned by ``user_id`` with id ``token_id``.

        Returns ``True`` iff a row was actually removed; ``False`` on a
        miss. The domain service uses the boolean to decide whether to
        emit an audit row (no-op deletes are not audit-worthy).
        """
        ...
