"""In-memory rate-limiter for the magic-link surface.

**Partial migration in progress — see cd-7huk.** This module
predates the shared abuse-throttle module (``app/abuse/throttle.py``),
which now exists. cd-7huk migrated the passkey-login-begin endpoint
onto the new :func:`~app.abuse.throttle.throttle` decorator. The
remaining buckets (magic-link, signup-start, recover-start, passkey
login-finish lockout) still live here; their handoff to
``app/abuse/throttle.py`` is pending and not yet budgeted.

Three scoped buckets per caller:

* **Request rate** — per-IP and per-email fixed window, 5 hits / 60 s
  on ``/auth/magic/request`` (§15 "Rate limiting and abuse controls":
  "5/min per IP for magic-link send").
* **Consume failure lockout** — per-IP sliding counter, 3 failed
  attempts / 60 s → 10-minute lockout on ``/auth/magic/consume``
  (§15: "3 failed attempts → 10-minute IP lockout").
* **Signup-start budget** — per-IP, per-email, and deployment-wide
  global fixed-window buckets on ``POST /api/v1/signup/start`` (§15
  "Self-serve abuse mitigations": "≤ 5 per IP / hour, ≤ 3 per email
  / hour, ≤ 200 deployment / hour"). Called from
  :mod:`app.auth.signup_abuse`; cd-7huk will absorb this alongside
  the magic-link buckets into the shared abuse throttle.

Storage: the rolling-window **hit counters** (magic-link request,
signup-start, recover-start) delegate to an injected
:class:`~app.abuse.window_store.WindowStore` — in-memory for
single-worker self-host, DB-backed (``throttle_window``) when
``settings.rate_limit_backend == "postgres"`` so the spec §15
per-deployment caps (≤ 200 signup / recover starts / deployment / hour)
hold across every worker instead of being multiplied by the worker
count (cd-0lnr9). The **lockout** state (consume-fail, passkey-login-
fail) still lives in this instance's dicts, guarded by a
:class:`threading.Lock`; sharing those per-actor security floors across
workers is tracked as a follow-up (they need a distinct expiry table,
not a hit-count window).

No persistence (in-memory backend / lockouts): a process restart
resets those buckets. That's a feature, not a bug, for a dev-scoped
throttle: operators can clear the counters by bouncing the service.
The shared DB counter backend outlives a restart, matching multi-worker
expectations.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Final

from app.abuse.window_store import MemoryWindowStore, WindowCheck, WindowStore

__all__ = [
    "ConsumeLockout",
    "PasskeyLoginLockout",
    "RateLimited",
    "RecoveryRateLimited",
    "SignupRateLimited",
    "Throttle",
]


# Defaults documented in the module docstring and §15. Exposed as
# module-level Finals so tests can monkey-patch them to tight values
# without re-plumbing the service.
_REQUEST_LIMIT: Final[int] = 5
_REQUEST_WINDOW: Final[timedelta] = timedelta(minutes=1)

_CONSUME_FAIL_LIMIT: Final[int] = 3
_CONSUME_FAIL_WINDOW: Final[timedelta] = timedelta(minutes=1)
_CONSUME_LOCKOUT: Final[timedelta] = timedelta(minutes=10)

# Signup-start budgets — spec §15 "Self-serve abuse mitigations":
#   * ≤ 5 successful starts per source IP per hour
#   * ≤ 3 successful starts per email lifetime on the deployment
#   * ≤ 200 signup starts per deployment per hour (global cool-off)
#
# Spec treats the per-email cap as "lifetime on the deployment"; we
# implement it as a 1-hour rolling window here. The deployment-wide
# persistent counter moves to the shared throttle with cd-7huk — the
# in-memory rolling window is the right local approximation until
# then, and the per-IP + global caps are already enough to defeat a
# single-shot abuser before the email cap even comes into play.
_SIGNUP_IP_LIMIT: Final[int] = 5
_SIGNUP_EMAIL_LIMIT: Final[int] = 3
_SIGNUP_GLOBAL_LIMIT: Final[int] = 200
_SIGNUP_WINDOW: Final[timedelta] = timedelta(hours=1)
_SIGNUP_GLOBAL_KEY: Final[str] = "__global__"

# Recover-start budgets — spec §15 "Self-service lost-device &
# email-change abuse mitigations":
#   * ≤ 3 successful starts per email per hour
#   * ≤ 10 starts per source IP per hour
#   * ≤ 200 starts per deployment per hour (global cool-off)
#
# The email + global caps match signup's; the per-IP cap is looser
# (10 vs 5) because recovery is the "I already have an account" door
# and a shared egress (CGNAT / campus / corporate NAT) can legitimately
# push more concurrent recoveries than signups. Buckets are scoped
# under their own prefixes so a signup burst does not poison the
# recovery counter (and vice versa): the two flows share machinery,
# not state.
_RECOVER_IP_LIMIT: Final[int] = 10
_RECOVER_EMAIL_LIMIT: Final[int] = 3
_RECOVER_GLOBAL_LIMIT: Final[int] = 200
_RECOVER_WINDOW: Final[timedelta] = timedelta(hours=1)
_RECOVER_GLOBAL_KEY: Final[str] = "__global__"

# Passkey-login failure lockout — spec §15 "Passkey specifics" +
# §"Rate limiting and abuse controls" (magic-link consume carries the
# same 3-fails → 10-min shape; we mirror that for passkey assertion
# failures). Keyed on the credential-id hash AND the source-IP hash:
# an attacker cycling IPs against one credential trips the
# credential-scoped lockout; an attacker cycling credentials from one
# IP trips the IP-scoped lockout. The two keys are evaluated
# independently so either one raises on its own.
_PASSKEY_LOGIN_FAIL_LIMIT: Final[int] = 3
_PASSKEY_LOGIN_FAIL_WINDOW: Final[timedelta] = timedelta(minutes=1)
_PASSKEY_LOGIN_LOCKOUT: Final[timedelta] = timedelta(minutes=10)


class RateLimited(Exception):
    """Caller exceeded the per-scope request budget.

    429-equivalent. The HTTP router maps this to ``429 rate_limited``.
    """


class ConsumeLockout(Exception):
    """Caller IP is locked out of consume for the configured window.

    429-equivalent. Distinct from :class:`RateLimited` so the router
    can emit a different error symbol (``consume_locked_out``) and
    the test suite can pin the 3-fail trigger semantics.
    """


class SignupRateLimited(Exception):
    """Caller exceeded a signup-start bucket (per-IP, per-email, global).

    Carries a ``retry_after_seconds`` hint derived from the oldest
    hit still inside the window + :data:`_SIGNUP_WINDOW`. The HTTP
    router maps this to ``429 rate_limited`` with a ``Retry-After``
    header so the SPA can back off deterministically rather than
    poll-spamming. ``scope`` is one of ``"ip"``, ``"email"``, or
    ``"global"`` — audit rows carry it verbatim so operators can tell
    which limit tripped without parsing the exception message.
    """

    def __init__(self, scope: str, retry_after_seconds: int) -> None:
        super().__init__(
            f"signup-start rate limit exceeded (scope={scope!r}, "
            f"retry_after={retry_after_seconds}s)"
        )
        self.scope = scope
        self.retry_after_seconds = retry_after_seconds


class PasskeyLoginLockout(Exception):
    """Caller IP or credential is inside the passkey-login lockout window.

    429-equivalent. The router maps this to ``429 rate_limited`` —
    distinct from :class:`ConsumeLockout` (magic-link) so audit rows
    and metrics can tell the two surfaces apart, but the public error
    symbol stays identical so an attacker cannot tell *which* surface
    locked them out.

    ``scope`` is ``"credential"`` or ``"ip"`` — the bucket that
    tripped the lockout. Included so audit / metrics can distinguish
    "this credential is under attack" from "this IP is spraying"; the
    HTTP response body never reveals it (both map to the same
    ``rate_limited`` envelope).
    """

    def __init__(self, scope: str) -> None:
        super().__init__(f"passkey-login locked out (scope={scope!r})")
        self.scope = scope


class RecoveryRateLimited(Exception):
    """Caller exceeded a recover-start bucket (per-IP, per-email, global).

    Mirrors :class:`SignupRateLimited` for the self-service recovery
    surface (§15 "Self-service lost-device & email-change abuse
    mitigations"). A distinct exception type — rather than re-using
    :class:`SignupRateLimited` — means the router can emit a recover-
    specific audit symbol (``audit.recovery.rate_limited``) and tests
    can pin the recover-vs-signup dispatch without a stringly-typed
    ``scope`` discriminator. ``scope`` is one of ``"ip"``, ``"email"``,
    or ``"global"``.
    """

    def __init__(self, scope: str, retry_after_seconds: int) -> None:
        super().__init__(
            f"recover-start rate limit exceeded (scope={scope!r}, "
            f"retry_after={retry_after_seconds}s)"
        )
        self.scope = scope
        self.retry_after_seconds = retry_after_seconds


# Scope names reported on a signup / recover rejection, indexed by the
# order the buckets are evaluated (global first, then per-IP, then
# per-email). Carried on the raised exception so audit rows record which
# cap tripped.
_SIGNUP_SCOPES: Final[tuple[str, ...]] = ("global", "ip", "email")
_RECOVER_SCOPES: Final[tuple[str, ...]] = ("global", "ip", "email")


def _retry_after_seconds(oldest: datetime, window: timedelta, now: datetime) -> int:
    """Seconds until ``oldest`` falls out of ``window``, clamped up to 1.

    ``oldest`` is the oldest hit still inside the violating bucket; the
    client should back off until it evicts. Zero-or-negative values are
    clamped up to ``1`` so a ``Retry-After`` header never says "retry in
    0 seconds", which some SPAs treat as "now".
    """
    expires_at = oldest + window
    return max(int((expires_at - now).total_seconds()), 1)


class Throttle:
    """Per-process counter bucket with tripwires for magic-link flows.

    A single instance is shared by both routes; tests construct their
    own so the suite's state never bleeds across cases. The class is
    threadsafe but deliberately not async-aware — the work is
    microseconds of dict mutation, no I/O.
    """

    __slots__ = (
        "_fail_locked_until",
        "_fails",
        "_lock",
        "_passkey_login_fails",
        "_passkey_login_locked_until",
        "_window",
    )

    def __init__(self, window_store: WindowStore | None = None) -> None:
        self._lock = threading.Lock()
        # Rolling-window hit counters (request / signup / recover) live
        # in a pluggable backend so a multi-worker deployment can share
        # them through the DB (cd-0lnr9). Defaults to the in-memory store
        # for single-worker self-host and unit tests.
        self._window = window_store if window_store is not None else MemoryWindowStore()
        # Per-IP failed-consume counters — a rolling window like the
        # shared hit counters, but the reset trigger is different (§15:
        # 3 fails within the window flips the lockout) so it stays local.
        self._fails: dict[str, deque[datetime]] = defaultdict(deque)
        # IPs currently locked out (value is the moment the lockout
        # expires). Not a deque — single expiry per key.
        self._fail_locked_until: dict[str, datetime] = {}
        # Passkey-login failure counters, keyed by a composite
        # ``("credential"|"ip", key_hash)`` tuple so the two buckets
        # evict independently. Separate dicts from the magic-link
        # ``_fails`` so a consume failure can't count against a login
        # bucket and vice versa (§15 "Passkey specifics").
        self._passkey_login_fails: dict[tuple[str, str], deque[datetime]] = defaultdict(
            deque
        )
        self._passkey_login_locked_until: dict[tuple[str, str], datetime] = {}

    # ------------------------------------------------------------------
    # Request (/auth/magic/request) budget
    # ------------------------------------------------------------------

    def check_request(self, *, ip: str, email_hash: str, now: datetime) -> None:
        """Raise :class:`RateLimited` if either IP or email is over budget.

        Hits against the per-IP and per-email buckets count separately;
        a single call advances both. Exceeding either raises — the
        router maps the exception to ``429 rate_limited``. Below the
        gate, the enumeration guard still applies: a matched email and
        a missing email both produce an identical ``202`` response, so
        a caller who stays under the budget learns nothing about
        whether their email exists.
        """
        rejection = self._window.check_and_record_all(
            [
                WindowCheck(
                    scope="request:ip",
                    key=ip,
                    limit=_REQUEST_LIMIT,
                    window=_REQUEST_WINDOW,
                ),
                WindowCheck(
                    scope="request:email",
                    key=email_hash,
                    limit=_REQUEST_LIMIT,
                    window=_REQUEST_WINDOW,
                ),
            ],
            now=now,
        )
        if rejection is None:
            return
        if rejection.index == 0:
            raise RateLimited(f"per-IP request budget exceeded for {ip!r}")
        raise RateLimited("per-email request budget exceeded")

    # ------------------------------------------------------------------
    # Consume (/auth/magic/consume) lockout
    # ------------------------------------------------------------------

    def check_consume_allowed(self, *, ip: str, now: datetime) -> None:
        """Raise :class:`ConsumeLockout` if ``ip`` is inside its lockout.

        The router calls this **before** trying to consume the token
        so a locked-out IP never even touches the nonce row. Clears a
        lapsed lockout in passing.
        """
        with self._lock:
            self._evict_expired_lockout(ip, now)
            if ip in self._fail_locked_until:
                raise ConsumeLockout(f"consume locked out for {ip!r}")

    def record_consume_failure(self, *, ip: str, now: datetime) -> None:
        """Increment the per-IP failure counter; flip lockout on the Nth fail.

        The router calls this after a consume raises (bad signature,
        unknown nonce, expired, already-consumed, purpose mismatch) —
        anything observable as "the caller asked us to redeem a token
        that didn't redeem". Success does **not** call this.
        """
        with self._lock:
            bucket = self._fails[ip]
            self._evict_expired(bucket, now, _CONSUME_FAIL_WINDOW)
            bucket.append(now)
            if len(bucket) >= _CONSUME_FAIL_LIMIT:
                self._fail_locked_until[ip] = now + _CONSUME_LOCKOUT
                # Clear the rolling window so the IP has to earn the
                # next lockout from scratch once this one expires.
                bucket.clear()

    def record_consume_success(self, *, ip: str) -> None:
        """Reset the per-IP failure counter on a successful consume.

        A consume that returned a fresh ``MagicLinkOutcome`` means the
        user finally got through — we don't want one bad attempt an
        hour ago to still count against their next legitimate try.
        """
        with self._lock:
            self._fails.pop(ip, None)
            self._fail_locked_until.pop(ip, None)

    # ------------------------------------------------------------------
    # Signup-start (/api/v1/signup/start) budget
    # ------------------------------------------------------------------

    def check_signup_start(
        self, *, ip_hash: str, email_hash: str, now: datetime
    ) -> None:
        """Raise :class:`SignupRateLimited` when any signup bucket is over.

        Evaluates three fixed-window buckets in priority order so the
        caller learns which one tripped first:

        1. **Global** (``_SIGNUP_GLOBAL_LIMIT`` per
           ``_SIGNUP_WINDOW``) — deployment-wide cool-off. Checked
           first so a hostile swarm across distinct IPs still flips
           the brake before either per-IP or per-email even counts.
        2. **Per-IP** (``_SIGNUP_IP_LIMIT`` per
           ``_SIGNUP_WINDOW``) — stop a single IP spraying many
           emails.
        3. **Per-email** (``_SIGNUP_EMAIL_LIMIT`` per
           ``_SIGNUP_WINDOW``) — stop an attacker cycling IPs against
           one inbox.

        On success (below every cap) each bucket is incremented in
        turn — a successful call advances all three. The ``ip`` key
        is an **IP hash**, not the raw IP: signup_abuse hashes the
        address with the per-deployment pepper before handing it in,
        so this module never touches plaintext PII. Mirror this for
        ``email_hash``.

        ``retry_after_seconds`` on the raised exception is computed
        from the oldest hit inside the violating bucket + window, so
        the client's back-off matches the window tail exactly rather
        than always being the full hour.
        """
        rejection = self._window.check_and_record_all(
            [
                WindowCheck(
                    scope="signup_start:global",
                    key=_SIGNUP_GLOBAL_KEY,
                    limit=_SIGNUP_GLOBAL_LIMIT,
                    window=_SIGNUP_WINDOW,
                ),
                WindowCheck(
                    scope="signup_start:ip",
                    key=ip_hash,
                    limit=_SIGNUP_IP_LIMIT,
                    window=_SIGNUP_WINDOW,
                ),
                WindowCheck(
                    scope="signup_start:email",
                    key=email_hash,
                    limit=_SIGNUP_EMAIL_LIMIT,
                    window=_SIGNUP_WINDOW,
                ),
            ],
            now=now,
        )
        if rejection is not None:
            raise SignupRateLimited(
                scope=_SIGNUP_SCOPES[rejection.index],
                retry_after_seconds=_retry_after_seconds(
                    rejection.oldest_in_window, _SIGNUP_WINDOW, now
                ),
            )

    # ------------------------------------------------------------------
    # Recover-start (/api/v1/auth/recover/passkey/request) budget
    # ------------------------------------------------------------------

    def check_recover_start(
        self, *, ip_hash: str, email_hash: str, now: datetime
    ) -> None:
        """Raise :class:`RecoveryRateLimited` when any recover bucket is over.

        Mirrors :meth:`check_signup_start` in structure but pins
        recover's own caps (:data:`_RECOVER_IP_LIMIT`,
        :data:`_RECOVER_EMAIL_LIMIT`, :data:`_RECOVER_GLOBAL_LIMIT`)
        and uses distinct bucket prefixes (``recover_start:ip`` /
        ``recover_start:email`` / ``recover_start:global``) so the
        two flows share machinery without sharing state: a signup
        burst does not poison the recover counter, and a recover
        burst does not poison the signup counter.

        Eval order mirrors signup — global → per-IP → per-email — so
        a deployment-wide cool-off fires before either per-IP or
        per-email caps come into play. ``retry_after_seconds`` on
        the raised exception is computed from the oldest hit inside
        the violating bucket.
        """
        rejection = self._window.check_and_record_all(
            [
                WindowCheck(
                    scope="recover_start:global",
                    key=_RECOVER_GLOBAL_KEY,
                    limit=_RECOVER_GLOBAL_LIMIT,
                    window=_RECOVER_WINDOW,
                ),
                WindowCheck(
                    scope="recover_start:ip",
                    key=ip_hash,
                    limit=_RECOVER_IP_LIMIT,
                    window=_RECOVER_WINDOW,
                ),
                WindowCheck(
                    scope="recover_start:email",
                    key=email_hash,
                    limit=_RECOVER_EMAIL_LIMIT,
                    window=_RECOVER_WINDOW,
                ),
            ],
            now=now,
        )
        if rejection is not None:
            raise RecoveryRateLimited(
                scope=_RECOVER_SCOPES[rejection.index],
                retry_after_seconds=_retry_after_seconds(
                    rejection.oldest_in_window, _RECOVER_WINDOW, now
                ),
            )

    # ------------------------------------------------------------------
    # Passkey-login (/auth/passkey/login/finish) lockout
    # ------------------------------------------------------------------

    def check_passkey_login_allowed(
        self,
        *,
        credential_id_hash: str,
        ip_hash: str,
        now: datetime,
    ) -> None:
        """Raise :class:`PasskeyLoginLockout` on an active lockout.

        Evaluates both buckets (credential-scoped + IP-scoped) in
        turn; either one raises on its own. The router calls this
        **before** calling :func:`app.auth.webauthn.verify_authentication`
        so a locked-out IP or credential never exercises the
        verification code path. Clears lapsed lockouts in passing.

        ``credential_id_hash`` and ``ip_hash`` are caller-supplied
        hashes — this module deliberately never touches plaintext
        credentials or IPs. The caller hashes them with the same
        ``hash_with_pepper`` subkey the audit layer uses so a single
        pepper rotation invalidates every live lockout.
        """
        with self._lock:
            for scope, key in (
                ("credential", credential_id_hash),
                ("ip", ip_hash),
            ):
                bucket_key = (scope, key)
                self._evict_expired_passkey_lockout(bucket_key, now)
                if bucket_key in self._passkey_login_locked_until:
                    raise PasskeyLoginLockout(scope)

    def record_passkey_login_failure(
        self,
        *,
        credential_id_hash: str,
        ip_hash: str,
        now: datetime,
    ) -> None:
        """Increment both buckets; flip the per-bucket lockout at N failures.

        Called by the router on any observable failure: unknown
        credential, bad signature, clone-detected, challenge consumed
        or expired. Bumps the credential-scoped AND IP-scoped
        counters — an attacker spraying credentials from one IP trips
        the IP bucket, an attacker cycling IPs against one credential
        trips the credential bucket. Either lockout is enough to stop
        the next attempt.
        """
        with self._lock:
            for scope, key in (
                ("credential", credential_id_hash),
                ("ip", ip_hash),
            ):
                bucket_key = (scope, key)
                bucket = self._passkey_login_fails[bucket_key]
                self._evict_expired(bucket, now, _PASSKEY_LOGIN_FAIL_WINDOW)
                bucket.append(now)
                if len(bucket) >= _PASSKEY_LOGIN_FAIL_LIMIT:
                    self._passkey_login_locked_until[bucket_key] = (
                        now + _PASSKEY_LOGIN_LOCKOUT
                    )
                    # Clear the rolling window so the bucket has to
                    # earn the next lockout from scratch once this
                    # one expires — matches the magic-link shape.
                    bucket.clear()

    def record_passkey_login_success(
        self,
        *,
        credential_id_hash: str,
        ip_hash: str,
    ) -> None:
        """Reset both per-credential and per-IP failure counters.

        A successful login means the user finally got through — we
        don't want one bad attempt 30 seconds ago to still count
        against their next legitimate try. Called by the router
        after :func:`app.auth.passkey.login_finish` returns.
        """
        with self._lock:
            for scope, key in (
                ("credential", credential_id_hash),
                ("ip", ip_hash),
            ):
                bucket_key = (scope, key)
                self._passkey_login_fails.pop(bucket_key, None)
                self._passkey_login_locked_until.pop(bucket_key, None)

    def _evict_expired_passkey_lockout(
        self, bucket_key: tuple[str, str], now: datetime
    ) -> None:
        """Clear ``bucket_key`` from the lockout table if the ban has elapsed."""
        expires_at = self._passkey_login_locked_until.get(bucket_key)
        if expires_at is not None and expires_at <= now:
            del self._passkey_login_locked_until[bucket_key]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _evict_expired(
        bucket: deque[datetime], now: datetime, window: timedelta
    ) -> None:
        """Drop hits older than ``now - window`` from the left of ``bucket``."""
        cutoff = now - window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def _evict_expired_lockout(self, ip: str, now: datetime) -> None:
        """Clear ``ip`` from the lockout table if the ban has elapsed."""
        expires_at = self._fail_locked_until.get(ip)
        if expires_at is not None and expires_at <= now:
            del self._fail_locked_until[ip]
