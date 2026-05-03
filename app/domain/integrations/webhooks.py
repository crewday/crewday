"""Outbound HMAC-signed webhook delivery (cd-q885).

Public surface:

* :func:`sign` / :func:`verify` — Stripe-style ``t=<unix>,v1=<hex>``
  HMAC-SHA256 over ``f"{t}.{body}"``. Used by senders (us) and
  receivers (third parties) to confirm a payload was minted by the
  party holding the shared secret and not modified in transit.
* :func:`enqueue` — service-layer entry point that records a fresh
  ``webhook_delivery`` row in ``status='pending'`` for every active
  subscription whose event list matches the published event. The
  caller's UoW commits the row; the dispatcher's worker tick fires
  the actual HTTP POST.
* :func:`deliver` — worker tick. Loads one delivery row, POSTs the
  payload to the subscription URL with the signed ``X-Crewday-
  Signature`` header, classifies the response per the §10 retry
  schedule, and stamps the row with the outcome. On retry exhaustion
  or permanent (4xx other than 408 / 429) failure the row flips to
  ``status='dead_lettered'`` and one
  ``audit.webhook_delivery.dead_lettered`` row lands.
* :func:`replay_delivery` — service-layer manual replay. Mints a
  fresh delivery row with the same payload + a new signature
  timestamp; the dispatcher then walks it through the schedule from
  scratch.
* :func:`create_subscription` / :func:`list_subscriptions` /
  :func:`update_subscription` / :func:`delete_subscription` — CRUD
  on the subscription registry. The plaintext signing secret is
  returned exactly once, at create time; subsequent reads only
  expose ``secret_last_4``.

Audit:

* ``audit.webhook_subscription.{created,updated,deleted}`` on every
  subscription mutation.
* ``audit.webhook_delivery.dead_lettered`` only on the terminal
  permanent-failure / retry-exhaustion branch. Successful deliveries
  do not audit (operational telemetry; the §10 spec pins this).

Header format: ``X-Crewday-Signature: t=<unix>,v1=<hex>`` where
``hex`` is HMAC-SHA256 over ``f"{t}.{raw_body_bytes_decoded_utf8}"``
under the per-subscription secret. ``t`` is the unix epoch second the
signature was minted. Receivers should reject signatures whose ``t``
is more than a few minutes from "now" to block replay.

The dispatcher uses a 10-second per-attempt HTTP timeout. The retry
schedule from the moment of enqueue is::

    [0, 30, 300, 3600, 21600, 86400]    # 0 s, 30 s, 5 m, 1 h, 6 h, 24 h

…six attempts total. Permanent (4xx other than 408 / 429) responses
short-circuit the schedule: the row dead-letters on the same attempt.
408 / 429 / 5xx / network errors / timeouts walk the schedule.

See ``docs/specs/10-messaging-notifications.md`` §"Webhooks
(outbound)", ``docs/specs/02-domain-model.md`` §"webhook_subscription"
/ §"webhook_delivery", and ``docs/specs/12-rest-api.md`` §"Messaging".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from sqlalchemy.orm import Session

from app.adapters.storage.ports import EnvelopeEncryptor, EnvelopeOwner
from app.audit import write_audit
from app.config import get_settings
from app.domain.integrations.ports import (
    WebhookDeliveryRow,
    WebhookHealthCandidate,
    WebhookRepository,
    WebhookSubscriptionRow,
)
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "DELIVERY_DEAD_LETTERED",
    "DELIVERY_IN_FLIGHT",
    "DELIVERY_PENDING",
    "DELIVERY_SUCCEEDED",
    "DELIVERY_SUPPRESSED_DEMO",
    "PAUSED_REASON_AUTO_UNHEALTHY",
    "RETRY_SCHEDULE_SECONDS",
    "SIGNATURE_HEADER",
    "SUBSCRIPTION_SECRET_PURPOSE",
    "DeliveryReport",
    "SubscriptionView",
    "WebhookHealthThresholds",
    "create_subscription",
    "delete_subscription",
    "deliver",
    "enable_subscription",
    "enqueue",
    "list_subscriptions",
    "pause_unhealthy_subscriptions",
    "replay_delivery",
    "rotate_subscription_secret",
    "sign",
    "update_subscription",
    "verify",
]


_log = logging.getLogger(__name__)


# §10 retry schedule for cd-q885: 0 s, 30 s, 5 m, 1 h, 6 h, 24 h.
# Six entries → six attempts. The dispatcher uses ``attempt`` as the
# index: attempt 0 fires immediately on enqueue; on failure
# ``next_attempt_at`` is stamped at ``last_attempted_at +
# RETRY_SCHEDULE_SECONDS[attempt]`` for the next slot. After the last
# attempt, the row dead-letters.
RETRY_SCHEDULE_SECONDS: Final[tuple[int, ...]] = (0, 30, 300, 3600, 21600, 86400)

# Per-attempt HTTP timeout. Aligned with §10's documented receiver
# timeout (10 s).
DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 10.0

# Header pin. ``X-Crewday-Signature`` is the cd-q885 canonical name;
# the §10 doc historically wrote ``X-CrewDay-Signature`` (camelCase)
# but HTTP header names are case-insensitive (RFC 7230 §3.2) so the
# wire bytes still round-trip — the canonical name is the
# lowercase-CD form per §12 cross-ref. Receivers normalise.
SIGNATURE_HEADER: Final[str] = "X-Crewday-Signature"

# Subscription-secret purpose label folded into HKDF-Expand at the
# envelope cipher layer. Never colliding with the iCal feed URL
# purpose (``ical-feed-url``) because the prefix is namespaced.
SUBSCRIPTION_SECRET_PURPOSE: Final[str] = "webhook-subscription-secret"

# Status-string constants — match the CHECK constraint on the
# ``webhook_delivery`` table.
DELIVERY_PENDING: Final[str] = "pending"
DELIVERY_IN_FLIGHT: Final[str] = "in_flight"
DELIVERY_SUCCEEDED: Final[str] = "succeeded"
DELIVERY_DEAD_LETTERED: Final[str] = "dead_lettered"
DELIVERY_SUPPRESSED_DEMO: Final[str] = "suppressed_demo"

PAUSED_REASON_AUTO_UNHEALTHY: Final[str] = "auto_unhealthy"

# §10 catalog: minimum length the random secret. 32 bytes of urandom
# rendered hex = 64 hex chars. The receiver shop expects "long enough
# to brute-force HMAC-SHA256 in deep time" — 256 bits of entropy is
# the floor.
_DEFAULT_SECRET_BYTES: Final[int] = 32

# Canonical system-actor placeholder for ``audit_log`` rows the worker
# writes without an ambient :class:`WorkspaceContext`. Mirrors the
# convention already in use across the auth + worker modules
# (:func:`app.worker.scheduler._system_actor_context`,
# :data:`app.auth.signup._AGNOSTIC_ACTOR_ID`, …) so operators can
# ``grep`` one shape when triaging "who fired this row?". The string
# is a 26-char zero ULID — same length as a real ULID, but visibly
# all-zero so it never collides with a minted id.
_SYSTEM_ACTOR_ZERO_ULID: Final[str] = "00000000000000000000000000"


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------


def sign(payload_bytes: bytes, secret: bytes, t: int) -> str:
    """Return ``t=<unix>,v1=<hex>``: the canonical signature header value.

    ``payload_bytes`` is the raw HTTP body the receiver will see on
    the wire (no whitespace normalisation, no charset conversion).
    ``secret`` is the per-subscription HMAC secret (the bytes the
    cipher decrypts to). ``t`` is the unix epoch second the
    signature was minted; pass ``int(clock.now().timestamp())`` from
    the caller's clock so tests can pin it.

    The signed payload is ``f"{t}.{body}"`` UTF-8-encoded — Stripe's
    convention. Receivers reject signatures whose ``t`` is far from
    "now" to block replay; the recommended tolerance is 5 minutes.

    Empty inputs are accepted: the caller is responsible for refusing
    a zero-length secret (the cipher's create_subscription path does
    that). HMAC-SHA256 over an empty body is a real and useful value;
    we don't pre-empt it.
    """
    signing_input = f"{t}.".encode() + payload_bytes
    digest = hmac.new(secret, signing_input, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


def verify(
    header: str,
    body: bytes,
    secret: bytes,
    *,
    tolerance_s: int = 300,
    now_unix: int | None = None,
) -> bool:
    """Round-trip check for a ``X-Crewday-Signature`` header value.

    Returns ``True`` iff:

    * The header parses as exactly one ``t=<int>`` and one ``v1=<hex>``
      pair (extra commas / spaces are tolerated; case is preserved on
      the v1 hex).
    * ``abs(now - t) <= tolerance_s`` — the spec's replay-window check.
    * :func:`hmac.compare_digest` matches the recomputed signature.

    ``now_unix`` is the time used for the freshness window; defaults
    to the system clock so receivers get the right behaviour without
    threading a clock. Tests pin it.

    The function never raises on a malformed header — a bad shape is
    treated as a verification failure. Bubbling a parse error would
    let an attacker distinguish "I sent a bad header" from "I sent a
    valid header with the wrong signature" via the response code,
    which is a side-channel a receiver might not want to expose.
    """
    parsed = _parse_signature_header(header)
    if parsed is None:
        return False
    t, signature_hex = parsed

    if now_unix is None:
        now_unix = int(SystemClock().now().timestamp())
    if abs(now_unix - t) > tolerance_s:
        return False

    expected = sign(body, secret, t).split(",", 1)[1].split("=", 1)[1]
    return hmac.compare_digest(signature_hex, expected)


def _parse_signature_header(header: str) -> tuple[int, str] | None:
    """Return ``(t, hex)`` for a well-formed header, ``None`` otherwise.

    Splits on commas; the order of ``t=`` / ``v1=`` is not pinned.
    Whitespace around segments is stripped. A header with multiple
    ``v1=`` values (e.g. dual-secret rotation) is rejected here —
    the cd-q885 sender uses one secret per subscription; the
    rotation surface lands later (§10 "Secret rotation").
    """
    t_value: int | None = None
    v1_hex: str | None = None
    for segment in header.split(","):
        kv = segment.strip().split("=", 1)
        if len(kv) != 2:
            return None
        key, value = kv[0].strip(), kv[1].strip()
        if key == "t":
            if t_value is not None:
                return None
            try:
                t_value = int(value)
            except ValueError:
                return None
        elif key == "v1":
            if v1_hex is not None:
                return None
            v1_hex = value
        else:
            # Unknown segment — ignore (forward-compat with future
            # ``v2=...`` etc.). Receivers MUST fail closed if they
            # don't understand any present scheme; we fail closed by
            # only matching ``v1``.
            continue
    if t_value is None or not v1_hex:
        return None
    # Hex sanity — reject non-hex bodies before the constant-time
    # compare so a non-hex body doesn't quietly succeed.
    try:
        bytes.fromhex(v1_hex)
    except ValueError:
        return None
    return t_value, v1_hex


# ---------------------------------------------------------------------------
# Subscription CRUD (service layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    """View returned by :func:`create_subscription` / :func:`list_subscriptions`.

    ``plaintext_secret`` is populated **only** on the create path and
    only on the immediate return; subsequent reads (list / get) leave
    it ``None`` and surface ``secret_last_4`` instead.
    """

    id: str
    workspace_id: str
    name: str
    url: str
    secret_last_4: str
    plaintext_secret: str | None
    events: tuple[str, ...]
    active: bool
    paused_reason: str | None
    paused_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WebhookHealthThresholds:
    """Deployment knobs for auto-pausing unhealthy subscriptions."""

    window_h: int = 24
    min_deliveries: int = 3


WebhookAutoPauseNotifier = Callable[
    [WebhookSubscriptionRow, int, WebhookHealthThresholds], None
]


def _generate_secret() -> str:
    """Mint a fresh 256-bit random hex secret."""
    return secrets.token_hex(_DEFAULT_SECRET_BYTES)


def _validate_url(url: str) -> str:
    """Reject obviously-bad URLs at the service layer.

    The §10 spec mandates HTTPS in production but allows HTTP for
    local dev / tests; we accept both so the test harness doesn't
    need a TLS bring-up. Receivers MUST run TLS on the public
    surface (operator policy, not a code-layer check).
    """
    cleaned = url.strip()
    if not cleaned:
        raise ValueError("webhook subscription URL must be non-blank")
    if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
        raise ValueError("webhook subscription URL must use http(s) scheme")
    return cleaned


def _validate_events(events: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Refuse empty event lists and de-duplicate entries.

    The §10 catalog is the authoritative event-name list; this layer
    accepts any non-blank slug rather than hard-pinning to that list,
    because new events land additively in the spec and forcing a
    code update on every catalog widening would block subscribers
    from registering for the new entries until the next deploy.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in events:
        slug = raw.strip()
        if not slug:
            raise ValueError("webhook event name must be non-blank")
        if slug in seen:
            continue
        seen.add(slug)
        cleaned.append(slug)
    if not cleaned:
        raise ValueError("webhook subscription must have at least one event")
    return tuple(cleaned)


def create_subscription(
    session: Session,
    ctx: WorkspaceContext,
    *,
    repo: WebhookRepository,
    envelope: EnvelopeEncryptor,
    name: str,
    url: str,
    events: tuple[str, ...] | list[str],
    secret: str | None = None,
    active: bool = True,
    clock: Clock | None = None,
) -> SubscriptionView:
    """Register a new outbound webhook subscription.

    The plaintext ``secret`` is generated when omitted. It is encrypted
    via the row-backed envelope cipher and only the pointer-tagged
    blob is stored on the row. The view returned by this call is the
    **only** time the plaintext leaves the server — listing /
    get-by-id reads return ``None`` for ``plaintext_secret`` and
    surface ``secret_last_4`` instead.

    Audits ``audit.webhook_subscription.created``.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    cleaned_url = _validate_url(url)
    cleaned_events = _validate_events(events)
    if not name.strip():
        raise ValueError("webhook subscription name must be non-blank")
    plaintext = secret if secret is not None else _generate_secret()
    if not plaintext:
        raise ValueError("webhook subscription secret must be non-blank")
    if len(plaintext) < 16:
        raise ValueError("webhook subscription secret must be at least 16 chars")

    sub_id = new_ulid()
    ciphertext = envelope.encrypt(
        plaintext.encode("utf-8"),
        purpose=SUBSCRIPTION_SECRET_PURPOSE,
        owner=EnvelopeOwner(kind="webhook_subscription", id=sub_id),
    )
    secret_blob = ciphertext.decode("latin-1")
    secret_last_4 = plaintext[-4:]

    row = repo.insert_subscription(
        sub_id=sub_id,
        workspace_id=ctx.workspace_id,
        name=name.strip(),
        url=cleaned_url,
        secret_blob=secret_blob,
        secret_last_4=secret_last_4,
        events=cleaned_events,
        active=active,
        created_at=now,
    )
    write_audit(
        session,
        ctx,
        entity_kind="webhook_subscription",
        entity_id=row.id,
        action="created",
        diff={
            "after": {
                "name": row.name,
                "url": row.url,
                "events": list(row.events),
                "active": row.active,
                "secret_last_4": row.secret_last_4,
            }
        },
        clock=resolved_clock,
    )
    return _to_view(row, plaintext_secret=plaintext)


