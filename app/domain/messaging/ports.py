"""Messaging context — repository port for the web-push subscription seam.

Defines :class:`PushTokenRepository`, the seam
:mod:`app.domain.messaging.push_tokens` uses to read and write the
``push_token`` rows + the per-workspace VAPID public key in
``workspace.settings_json`` — without importing SQLAlchemy model
classes (cd-74pb).

Spec: ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 —
each context defines its own repository port in its public surface
(``app/domain/<context>/ports.py``) and a SQLAlchemy adapter under
``app/adapters/db/<context>/`` (cd-jzfc reconciled the placement
introduced by cd-duv6). The SA-backed concretion lives in
:mod:`app.adapters.db.messaging.repositories`; tests substitute fakes.

The repo carries an open SQLAlchemy ``Session`` so the audit writer
(:func:`app.audit.write_audit`) — which still takes a concrete
``Session`` today — can ride the same Unit of Work without forcing
callers to thread a second seam. Drops once the audit writer gains
its own Protocol.

The repo-shaped value object :class:`PushTokenRow` mirrors the domain's
:class:`~app.domain.messaging.push_tokens.PushTokenView`. It lives on
the seam so the SA adapter has a domain-owned shape to project ORM
rows into without importing the service module that produces the view
(which would create a circular dependency between ``push_tokens`` and
this module).

Protocol is deliberately **not** ``runtime_checkable``: structural
compatibility is checked statically by mypy. Runtime ``isinstance``
against this Protocol would mask typos and invite duck-typing
shortcuts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

__all__ = [
    "ChatChannelBindingRepository",
    "ChatChannelBindingRow",
    "ChatChannelRepository",
    "ChatChannelRow",
    "ChatGatewayBindingRow",
    "ChatGatewayRepository",
    "ChatLinkChallengeRow",
    "ChatMessageRepository",
    "ChatMessageRow",
    "EmailDeliveryRepository",
    "EmailDeliveryRow",
    "PushDeliveryRepository",
    "PushDeliveryRow",
    "PushTokenRepository",
    "PushTokenRow",
]


# ---------------------------------------------------------------------------
# Row shape (value object)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChatChannelRow:
    """Immutable projection of a ``chat_channel`` row."""

    id: str
    workspace_id: str
    kind: str
    source: str
    external_ref: str | None
    title: str | None
    created_at: datetime
    archived_at: datetime | None


@dataclass(frozen=True, slots=True)
class ChatMessageRow:
    """Immutable projection of a ``chat_message`` row."""

    id: str
    workspace_id: str
    channel_id: str
    author_user_id: str | None
    author_label: str
    body_md: str
    attachments_json: list[dict[str, str]]
    dispatched_to_agent_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChatChannelBindingRow:
    """Immutable projection of a ``chat_channel_binding`` row."""

    id: str
    workspace_id: str
    user_id: str
    user_display_name: str
    channel_kind: str
    address: str
    address_hash: str
    display_label: str
    state: str
    created_at: datetime
    verified_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None
    last_message_at: datetime | None


@dataclass(frozen=True, slots=True)
class ChatLinkChallengeRow:
    """Immutable projection of a ``chat_link_challenge`` row."""

    id: str
    binding_id: str
    code_hash: str
    code_hash_params: str
    attempts: int
    expires_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ChatGatewayBindingRow:
    """Immutable projection of a ``chat_gateway_binding`` row."""

    id: str
    workspace_id: str
    provider: str
    external_contact: str
    channel_id: str
    display_label: str
    provider_metadata_json: dict[str, object]
    created_at: datetime
    last_message_at: datetime | None


@dataclass(frozen=True, slots=True)
class PushDeliveryRow:
    """Immutable projection of a ``notification_push_queue`` row.

    Carries only the fields the worker needs to drive the §10 retry
    schedule (``status``, ``attempt``, ``next_attempt_at``) plus the
    enough context to fire the ``pywebpush`` send (``push_token_id``,
    ``body``, ``payload_json``). The encryption keys + endpoint are
    fetched from the matching :class:`PushTokenRow` by the worker
    when claiming the row, so the queue projection deliberately does
    not duplicate them — a token rotation between enqueue and send
    must use the latest material.
    """

    id: str
    workspace_id: str
    notification_id: str
    push_token_id: str
    kind: str
    body: str
    payload_json: dict[str, object]
    status: str
    attempt: int
    next_attempt_at: datetime | None
    last_status_code: int | None
    last_error: str | None
    last_attempted_at: datetime | None
    sent_at: datetime | None
    dead_lettered_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PushTokenRow:
    """Immutable projection of a ``push_token`` row.

    Mirrors the shape of
    :class:`app.domain.messaging.push_tokens.PushTokenView`; declared
    here so the Protocol surface does not depend on the service module
    (which itself imports this seam).
    """

    id: str
    workspace_id: str
    user_id: str
    endpoint: str
    p256dh: str
    auth: str
    user_agent: str | None
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True, slots=True)
class EmailDeliveryRow:
    """Immutable projection of an ``email_delivery`` row.

    Carries the columns the §10 ledger consumers need: the snapshot
    fields (``to_person_id``, ``to_email_at_send``, ``template_key``,
    ``context_snapshot_json``), the dispatch-state machine
    (``delivery_state``, ``provider_message_id``, ``sent_at``,
    ``first_error``, ``retry_count``), and ``inbound_linkage`` for the
    bounce-reply correlator that may key on a custom VERP token rather
    than the provider message id.
    """

    id: str
    workspace_id: str
    to_person_id: str
    to_email_at_send: str
    template_key: str
    context_snapshot_json: dict[str, object]
    sent_at: datetime | None
    provider_message_id: str | None
    delivery_state: str
    first_error: str | None
    retry_count: int
    inbound_linkage: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# PushTokenRepository
# ---------------------------------------------------------------------------


class PushTokenRepository(Protocol):
    """Read + write seam for ``push_token`` plus the workspace VAPID setting.

    The repo carries an open SQLAlchemy ``Session`` so domain callers
    that also need :func:`app.audit.write_audit` (which still takes a
    concrete ``Session`` today) can thread the same UoW without
    holding a second seam. The accessor drops once the audit writer
    gains its own Protocol port.

    Every method honours the workspace-scoping invariant: the SA
    concretion always pins reads + writes to the ``workspace_id``
    passed by the caller, mirroring the ORM tenant filter as
    defence-in-depth (a misconfigured filter must fail loud).

    The repo never commits or flushes outside what the underlying
    statements require — the caller's UoW owns the transaction
    boundary (§01 "Key runtime invariants" #3).
    """

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session.

        Exposed for callers that need to thread the same UoW through
        :func:`app.audit.write_audit` (which still takes a concrete
        ``Session`` today). Drops when the audit writer gains its
        own Protocol port.
        """
        ...

    # -- Reads -----------------------------------------------------------

    def find_by_user_endpoint(
        self, *, workspace_id: str, user_id: str, endpoint: str
    ) -> PushTokenRow | None:
        """Return the ``(workspace_id, user_id, endpoint)`` row or ``None``.

        Drives both the idempotent ``register`` upsert and the
        ``unregister`` lookup. Scoped to ``workspace_id`` for tenant
        hygiene even though the ORM tenant filter already applies —
        defence-in-depth matches the rest of the messaging service.
        """
        ...

    def list_for_user(
        self, *, workspace_id: str, user_id: str
    ) -> Sequence[PushTokenRow]:
        """Return every push token for ``user_id`` ordered by creation.

        Stable secondary sort on ``id`` so callers that page or diff
        the response see a deterministic order across calls. Returns
        an empty sequence when the user holds no rows in the
        workspace — the ``/me`` surface treats "no devices" as a
        normal state, not an error.
        """
        ...

    def get_workspace_vapid_public_key(
        self, *, workspace_id: str, settings_key: str
    ) -> str | None:
        """Return the VAPID public-key value at ``settings_key`` or ``None``.

        Reads from ``workspace.settings_json[settings_key]``. Returns
        ``None`` for any of:

        * the workspace row is missing (defensive — the tenancy
          middleware should have resolved it);
        * the ``settings_json`` payload is not a dict (corruption);
        * the key is absent;
        * the value is not a non-empty string.

        The caller maps every miss to a single
        :class:`~app.domain.messaging.push_tokens.VapidNotConfigured`
        — the four shapes are operationally identical (the operator
        needs to provision the keypair) and a unified return surface
        keeps the domain service free of model imports.
        """
        ...

    # -- Writes ----------------------------------------------------------

    def insert(
        self,
        *,
        token_id: str,
        workspace_id: str,
        user_id: str,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: str | None,
        created_at: datetime,
    ) -> PushTokenRow:
        """Insert a fresh ``push_token`` row and return its projection.

        Flushes so the caller's next read (and the audit writer's
        FK reference to ``entity_id``) sees the new row.
        """
        ...

    def update_keys(
        self,
        *,
        workspace_id: str,
        user_id: str,
        endpoint: str,
        p256dh: str | None = None,
        auth: str | None = None,
        user_agent: str | None = None,
    ) -> PushTokenRow:
        """Refresh the encryption material on an existing row.

        Used by the idempotent re-subscribe path in :func:`register`:
        a browser that re-runs its service worker against the same
        ``(user_id, endpoint)`` may have rotated ``p256dh`` / ``auth``
        and may carry a new ``user_agent``. Each kwarg is applied
        only when not ``None``; ``user_agent`` follows the existing
        service rule of "only refresh when the caller actually
        provided one" (a curl caller passes ``None`` and we keep the
        prior snapshot).

        The SA concretion mirrors the prior service-layer change-
        detection so a no-op refresh never marks the row dirty —
        keeps the audit "no row written on benign refresh" invariant
        intact.

        Flushes when something actually changed.
        """
        ...

    def delete(self, *, workspace_id: str, user_id: str, endpoint: str) -> None:
        """Hard-delete the named row.

        Caller is responsible for the existence check via
        :meth:`find_by_user_endpoint` — the SA concretion treats a
        missing row as a no-op so a stale "remove me again" doesn't
        trip an :class:`~sqlalchemy.orm.exc.UnmappedInstanceError` at
        flush. The caller's audit row still records the intent on a
        successful prior find.
        """
        ...


class ChatChannelRepository(Protocol):
    """Read + write seam for ``chat_channel`` and explicit members."""

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session for audit/authz seams."""
        ...

    def insert(
        self,
        *,
        channel_id: str,
        workspace_id: str,
        kind: str,
        source: str,
        external_ref: str | None,
        title: str | None,
        created_at: datetime,
    ) -> ChatChannelRow:
        """Insert a fresh channel row."""
        ...

    def list(
        self,
        *,
        workspace_id: str,
        kinds: Sequence[str],
        include_archived: bool,
        after_id: str | None,
        limit: int,
    ) -> Sequence[ChatChannelRow]:
        """Return channels ordered by id for cursor pagination."""
        ...

    def get(self, *, workspace_id: str, channel_id: str) -> ChatChannelRow | None:
        """Return the channel or ``None`` within ``workspace_id``."""
        ...

    def rename(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        title: str | None,
    ) -> ChatChannelRow:
        """Update the display title and return the current row."""
        ...

    def archive(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        archived_at: datetime,
    ) -> ChatChannelRow:
        """Soft-archive the channel and return the current row."""
        ...

    def add_member(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        user_id: str,
        added_at: datetime,
    ) -> None:
        """Add an explicit channel member idempotently."""
        ...

    def is_workspace_member(self, *, workspace_id: str, user_id: str) -> bool:
        """Return true when ``user_id`` belongs to ``workspace_id``."""
        ...

    def remove_member(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        user_id: str,
    ) -> None:
        """Remove an explicit channel member idempotently."""
        ...


class ChatMessageRepository(Protocol):
    """Read + write seam for ``chat_message`` rows."""

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session for audit seams."""
        ...

    def display_label_for_user(self, *, workspace_id: str, user_id: str) -> str:
        """Return the denormalised display label for an author."""
        ...

    def insert(
        self,
        *,
        message_id: str,
        workspace_id: str,
        channel_id: str,
        author_user_id: str | None,
        author_label: str,
        body_md: str,
        attachments_json: list[dict[str, str]],
        created_at: datetime,
    ) -> ChatMessageRow:
        """Insert a fresh message row and return its projection."""
        ...

    def list_for_channel(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        before_created_at: datetime | None,
        before_id: str | None,
        limit: int,
    ) -> Sequence[ChatMessageRow]:
        """Return messages newest-first, keyset-paged by ``created_at`` + ``id``."""
        ...


class ChatChannelBindingRepository(Protocol):
    """Read/write seam for §23 chat-channel bindings."""

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session for audit/authz seams."""
        ...

    def list_bindings(
        self,
        *,
        workspace_id: str,
        user_id: str | None,
        include_revoked: bool,
    ) -> Sequence[ChatChannelBindingRow]:
        """List workspace bindings, optionally narrowed to one user."""
        ...

    def get_binding(
        self,
        *,
        workspace_id: str,
        binding_id: str,
    ) -> ChatChannelBindingRow | None:
        """Return one binding in ``workspace_id`` or ``None``."""
        ...

    def user_exists(self, *, workspace_id: str, user_id: str) -> bool:
        """Return true iff the user belongs to the workspace."""
        ...

    def insert_pending_binding(
        self,
        *,
        binding_id: str,
        workspace_id: str,
        user_id: str,
        channel_kind: str,
        address: str,
        address_hash: str,
        display_label: str,
        created_at: datetime,
    ) -> ChatChannelBindingRow:
        """Insert a pending binding and return it."""
        ...

    def insert_challenge(
        self,
        *,
        challenge_id: str,
        binding_id: str,
        code_hash: str,
        code_hash_params: str,
        sent_via: str,
        expires_at: datetime,
        created_at: datetime,
    ) -> None:
        """Insert a fresh verification challenge."""
        ...

    def latest_open_challenge(
        self,
        *,
        binding_id: str,
    ) -> ChatLinkChallengeRow | None:
        """Return the newest unconsumed challenge for the binding."""
        ...

    def increment_challenge_attempts(
        self,
        *,
        challenge_id: str,
    ) -> None:
        """Increment failed attempts on the challenge."""
        ...

    def verify_binding(
        self,
        *,
        binding_id: str,
        challenge_id: str,
        verified_at: datetime,
    ) -> ChatChannelBindingRow:
        """Mark binding active and consume the challenge."""
        ...

    def revoke_binding(
        self,
        *,
        binding_id: str,
        revoked_at: datetime,
        reason: str,
    ) -> ChatChannelBindingRow:
        """Mark a binding revoked and return it."""
        ...


class ChatGatewayRepository(Protocol):
    """Read + write seam for inbound chat-gateway persistence."""

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session for audit seams."""
        ...

    def find_binding(
        self, *, provider: str, external_contact: str
    ) -> ChatGatewayBindingRow | None:
        """Return the provider/contact binding, or ``None``."""
        ...

    def insert_binding_with_channel(
        self,
        *,
        binding_id: str,
        channel_id: str,
        workspace_id: str,
        provider: str,
        external_contact: str,
        channel_source: str,
        display_label: str,
        provider_metadata_json: dict[str, object],
        created_at: datetime,
    ) -> ChatGatewayBindingRow:
        """Create the gateway channel and its binding in one UoW."""
        ...

    def touch_binding(
        self, *, binding_id: str, last_message_at: datetime
    ) -> ChatGatewayBindingRow:
        """Update ``last_message_at`` and return the binding."""
        ...

    def find_message_by_provider_id(
        self, *, source: str, provider_message_id: str
    ) -> ChatMessageRow | None:
        """Return an already-ingested provider message, if any."""
        ...

    def insert_inbound_message(
        self,
        *,
        message_id: str,
        workspace_id: str,
        channel_id: str,
        gateway_binding_id: str,
        source: str,
        provider_message_id: str,
        author_label: str,
        body_md: str,
        created_at: datetime,
    ) -> ChatMessageRow:
        """Insert a gateway-inbound message row."""
        ...


class PushDeliveryRepository(Protocol):
    """Read + write seam for the ``notification_push_queue`` staging table.

    The web-push delivery worker (cd-y60x) consults this seam to:

    * enqueue one row per active push token at notify time;
    * walk the deployment-wide pending set on each tick;
    * atomically claim a row (CAS update keyed on the prior status
      and ``attempt`` counter) so two workers never double-send;
    * stamp the per-attempt outcome (success / transient retry /
      dead-letter / token-purge).

    Cross-tenant by design — like the webhook dispatcher, the worker
    tick reads under :func:`app.tenancy.tenant_agnostic` because each
    row carries its own ``workspace_id`` and the dispatcher is
    deployment-scope, not per-workspace.
    """

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session for audit seams."""
        ...

    def enqueue(
        self,
        *,
        delivery_id: str,
        workspace_id: str,
        notification_id: str,
        push_token_id: str,
        kind: str,
        body: str,
        payload_json: dict[str, object],
        created_at: datetime,
        next_attempt_at: datetime,
    ) -> PushDeliveryRow:
        """Insert a fresh queue row in ``status='pending'``.

        ``next_attempt_at`` is set to the caller's ``now`` so the
        very next worker tick picks the row up. ``attempt`` starts
        at 0; the first send increments to 1.
        """
        ...

    def select_due(self, *, now: datetime, limit: int) -> Sequence[PushDeliveryRow]:
        """Return up-to-``limit`` rows whose retry window has opened.

        Filters on ``status='pending'`` and ``next_attempt_at <= now``,
        ordered by ``next_attempt_at`` ascending so older overdue
        rows fire first. Cross-tenant — caller wraps in
        :func:`tenant_agnostic`.
        """
        ...

    def claim(
        self,
        *,
        delivery_id: str,
        expected_attempt: int,
        now: datetime,
        in_flight_until: datetime,
    ) -> bool:
        """Atomically flip ``pending → in_flight`` for ``delivery_id``.

        Returns ``True`` when the CAS succeeded (the caller now owns
        the send), ``False`` when another worker already claimed the
        row or the row's ``attempt`` counter moved (race lost). The
        update bumps ``next_attempt_at`` to ``in_flight_until`` so a
        worker that crashes mid-send is recovered by the next tick
        once the in-flight window expires (cd-y60x restart safety).

        ``last_attempted_at`` is stamped to ``now`` so the row's
        attempt timestamp is fresh even if the send raises before
        the success / failure handlers run.
        """
        ...

    def mark_sent(
        self,
        *,
        delivery_id: str,
        attempt: int,
        now: datetime,
        last_status_code: int | None,
    ) -> PushDeliveryRow:
        """Stamp the row as ``status='sent'`` (terminal-clean)."""
        ...

    def mark_transient(
        self,
        *,
        delivery_id: str,
        attempt: int,
        next_attempt_at: datetime,
        now: datetime,
        last_status_code: int | None,
        last_error: str,
    ) -> PushDeliveryRow:
        """Schedule another retry — back to ``status='pending'``."""
        ...

    def mark_dead_lettered(
        self,
        *,
        delivery_id: str,
        attempt: int,
        now: datetime,
        last_status_code: int | None,
        last_error: str,
    ) -> PushDeliveryRow:
        """Stamp the row as ``status='dead_lettered'`` (terminal-failure)."""
        ...

    def get(self, *, delivery_id: str) -> PushDeliveryRow | None:
        """Return the queue row by id, or ``None`` if it's missing."""
        ...

    def get_token(self, *, push_token_id: str) -> PushTokenRow | None:
        """Return the matching :class:`PushTokenRow` by id.

        Reads cross-tenant — the token is identified by primary key
        and the queue row already carries the workspace stamp.
        Returns ``None`` if the token was deleted between enqueue
        and send (the worker treats that as a clean drop).
        """
        ...

    def delete_token(self, *, push_token_id: str) -> str | None:
        """Hard-delete the matching ``push_token`` row by id.

        Returns the deleted row's ``user_id`` so the caller can
        forward it to the audit writer (the audit ledger keys
        ``messaging.push.token_purged`` rows on the user). Returns
        ``None`` when the row was already gone — idempotent.
        """
        ...

    def get_workspace_setting(
        self, *, workspace_id: str, settings_key: str
    ) -> str | None:
        """Return ``workspace.settings_json[settings_key]`` or ``None``.

        Mirrors :meth:`PushTokenRepository.get_workspace_vapid_public_key`
        but takes the key explicitly so the worker can read both the
        VAPID private key and the optional subject claim through one
        seam.
        """
        ...

    def touch_token_last_used(self, *, push_token_id: str, now: datetime) -> None:
        """Update ``push_token.last_used_at`` to ``now`` after a 2xx send.

        Idempotent — a missing token (deleted between claim and
        success) is a no-op.
        """
        ...


class EmailDeliveryRepository(Protocol):
    """Read + write seam for the ``email_delivery`` per-send ledger (cd-8kg7).

    :class:`~app.domain.messaging.notifications.NotificationService`
    drives the row through three transitions:

    * :meth:`insert_queued` — one row per email at fanout time, before
      ``mailer.send``. Persisted with ``delivery_state='queued'`` so a
      worker that scans queued/failed rows (out-of-scope follow-up)
      can re-attempt deliveries the SMTP I/O dropped on the floor.
    * :meth:`mark_sent` — on a successful ``mailer.send`` return the
      provider-issued message id is stamped and the row flips to
      ``delivery_state='sent'``. Provider webhooks join on
      ``provider_message_id`` to advance to ``delivered`` / ``bounced``
      / ``complaint`` / ``failed``.
    * :meth:`mark_failed` — on
      :class:`~app.adapters.mail.ports.MailDeliveryError` the row
      captures the first error, bumps ``retry_count``, and flips to
      ``delivery_state='failed'``. The future retry worker re-walks
      these rows; this seam intentionally does not loop here so the
      service stays a single, predictable insert + update sequence.

    Tenancy: every method pins ``workspace_id`` so the ORM tenant
    filter sees a scoped predicate even when the caller is the
    cross-tenant bounce-webhook handler. The webhook lookup helper
    (:meth:`find_by_provider_message_id`) takes a ``workspace_id``
    argument explicitly because the bounce payload always carries the
    workspace context resolved from the inbound webhook router.

    The repo carries an open ``Session`` and never commits or flushes
    outside what the underlying statements require — the caller's UoW
    owns the transaction boundary (§01 "Key runtime invariants" #3).
    """

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session for audit seams."""
        ...

    def insert_queued(
        self,
        *,
        delivery_id: str,
        workspace_id: str,
        to_person_id: str,
        to_email_at_send: str,
        template_key: str,
        context_snapshot_json: dict[str, object],
        created_at: datetime,
    ) -> EmailDeliveryRow:
        """Insert a fresh ``email_delivery`` row in ``delivery_state='queued'``.

        Flushes so the caller can reference the row id in a follow-up
        :meth:`mark_sent` / :meth:`mark_failed` within the same UoW
        and so the bounce-webhook lookup sees the row even if the
        outer transaction has not yet committed (the §10 worker reads
        through the same session pool).
        """
        ...

    def mark_sent(
        self,
        *,
        delivery_id: str,
        provider_message_id: str,
        sent_at: datetime,
    ) -> EmailDeliveryRow:
        """Stamp the row as ``delivery_state='sent'``.

        Records ``provider_message_id`` (the bounce-webhook join key)
        and ``sent_at``. Other state-machine fields are left alone —
        a future provider webhook advances ``sent → delivered`` or
        ``sent → bounced``.
        """
        ...

    def mark_failed(
        self,
        *,
        delivery_id: str,
        error_text: str,
        now: datetime,
    ) -> EmailDeliveryRow:
        """Stamp the row as ``delivery_state='failed'``.

        Captures ``error_text`` into ``first_error`` only when the
        column is NULL — §10 says "Stored on first failure and not
        overwritten on subsequent retries so support can answer 'what
        went wrong initially?'". Bumps ``retry_count`` so the future
        retry worker's back-off has a counter to drive.
        """
        ...

    def select_due_for_retry(
        self,
        *,
        workspace_id: str,
        now: datetime,
        backoff_schedule_seconds: Sequence[int],
        max_attempts: int,
        limit: int,
    ) -> Sequence[EmailDeliveryRow]:
        """Return queued / failed rows whose retry backoff has elapsed.

        The query is workspace-scoped so the adapter can ride the
        ``ix_email_delivery_workspace_state_sent`` hot-path index:
        ``workspace_id`` equality, ``delivery_state`` bucket, and
        ``sent_at IS NULL`` for the pending states. ``retry_count``
        remains the retry-budget guard and ``created_at`` is the
        persisted timestamp available for backoff in the v1 schema.
        """
        ...

    def find_by_provider_message_id(
        self,
        *,
        workspace_id: str,
        provider_message_id: str,
    ) -> EmailDeliveryRow | None:
        """Look up a row by the ESP-issued message id for bounce/delivered webhooks.

        Returns ``None`` when no row matches — the inbound webhook
        handler treats that as "we never sent this id, drop the
        event". The composite
        ``ix_email_delivery_workspace_provider_msgid`` index keeps
        the lookup cheap even at v1's modest volume.
        """
        ...

    def apply_provider_delivery_state(
        self,
        *,
        workspace_id: str,
        delivery_id: str,
        provider_message_id: str,
        delivery_state: str,
        error_text: str | None,
    ) -> EmailDeliveryRow | None:
        """Apply a provider webhook state update to a previously sent row.

        The update remains workspace-scoped and re-checks the provider
        message id selected by :meth:`find_by_provider_message_id` so a
        stale webhook handler cannot update a row outside the matched
        ledger entry. State movement is monotonic and idempotent:
        duplicate redeliveries return the current row, while older
        states never overwrite a newer terminal state. ``first_error``
        is captured only when it is currently NULL.
        """
        ...
