"""Identity context — repository + magic-link seams for email_change (cd-24im).

Defines the seams :mod:`app.domain.identity.email_change` uses to read
and write the self-service email-change rows
(:class:`~app.adapters.db.identity.models.User`,
:class:`~app.adapters.db.identity.models.PasskeyCredential`,
:class:`~app.adapters.db.identity.models.EmailChangePending`,
:func:`~app.adapters.db.identity.models.canonicalise_email`) and to
delegate magic-link mint / consume / peek to :mod:`app.auth.magic_link`
— without importing SQLAlchemy model classes or :mod:`app.auth.magic_link`
directly.

Spec: ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 —
each context defines its own repository port in its public surface
(``app/domain/<context>/ports.py`` — split into a sibling
``email_change_ports.py`` here so the sibling
:mod:`app.domain.identity.availability_ports` /
:mod:`app.domain.identity.me_schedule_ports` files stay focused on the
override / leave / schedule reads they already declare, and the
``email_change`` reads can grow independently of the rest).

Two seams live here:

* :class:`EmailChangeRepository` — read + write seam for the four
  identity rows the email-change flow touches: passkey-cool-off
  lookup, ``users`` swap (and lookup), ``email_change_pending`` CRUD,
  plus the canonicalisation helper. Returns immutable
  :class:`UserIdentityRow` / :class:`EmailChangePendingRow`
  projections so the domain never sees an ORM row.

* :class:`MagicLinkPort` — narrow seam over :mod:`app.auth.magic_link`
  exposing only the four operations email-change calls
  (``request_link``, ``peek_link``, ``consume_link``,
  ``inspect_token_jti``). Translates magic-link-layer exceptions into
  domain-side equivalents (:class:`MagicLinkInvalidToken`,
  :class:`MagicLinkPurposeMismatch`, :class:`MagicLinkTokenExpired`,
  :class:`MagicLinkAlreadyConsumed`) and surfaces a
  :class:`MagicLinkHandle` value object that abstracts the
  ``PendingMagicLink`` (``url`` + idempotent ``deliver()``) without
  forcing the domain to import the concrete class.

The :class:`MagicLinkDispatch` Protocol lets the domain register
deferred sends onto the caller-owned outbox queue (today,
:class:`app.auth.magic_link.PendingDispatch`) without naming that
class — same structural-typing contract as the rest of the seams.

Rationale for option (a) of the cd-24im task brief: a ``MagicLinkPort``
keeps email_change's import surface free of ``app.auth.magic_link``
entirely (and therefore of the transitive ``app.adapters.db.identity.models``
walk that's the actual cd-7qxh reason for the ignore_imports). The
underlying :mod:`app.auth.magic_link` module still reads identity ORM
models directly — that stopgap is tracked at the auth-layer level (see
the ``app.domain.identity.membership -> app.auth.magic_link`` ignore
in pyproject.toml) — but it no longer leaks through email_change's
import contract.

Protocols are deliberately **not** ``runtime_checkable``: structural
compatibility is checked statically by mypy. Runtime ``isinstance``
against these protocols would mask typos and invite duck-typing
shortcuts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from sqlalchemy.orm import Session

from app.adapters.mail.ports import Mailer
from app.auth._throttle import Throttle
from app.config import Settings
from app.util.clock import Clock

__all__ = [
    "EmailChangePendingRow",
    "EmailChangeRepository",
    "MagicLinkAlreadyConsumed",
    "MagicLinkDispatch",
    "MagicLinkHandle",
    "MagicLinkInvalidToken",
    "MagicLinkOutcome",
    "MagicLinkPort",
    "MagicLinkPurposeMismatch",
    "MagicLinkTokenExpired",
    "UserIdentityRow",
]


# ---------------------------------------------------------------------------
# Seam exceptions
# ---------------------------------------------------------------------------


class MagicLinkInvalidToken(ValueError):
    """Magic-link token failed signature / shape verification.

    Seam-level analogue of :class:`app.auth.magic_link.InvalidToken` so
    the domain service can ``except`` on it without importing
    :mod:`app.auth.magic_link`. The SA-backed
    :class:`MagicLinkPort` concretion in :mod:`app.auth.magic_link_port`
    translates the underlying auth-layer exception into this seam-level
    one before raising.
    """


class MagicLinkPurposeMismatch(ValueError):
    """Magic-link token's ``purpose`` differs from the expected one.

    Seam-level analogue of :class:`app.auth.magic_link.PurposeMismatch`.
    """


class MagicLinkTokenExpired(ValueError):
    """Magic-link token's ``exp`` claim or persisted TTL has lapsed.

    Seam-level analogue of :class:`app.auth.magic_link.TokenExpired`.
    """


class MagicLinkAlreadyConsumed(ValueError):
    """Magic-link nonce row is already flipped or unknown.

    Seam-level analogue of :class:`app.auth.magic_link.AlreadyConsumed`.
    """


# ---------------------------------------------------------------------------
# Row + value-object shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserIdentityRow:
    """Immutable projection of a :class:`~app.adapters.db.identity.models.User` row.

    Carries the four columns the email-change flow renders or audits:
    ``id``, ``email`` (display value), ``email_lower`` (canonical
    lookup form), and ``display_name``. The shape is intentionally
    narrow — the email-change service does not need timestamps,
    archive tombstones, locale, or avatar.
    """

    id: str
    email: str
    email_lower: str
    display_name: str


@dataclass(frozen=True, slots=True)
class EmailChangePendingRow:
    """Immutable projection of an ``email_change_pending`` row.

    Mirrors :class:`app.adapters.db.identity.models.EmailChangePending`
    column-for-column. Declared on the seam so the SA adapter projects
    ORM rows into a domain-owned shape without forcing the domain
    service to import the ORM class.

    The plaintext addresses live on this row across the magic-link
    flight + the 72-hour revert window — see the model docstring for
    the lifecycle. The seam keeps both the display-cased form
    (``previous_email`` / ``new_email``) and the canonical lookup form
    (``previous_email_lower`` / ``new_email_lower``); the verify path
    needs both (display for the SMTP send, lower for the
    email-uniqueness re-check).
    """

    id: str
    user_id: str
    request_jti: str
    revert_jti: str | None
    previous_email: str
    previous_email_lower: str
    new_email: str
    new_email_lower: str
    created_at: datetime
    verified_at: datetime | None
    revert_expires_at: datetime | None
    reverted_at: datetime | None


@dataclass(frozen=True, slots=True)
class MagicLinkOutcome:
    """Result of :meth:`MagicLinkPort.peek_link` / :meth:`consume_link`.

    Seam-level analogue of :class:`app.auth.magic_link.MagicLinkOutcome`
    with the same four fields. Re-declared on the domain side so the
    Protocol surface does not depend on the auth-layer dataclass.
    """

    purpose: str
    subject_id: str
    email_hash: str
    ip_hash: str


# ---------------------------------------------------------------------------
# MagicLinkHandle / MagicLinkDispatch Protocols
# ---------------------------------------------------------------------------


class MagicLinkHandle(Protocol):
    """Pending magic-link mint with its rendered URL and deferred SMTP send.

    Structural alias for :class:`app.auth.magic_link.PendingMagicLink`:
    the email-change service reads ``url`` and may invoke
    :meth:`deliver` on the legacy synchronous fallback path, so both
    members are part of the seam contract. Production callers register
    the handle onto a :class:`MagicLinkDispatch` instead of calling
    :meth:`deliver` directly (cd-9slq outbox ordering).

    :meth:`deliver` is idempotent — see
    :class:`app.auth.magic_link.PendingMagicLink` for the
    single-fire / MailDeliveryError-swallow contract.
    """

    @property
    def url(self) -> str:
        """The signed acceptance URL ``{base}/auth/magic/{token}``."""
        ...

    def deliver(self) -> None:
        """Fire the deferred SMTP send. Idempotent across repeat calls."""
        ...


class MagicLinkDispatch(Protocol):
    """Outbox collector for one or more deferred SMTP sends (cd-9slq).

    Structural alias for :class:`app.auth.magic_link.PendingDispatch`:
    the email-change service registers callbacks +
    :class:`MagicLinkHandle` instances on the dispatch the router
    constructs; the router fires :meth:`deliver` post-commit. The seam
    only exposes the two appender methods the domain service uses —
    :meth:`deliver` stays a router responsibility.
    """

    def add_callback(self, callback: Callable[[], None]) -> None:
        """Register one parameter-free deferred send."""
        ...

    def add_pending(self, pending: MagicLinkHandle | None) -> None:
        """Register a magic-link handle for post-commit delivery.

        ``None`` is a no-op so callers can pipe a request-link return
        value directly without a guard (the enumeration-guard
        short-circuit returns ``None`` from
        :meth:`MagicLinkPort.request_link`).
        """
        ...


# ---------------------------------------------------------------------------
# MagicLinkPort
# ---------------------------------------------------------------------------


# The set of magic-link purposes email_change mints / redeems. Mirrors
# the ``MagicLinkPurpose`` Literal in :mod:`app.auth.magic_link`; the
# seam pins the two values it actually uses so a typo at the call site
# fails at type-check time without forcing the domain to import the
# auth-layer alias.
EmailChangeMagicLinkPurpose = Literal[
    "email_change_confirm",
    "email_change_revert",
]


class MagicLinkPort(Protocol):
    """Narrow seam over :mod:`app.auth.magic_link` for the email-change flow.

    Exposes only the four operations email-change calls. Hides the
    auth-layer's identity-ORM reads (cd-4zz stopgap) from
    :mod:`app.domain.identity.email_change`'s import surface so the
    cd-7qxh ``email_change -> app.auth.magic_link`` ignore_imports can
    drop without refactoring magic_link itself.

    The concretion in :mod:`app.auth.magic_link_port` delegates to
    :mod:`app.auth.magic_link` and translates the auth-layer exceptions
    (``InvalidToken`` / ``PurposeMismatch`` / ``TokenExpired`` /
    ``AlreadyConsumed``) into the seam-level equivalents
    (:class:`MagicLinkInvalidToken` / …) before raising. Throttle-layer
    exceptions (:class:`app.auth._throttle.RateLimited` /
    :class:`~app.auth._throttle.ConsumeLockout`) propagate verbatim —
    they're shared infrastructure between the request and consume
    paths and the router maps them to the same vocabulary as the
    other auth flows.
    """

    def request_link(
        self,
        *,
        email: str,
        purpose: EmailChangeMagicLinkPurpose,
        ip: str,
        mailer: Mailer | None,
        base_url: str,
        now: datetime,
        ttl: timedelta | None = None,
        throttle: Throttle,
        settings: Settings | None = None,
        clock: Clock | None = None,
        subject_id: str | None = None,
        send_email: bool = True,
    ) -> MagicLinkHandle | None:
        """Mint one magic-link and queue its writes; return a deferred-send handle.

        Returns ``None`` when the underlying flow's enumeration guard
        short-circuited (no user row matched the email). Email-change
        callers always pass ``subject_id=user.id`` so the silent-miss
        branch is bypassed and a ``None`` here is a programming error
        the caller asserts on.

        See :func:`app.auth.magic_link.request_link` for the full
        contract — this Protocol mirrors its behaviour verbatim except
        that the caller passes a resolved ``now`` instead of relying
        on the implicit ``Clock`` default (the email-change service
        always resolves its own ``now`` first so the cool-off /
        revert-expiry maths stay deterministic).
        """
        ...

    def peek_link(
        self,
        *,
        token: str,
        expected_purpose: EmailChangeMagicLinkPurpose,
        ip: str,
        now: datetime,
        throttle: Throttle,
        settings: Settings | None = None,
        clock: Clock | None = None,
    ) -> MagicLinkOutcome:
        """Validate ``token`` without burning the nonce — read-only preview.

        Mirrors :func:`app.auth.magic_link.peek_link`. Raises
        :class:`MagicLinkInvalidToken`,
        :class:`MagicLinkPurposeMismatch`,
        :class:`MagicLinkTokenExpired`,
        :class:`MagicLinkAlreadyConsumed`, or the throttle's
        :class:`~app.auth._throttle.ConsumeLockout`.
        """
        ...

    def consume_link(
        self,
        *,
        token: str,
        expected_purpose: EmailChangeMagicLinkPurpose,
        ip: str,
        now: datetime,
        throttle: Throttle,
        settings: Settings | None = None,
        clock: Clock | None = None,
    ) -> MagicLinkOutcome:
        """Unseal, race-check-flip, audit. Return the outcome.

        Mirrors :func:`app.auth.magic_link.consume_link`. Raises the
        same seam-level errors as :meth:`peek_link` plus the
        throttle's :class:`~app.auth._throttle.ConsumeLockout`.
        """
        ...

    def inspect_token_jti(
        self,
        token: str,
        *,
        settings: Settings | None = None,
    ) -> str:
        """Return the ``jti`` claim of a signature-verified ``token``.

        Mirrors :func:`app.auth.magic_link.inspect_token_jti`. Raises
        :class:`MagicLinkInvalidToken` on any signature / shape failure.
        """
        ...


# ---------------------------------------------------------------------------
# EmailChangeRepository
# ---------------------------------------------------------------------------


class EmailChangeRepository(Protocol):
    """Read + write seam for the identity rows the email-change flow touches.

    Hides every direct ORM read from
    :mod:`app.domain.identity.email_change`'s import surface so the
    cd-7qxh ``email_change -> app.adapters.db.identity.models``
    ignore_imports can drop. The SA-backed concretion in
    :mod:`app.adapters.db.identity.repositories` walks four ORM
    classes:

    * :class:`~app.adapters.db.identity.models.User` — display-name
      lookup (verify) + email swap (verify) + email-uniqueness
      probe (request, verify).
    * :class:`~app.adapters.db.identity.models.PasskeyCredential` —
      cool-off lookup (request).
    * :class:`~app.adapters.db.identity.models.EmailChangePending` —
      pending-row CRUD (request inserts, verify finds + stamps,
      revert finds + stamps).
    * :func:`~app.adapters.db.identity.models.canonicalise_email` —
      pure helper exposed for the domain's own email
      canonicalisation paths.

    The repo carries an open SQLAlchemy ``Session`` so domain callers
    that also need :func:`app.audit.write_audit` (which still takes a
    concrete ``Session`` today) can thread the same UoW without
    holding a second seam. The accessor drops once the audit writer
    gains its own Protocol port.

    Email-change is identity-scoped — every read and write runs under
    :func:`app.tenancy.tenant_agnostic` because the rows it touches
    have no ``workspace_id`` column. The SA concretion handles the
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
        Protocol port. Mirrors the same accessor on the sibling
        :class:`UserAvailabilityOverrideRepository` /
        :class:`UserLeaveRepository` Protocols — the SA-orm import is
        the established pattern across identity-context ports.
        """
        ...

    # -- Pure helpers ----------------------------------------------------

    def canonicalise_email(self, email: str) -> str:
        """Return the canonical case-folded lookup form of ``email``.

        Mirrors :func:`app.adapters.db.identity.models.canonicalise_email`.
        Exposed on the seam so the domain doesn't have to import the
        adapter helper directly. Pure — no DB access.
        """
        ...

    # -- User reads / writes --------------------------------------------

    def get_user(self, *, user_id: str) -> UserIdentityRow | None:
        """Return the user projection or ``None`` when the row is absent."""
        ...

    def update_user_email(self, *, user_id: str, new_email: str) -> UserIdentityRow:
        """Swap ``users.email`` (and ``email_lower`` via the ORM listener).

        Returns the refreshed projection. The
        ``before_update`` listener on
        :class:`app.adapters.db.identity.models.User` keeps
        ``email_lower`` in sync, mirroring the previous in-line swap.
        Flushes so a peer read in the same UoW sees the new value.
        """
        ...

    # -- Email-uniqueness + cool-off probes ------------------------------

    def email_taken_by_other(
        self, *, new_email_lower: str, current_user_id: str
    ) -> bool:
        """Return ``True`` iff another :class:`User` already holds ``new_email_lower``.

        Used by both ``request_change`` (pre-flight uniqueness gate)
        and ``verify_change`` (re-check inside the swap UoW so a
        sibling claim that landed mid-window is detected). Returns
        ``False`` when the only matching row is the caller's own — a
        no-op self-swap doesn't count as a clash.
        """
        ...

    def latest_passkey_created_at(self, *, user_id: str) -> datetime | None:
        """Return the most-recent ``created_at`` for any of ``user_id``'s passkeys.

        Returns ``None`` when the user has zero passkeys (no cool-off
        applies — the §15 "Self-service lost-device & email-change abuse
        mitigations" "Recent re-enrollment cool-off" only fires when a
        credential exists). The SA concretion normalises tzinfo for
        the SQLite roundtrip so the domain caller can compare against
        an aware ``datetime`` directly.
        """
        ...

    # -- EmailChangePending CRUD -----------------------------------------

    def insert_pending(
        self,
        *,
        pending_id: str,
        user_id: str,
        request_jti: str,
        previous_email: str,
        previous_email_lower: str,
        new_email: str,
        new_email_lower: str,
        created_at: datetime,
    ) -> EmailChangePendingRow:
        """Insert a fresh ``email_change_pending`` row.

        ``revert_jti`` / ``revert_expires_at`` / ``verified_at`` /
        ``reverted_at`` are stamped later via :meth:`mark_verified` /
        :meth:`mark_reverted` so the row is born in the
        request-pending state with all four optional columns NULL.
        Flushes so the audit writer's FK reference sees the new row.
        """
        ...

    def find_pending_by_request_jti(
        self, *, request_jti: str
    ) -> EmailChangePendingRow | None:
        """Return the pending row whose ``request_jti`` matches, or ``None``."""
        ...

    def find_pending_by_revert_jti(
        self, *, revert_jti: str
    ) -> EmailChangePendingRow | None:
        """Return the pending row whose ``revert_jti`` matches, or ``None``."""
        ...

    def mark_verified(
        self,
        *,
        pending_id: str,
        revert_jti: str,
        revert_expires_at: datetime,
        verified_at: datetime,
    ) -> EmailChangePendingRow:
        """Stamp ``revert_jti`` + ``revert_expires_at`` + ``verified_at``.

        Returns the refreshed projection. Flushes so a peer read in
        the same UoW sees the verify-side stamps before the revert
        magic-link goes out.
        """
        ...

    def mark_reverted(
        self, *, pending_id: str, reverted_at: datetime
    ) -> EmailChangePendingRow:
        """Stamp ``reverted_at`` so the row terminates.

        Returns the refreshed projection. Flushes so a peer read in
        the same UoW sees the revert-side stamp before the audit row
        commits.
        """
        ...