def list_subscriptions(
    ctx: WorkspaceContext,
    *,
    repo: WebhookRepository,
    active_only: bool = False,
) -> tuple[SubscriptionView, ...]:
    """Return every subscription in the caller's workspace, newest first."""
    rows = repo.list_subscriptions(
        workspace_id=ctx.workspace_id, active_only=active_only
    )
    return tuple(_to_view(row, plaintext_secret=None) for row in rows)


def update_subscription(
    session: Session,
    ctx: WorkspaceContext,
    *,
    repo: WebhookRepository,
    sub_id: str,
    name: str | None = None,
    url: str | None = None,
    events: tuple[str, ...] | list[str] | None = None,
    active: bool | None = None,
    clock: Clock | None = None,
) -> SubscriptionView:
    """Patch a subscription's mutable fields.

    Refuses to roll a workspace boundary: the load + tenant filter
    pin the row to the caller's workspace_id, and the repo raises if
    the row was sweept by another tenant's flow.

    Audits ``audit.webhook_subscription.updated`` with a sparse diff
    listing only the fields the caller asked to change.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    existing = repo.get_subscription(sub_id=sub_id)
    if existing is None or existing.workspace_id != ctx.workspace_id:
        raise LookupError(f"webhook_subscription {sub_id!r} not found")

    cleaned_url = _validate_url(url) if url is not None else None
    cleaned_events = _validate_events(events) if events is not None else None
    cleaned_name: str | None = None
    if name is not None:
        if not name.strip():
            raise ValueError("webhook subscription name must be non-blank")
        cleaned_name = name.strip()

    diff: dict[str, Any] = {}
    if cleaned_name is not None and cleaned_name != existing.name:
        diff["name"] = {"before": existing.name, "after": cleaned_name}
    if cleaned_url is not None and cleaned_url != existing.url:
        diff["url"] = {"before": existing.url, "after": cleaned_url}
    if cleaned_events is not None and cleaned_events != existing.events:
        diff["events"] = {
            "before": list(existing.events),
            "after": list(cleaned_events),
        }
    if active is not None and active != existing.active:
        diff["active"] = {"before": existing.active, "after": active}
    if active is True and existing.paused_reason is not None:
        diff["paused_reason"] = {
            "before": existing.paused_reason,
            "after": None,
        }
        diff["paused_at"] = {
            "before": _audit_dt(existing.paused_at),
            "after": None,
        }

    row = repo.update_subscription(
        sub_id=sub_id,
        name=cleaned_name,
        url=cleaned_url,
        events=cleaned_events,
        active=active,
        updated_at=now,
    )
    if diff:
        write_audit(
            session,
            ctx,
            entity_kind="webhook_subscription",
            entity_id=row.id,
            action="updated",
            diff=diff,
            clock=resolved_clock,
        )
    return _to_view(row, plaintext_secret=None)


def enable_subscription(
    session: Session,
    ctx: WorkspaceContext,
    *,
    repo: WebhookRepository,
    sub_id: str,
    clock: Clock | None = None,
) -> SubscriptionView:
    """Re-enable a subscription and clear any queue pause metadata.

    This does not replay deliveries dropped while the subscription
    was inactive; callers must use the replay surface for that.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    existing = repo.get_subscription(sub_id=sub_id)
    if existing is None or existing.workspace_id != ctx.workspace_id:
        raise LookupError(f"webhook_subscription {sub_id!r} not found")

    row = repo.enable_subscription(sub_id=sub_id, updated_at=now)
    write_audit(
        session,
        ctx,
        entity_kind="webhook_subscription",
        entity_id=row.id,
        action="enabled",
        diff={
            "active": {"before": existing.active, "after": row.active},
            "paused_reason": {
                "before": existing.paused_reason,
                "after": row.paused_reason,
            },
            "paused_at": {
                "before": _audit_dt(existing.paused_at),
                "after": _audit_dt(row.paused_at),
            },
        },
        clock=resolved_clock,
    )
    return _to_view(row, plaintext_secret=None)


def delete_subscription(
    session: Session,
    ctx: WorkspaceContext,
    *,
    repo: WebhookRepository,
    sub_id: str,
    clock: Clock | None = None,
) -> None:
    """Hard-delete a subscription. Cascade nukes its delivery log.

    Audits ``audit.webhook_subscription.deleted`` with a snapshot of
    the row's identity at delete time.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    existing = repo.get_subscription(sub_id=sub_id)
    if existing is None or existing.workspace_id != ctx.workspace_id:
        raise LookupError(f"webhook_subscription {sub_id!r} not found")
    repo.delete_subscription(sub_id=sub_id)
    write_audit(
        session,
        ctx,
        entity_kind="webhook_subscription",
        entity_id=sub_id,
        action="deleted",
        diff={
            "before": {
                "name": existing.name,
                "url": existing.url,
                "events": list(existing.events),
                "active": existing.active,
                "secret_last_4": existing.secret_last_4,
            }
        },
        clock=resolved_clock,
    )


def rotate_subscription_secret(
    session: Session,
    ctx: WorkspaceContext,
    *,
    repo: WebhookRepository,
    envelope: EnvelopeEncryptor,
    sub_id: str,
    secret: str | None = None,
    clock: Clock | None = None,
) -> SubscriptionView:
    """Mint and store a new subscription secret.

    Plaintext is returned exactly once, mirroring
    :func:`create_subscription`. Existing delivery rows stay intact;
    future dispatcher attempts sign with the new secret.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    existing = repo.get_subscription(sub_id=sub_id)
    if existing is None or existing.workspace_id != ctx.workspace_id:
        raise LookupError(f"webhook_subscription {sub_id!r} not found")

    plaintext = secret if secret is not None else _generate_secret()
    if not plaintext:
        raise ValueError("webhook subscription secret must be non-blank")
    if len(plaintext) < 16:
        raise ValueError("webhook subscription secret must be at least 16 chars")

    ciphertext = envelope.encrypt(
        plaintext.encode("utf-8"),
        purpose=SUBSCRIPTION_SECRET_PURPOSE,
        owner=EnvelopeOwner(kind="webhook_subscription", id=sub_id),
    )
    row = repo.rotate_subscription_secret(
        sub_id=sub_id,
        secret_blob=ciphertext.decode("latin-1"),
        secret_last_4=plaintext[-4:],
        updated_at=now,
    )
    write_audit(
        session,
        ctx,
        entity_kind="webhook_subscription",
        entity_id=row.id,
        action="secret_rotated",
        diff={
            "before": {"secret_last_4": existing.secret_last_4},
            "after": {"secret_last_4": row.secret_last_4},
        },
        clock=resolved_clock,
    )
    return _to_view(row, plaintext_secret=plaintext)


# ---------------------------------------------------------------------------
# Health auto-pause
# ---------------------------------------------------------------------------


def pause_unhealthy_subscriptions(
    session: Session,
    *,
    repo: WebhookRepository,
    thresholds: WebhookHealthThresholds | None = None,
    notify_managers: WebhookAutoPauseNotifier | None = None,
    clock: Clock | None = None,
) -> tuple[SubscriptionView, ...]:
    """Pause subscriptions whose configured health window is all failures.

    The repository returns only active subscriptions with at least
    ``min_deliveries`` attempts since ``now - window_h`` and zero 2xx
    responses. Each paused row gets a system-actor audit entry. The
    optional notifier is called after the pause/audit writes so the
    worker can use the normal notification service without coupling
    this domain layer to recipient discovery.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_thresholds = thresholds or WebhookHealthThresholds()
    if resolved_thresholds.window_h <= 0:
        raise ValueError("webhook health window must be positive")
    if resolved_thresholds.min_deliveries <= 0:
        raise ValueError("webhook health minimum deliveries must be positive")

    now = resolved_clock.now()
    window_start = now - timedelta(hours=resolved_thresholds.window_h)
    candidates = repo.list_unhealthy_subscription_candidates(
        window_start=window_start,
        min_deliveries=resolved_thresholds.min_deliveries,
    )

    paused: list[SubscriptionView] = []
    for candidate in candidates:
        row = repo.pause_subscription(
            sub_id=candidate.subscription.id,
            paused_reason=PAUSED_REASON_AUTO_UNHEALTHY,
            paused_at=now,
            updated_at=now,
        )
        _audit_auto_pause(
            session,
            candidate=candidate,
            paused=row,
            thresholds=resolved_thresholds,
            clock=resolved_clock,
        )
        if notify_managers is not None:
            notify_managers(row, candidate.delivery_count, resolved_thresholds)
        paused.append(_to_view(row, plaintext_secret=None))
    return tuple(paused)


# ---------------------------------------------------------------------------
# Enqueue / replay (service layer)
# ---------------------------------------------------------------------------


def enqueue(
    *,
    repo: WebhookRepository,
    workspace_id: str,
    event: str,
    data: Mapping[str, Any],
    clock: Clock | None = None,
    subscription_id: str | None = None,
) -> tuple[str, ...]:
    """Mint one ``webhook_delivery`` row per matching active subscription.

    Returns the tuple of delivery ids (one per fanned-out
    subscription). The dispatcher's worker tick fires the actual
    POST; ``enqueue`` only records intent.

    ``subscription_id`` narrows the fan-out to a single subscription
    (used by :func:`replay_delivery`); ``None`` walks every active
    subscription whose ``events`` list contains ``event``.

    The delivery rows are created with ``status='pending'`` and
    ``next_attempt_at = now`` so the dispatcher picks them up on the
    next tick. Workspace-at-cap budget refusals do **not** block
    webhook delivery — the envelope is LLM-only.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    subscriptions = repo.list_subscriptions(workspace_id=workspace_id, active_only=True)
    if subscription_id is not None:
        subscriptions = tuple(s for s in subscriptions if s.id == subscription_id)
    matching = tuple(s for s in subscriptions if event in s.events)
    demo_mode = get_settings().demo_mode

    delivery_ids: list[str] = []
    for sub in matching:
        delivery_id = new_ulid()
        payload = _build_payload(
            event=event, delivery_id=delivery_id, delivered_at=now, data=dict(data)
        )
        repo.insert_delivery(
            delivery_id=delivery_id,
            workspace_id=workspace_id,
            subscription_id=sub.id,
            event=event,
            payload_json=payload,
            status=DELIVERY_SUPPRESSED_DEMO if demo_mode else DELIVERY_PENDING,
            attempt=0,
            next_attempt_at=None if demo_mode else now,
            replayed_from_id=None,
            created_at=now,
        )
        delivery_ids.append(delivery_id)
    return tuple(delivery_ids)


def replay_delivery(
    ctx: WorkspaceContext,
    *,
    repo: WebhookRepository,
    delivery_id: str,
    clock: Clock | None = None,
) -> str:
    """Mint a fresh delivery row carrying the same payload + a new timestamp.

    The new row stamps ``replayed_from_id`` at the source delivery so
    the audit trail keeps the chain readable. Outside demo, status
    starts at ``pending`` with attempt 0 and next_attempt_at = now;
    in demo mode, the replay is suppressed like a fresh enqueue.

    Each replay re-mints the signature timestamp at delivery time
    (the dispatcher signs with ``int(clock.now().timestamp())`` on
    every attempt), so a successful re-attempt carries a fresh
    ``t=<unix>`` even if the source delivery was minted long ago.

    The repo's :meth:`get_delivery` runs cross-tenant (the worker
    tick has no ambient :class:`WorkspaceContext`), so the
    boundary check happens here in the service layer: ``ctx`` MUST
    own the source row's workspace, otherwise the call raises
    :class:`LookupError` (the same shape as a missing row, so a
    cross-tenant probe cannot enumerate other workspaces' delivery
    ids).

    Returns the new delivery id.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    source = repo.get_delivery(delivery_id=delivery_id)
    if source is None or source.workspace_id != ctx.workspace_id:
        raise LookupError(f"webhook_delivery {delivery_id!r} not found")

    new_id = new_ulid()
    # Refresh the envelope's ``delivery_id`` and ``delivered_at`` so
    # the receiver sees a coherent payload — only ``data`` is reused
    # verbatim. The cd-q885 contract is "same payload, new
    # timestamp"; ``delivery_id`` is part of the payload and is
    # itself part of replay-protection on the receiver side, so it
    # has to roll forward too.
    data = source.payload_json.get("data", {})
    if not isinstance(data, dict):
        # The payload was hand-crafted into an unknown shape; replay
        # what we have rather than synthesising a dict.
        data_dict: dict[str, Any] = {}
    else:
        data_dict = data
    payload = _build_payload(
        event=source.event, delivery_id=new_id, delivered_at=now, data=data_dict
    )
    demo_mode = get_settings().demo_mode
    repo.insert_delivery(
        delivery_id=new_id,
        workspace_id=source.workspace_id,
        subscription_id=source.subscription_id,
        event=source.event,
        payload_json=payload,
        status=DELIVERY_SUPPRESSED_DEMO if demo_mode else DELIVERY_PENDING,
        attempt=0,
        next_attempt_at=None if demo_mode else now,
        replayed_from_id=source.id,
        created_at=now,
    )
    return new_id


# ---------------------------------------------------------------------------
# Worker — deliver one row
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """Summary of a single :func:`deliver` call.

    Returned for callers that want to log / metric the outcome (the
    worker tick logs it at INFO).
    """

    delivery_id: str
    status: str
    attempt: int
    last_status_code: int | None
    last_error: str | None
    dead_lettered: bool


def deliver(
    session: Session,
    *,
    delivery_id: str,
    repo: WebhookRepository,
    envelope: EnvelopeEncryptor,
    http: httpx.Client | None = None,
    clock: Clock | None = None,
) -> DeliveryReport:
    """Fire one HTTP POST attempt and stamp the row with the outcome.

    Worker entry point. Loads the delivery + its subscription (cross-
    tenant — the worker tick has no ambient
    :class:`WorkspaceContext`), builds the signed POST, and classifies
    the response per the §10 retry table:

    * 2xx → ``status='succeeded'``, ``succeeded_at`` stamped.
    * 4xx other than 408 / 429 → permanent failure; row dead-letters
      immediately, no retries; audit
      ``audit.webhook_delivery.dead_lettered``.
    * 408 / 429 / 5xx / network error / timeout → transient. If the
      next attempt is within ``RETRY_SCHEDULE_SECONDS`` the row stays
      ``pending`` with ``next_attempt_at`` bumped; otherwise it
      dead-letters with audit.

    Idempotent on a row already in a terminal state — re-invoking on
    a ``succeeded`` / ``dead_lettered`` / ``suppressed_demo`` row is a
    no-op (logged at INFO).
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    delivery = repo.get_delivery(delivery_id=delivery_id)
    if delivery is None:
        raise LookupError(f"webhook_delivery {delivery_id!r} not found")
    if delivery.status in (
        DELIVERY_SUCCEEDED,
        DELIVERY_DEAD_LETTERED,
        DELIVERY_SUPPRESSED_DEMO,
    ):
        return DeliveryReport(
            delivery_id=delivery.id,
            status=delivery.status,
            attempt=delivery.attempt,
            last_status_code=delivery.last_status_code,
            last_error=delivery.last_error,
            dead_lettered=delivery.status == DELIVERY_DEAD_LETTERED,
        )

    subscription = repo.get_subscription(sub_id=delivery.subscription_id)
    if subscription is None:
        # The subscription was deleted while a delivery was in-flight.
        # Cascade should have nuked the delivery row, but if we land
        # here treat it as a permanent failure rather than retry-loop.
        return _terminal_dead_letter(
            session,
            repo=repo,
            delivery=delivery,
            now=now,
            status_code=None,
            error="subscription_missing",
            attempt=delivery.attempt + 1,
            clock=resolved_clock,
        )

    # Decrypt the per-subscription HMAC secret. The cipher is
    # workspace-agnostic (it reads via the secret_envelope row), so
    # the worker tick does not need a WorkspaceContext for this call.
    secret_blob = subscription.secret_blob.encode("latin-1")
    plaintext_secret = envelope.decrypt(
        secret_blob,
        purpose=SUBSCRIPTION_SECRET_PURPOSE,
        expected_owner=EnvelopeOwner(kind="webhook_subscription", id=subscription.id),
    )

    # Build the signed POST body.
    body_bytes = json.dumps(delivery.payload_json, sort_keys=True).encode("utf-8")
    t_unix = int(now.timestamp())
    signature = sign(body_bytes, plaintext_secret, t_unix)
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature,
        "X-Crewday-Event": delivery.event,
        "X-Crewday-Delivery": delivery.id,
    }

    attempt_number = delivery.attempt + 1
    owns_client = http is None
    client = (
        http if http is not None else httpx.Client(timeout=DEFAULT_HTTP_TIMEOUT_SECONDS)
    )
    try:
        try:
            response = client.post(
                subscription.url, content=body_bytes, headers=headers
            )
        except httpx.TimeoutException as exc:
            return _stamp_transient(
                session,
                repo=repo,
                delivery=delivery,
                subscription=subscription,
                now=now,
                status_code=None,
                error=f"timeout:{type(exc).__name__}",
                attempt=attempt_number,
                clock=resolved_clock,
            )
        except httpx.HTTPError as exc:
            return _stamp_transient(
                session,
                repo=repo,
                delivery=delivery,
                subscription=subscription,
                now=now,
                status_code=None,
                error=f"network:{type(exc).__name__}",
                attempt=attempt_number,
                clock=resolved_clock,
            )
    finally:
        if owns_client:
            client.close()

    return _classify_response(
        session,
        repo=repo,
        delivery=delivery,
        subscription=subscription,
        response=response,
        now=now,
        attempt=attempt_number,
        clock=resolved_clock,
    )


# ---------------------------------------------------------------------------
# Internal classification helpers
# ---------------------------------------------------------------------------


def _classify_response(
    session: Session,
    *,
    repo: WebhookRepository,
    delivery: WebhookDeliveryRow,
    subscription: WebhookSubscriptionRow,
    response: httpx.Response,
    now: datetime,
    attempt: int,
    clock: Clock,
) -> DeliveryReport:
    """Stamp the row per the §10 retry table for ``response.status_code``."""
    status_code = response.status_code
    if 200 <= status_code < 300:
        # Success — clear last_error and mark succeeded.
        with tenant_agnostic():
            row = repo.update_delivery_attempt(
                delivery_id=delivery.id,
                status=DELIVERY_SUCCEEDED,
                attempt=attempt,
                next_attempt_at=None,
                last_status_code=status_code,
                last_error=None,
                last_attempted_at=now,
                succeeded_at=now,
            )
        return DeliveryReport(
            delivery_id=row.id,
            status=row.status,
            attempt=row.attempt,
            last_status_code=row.last_status_code,
            last_error=None,
            dead_lettered=False,
        )

    # 4xx other than 408 / 429 → permanent. The receiver said "no",
    # not "try again later".
    if 400 <= status_code < 500 and status_code not in (408, 429):
        return _terminal_dead_letter(
            session,
            repo=repo,
            delivery=delivery,
            now=now,
            status_code=status_code,
            error=f"http_{status_code}",
            attempt=attempt,
            clock=clock,
            workspace_id=subscription.workspace_id,
        )

    # Otherwise transient — 408 / 429 / 5xx walks the schedule.
    return _stamp_transient(
        session,
        repo=repo,
        delivery=delivery,
        subscription=subscription,
        now=now,
        status_code=status_code,
        error=f"http_{status_code}",
        attempt=attempt,
        clock=clock,
    )


def _stamp_transient(
    session: Session,
    *,
    repo: WebhookRepository,
    delivery: WebhookDeliveryRow,
    subscription: WebhookSubscriptionRow,
    now: datetime,
    status_code: int | None,
    error: str,
    attempt: int,
    clock: Clock,
) -> DeliveryReport:
    """Mark a transient failure; bump retry or dead-letter on exhaustion."""
    if attempt >= len(RETRY_SCHEDULE_SECONDS):
        # Retry budget exhausted — dead-letter with audit.
        return _terminal_dead_letter(
            session,
            repo=repo,
            delivery=delivery,
            now=now,
            status_code=status_code,
            error=error,
            attempt=attempt,
            clock=clock,
            workspace_id=subscription.workspace_id,
        )

    delay_s = RETRY_SCHEDULE_SECONDS[attempt]
    next_attempt_at = now + timedelta(seconds=delay_s)
    with tenant_agnostic():
        row = repo.update_delivery_attempt(
            delivery_id=delivery.id,
            status=DELIVERY_PENDING,
            attempt=attempt,
            next_attempt_at=next_attempt_at,
            last_status_code=status_code,
            last_error=error,
            last_attempted_at=now,
        )
    return DeliveryReport(
        delivery_id=row.id,
        status=row.status,
        attempt=row.attempt,
        last_status_code=row.last_status_code,
        last_error=row.last_error,
        dead_lettered=False,
    )


def _terminal_dead_letter(
    session: Session,
    *,
    repo: WebhookRepository,
    delivery: WebhookDeliveryRow,
    now: datetime,
    status_code: int | None,
    error: str,
    attempt: int,
    clock: Clock,
    workspace_id: str | None = None,
) -> DeliveryReport:
    """Flip the row to ``dead_lettered`` and write the §10 audit row.

    ``workspace_id`` defaults to the delivery row's own
    ``workspace_id`` when omitted; an explicit override lets callers
    that already loaded the subscription avoid a second read.
    """
    with tenant_agnostic():
        row = repo.update_delivery_attempt(
            delivery_id=delivery.id,
            status=DELIVERY_DEAD_LETTERED,
            attempt=attempt,
            next_attempt_at=None,
            last_status_code=status_code,
            last_error=error,
            last_attempted_at=now,
            dead_lettered_at=now,
        )
    _audit_dead_letter(
        session,
        delivery=row,
        workspace_id=workspace_id or delivery.workspace_id,
        clock=clock,
    )
    return DeliveryReport(
        delivery_id=row.id,
        status=row.status,
        attempt=row.attempt,
        last_status_code=row.last_status_code,
        last_error=row.last_error,
        dead_lettered=True,
    )


def _audit_dead_letter(
    session: Session,
    *,
    delivery: WebhookDeliveryRow,
    workspace_id: str,
    clock: Clock,
) -> None:
    """Write ``audit.webhook_delivery.dead_lettered``.

    The worker tick has no ambient :class:`WorkspaceContext`, so we
    synthesise one keyed off the row's own workspace and the
    canonical system-actor placeholder (:data:`_SYSTEM_ACTOR_ZERO_ULID`).
    Mirrors the convention every other system-actor audit site uses
    (:func:`app.worker.scheduler._system_actor_context`,
    :func:`app.auth.signup._agnostic_audit_ctx`, …) so the operator
    dashboard's filter-on-actor surface groups every system-issued
    write under the same id rather than splintering one row per
    dispatcher / sweep.

    ``actor_kind='system'`` makes the same dashboard show "the
    worker flipped this", not "a user did". ``actor_grant_role`` is
    ``'manager'`` per the same convention — the field is read off
    ``scope_kind`` for system rows and the value is structurally
    irrelevant; pinning the same canonical ``'manager'`` lets the
    grep-one-shape posture extend to grant-role filters too.
    """
    ctx = _system_audit_ctx(workspace_id=workspace_id)
    write_audit(
        session,
        ctx,
        entity_kind="webhook_delivery",
        entity_id=delivery.id,
        action="dead_lettered",
        diff={
            "after": {
                "subscription_id": delivery.subscription_id,
                "event": delivery.event,
                "attempt": delivery.attempt,
                "last_status_code": delivery.last_status_code,
                "last_error": delivery.last_error,
            }
        },
        clock=clock,
    )


def _audit_auto_pause(
    session: Session,
    *,
    candidate: WebhookHealthCandidate,
    paused: WebhookSubscriptionRow,
    thresholds: WebhookHealthThresholds,
    clock: Clock,
) -> None:
    ctx = _system_audit_ctx(workspace_id=paused.workspace_id)
    write_audit(
        session,
        ctx,
        entity_kind="webhook_subscription",
        entity_id=paused.id,
        action="auto_paused",
        diff={
            "active": {"before": candidate.subscription.active, "after": paused.active},
            "paused_reason": {
                "before": candidate.subscription.paused_reason,
                "after": paused.paused_reason,
            },
            "paused_at": {
                "before": _audit_dt(candidate.subscription.paused_at),
                "after": _audit_dt(paused.paused_at),
            },
            "thresholds": {
                "webhook_health_window_h": thresholds.window_h,
                "webhook_health_min_deliveries": thresholds.min_deliveries,
                "observed_deliveries": candidate.delivery_count,
                "required_successes": 0,
            },
        },
        clock=clock,
    )


def _system_audit_ctx(*, workspace_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="",
        actor_id=_SYSTEM_ACTOR_ZERO_ULID,
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=_SYSTEM_ACTOR_ZERO_ULID,
        principal_kind="system",
    )


def _audit_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    iso = value.astimezone(UTC).isoformat(timespec="seconds")
    if iso.endswith("+00:00"):
        return iso[:-6] + "Z"
    return iso


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _build_payload(
    *,
    event: str,
    delivery_id: str,
    delivered_at: datetime,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Return the §10 outbound envelope shape.

    ``delivered_at`` is rendered as ISO-8601 UTC with second
    precision and an explicit ``Z`` suffix — the same shape
    receivers compare against for replay-window checks if they
    cross-reference the body. The conversion is anchored to
    :data:`datetime.UTC` so the rendered timestamp is independent of
    the host's local timezone (a bare ``astimezone()`` would convert
    to the OS-local zone, producing ``...+02:00`` on a Paris host).
    """
    iso = delivered_at.astimezone(UTC).isoformat(timespec="seconds")
    if iso.endswith("+00:00"):
        iso = iso[:-6] + "Z"
    return {
        "event": event,
        "delivery_id": delivery_id,
        "delivered_at": iso,
        "data": data,
    }


def _to_view(
    row: WebhookSubscriptionRow, *, plaintext_secret: str | None
) -> SubscriptionView:
    return SubscriptionView(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        url=row.url,
        secret_last_4=row.secret_last_4,
        plaintext_secret=plaintext_secret,
        events=row.events,
        active=row.active,
        paused_reason=row.paused_reason,
        paused_at=row.paused_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
