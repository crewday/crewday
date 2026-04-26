"""SA-backed concretion of the cd-24im ``MagicLinkPort`` seam.

Implements
:class:`app.domain.identity.email_change_ports.MagicLinkPort`.
Wraps :mod:`app.auth.magic_link` to expose only the four operations
the email-change domain service calls (``request_link``, ``peek_link``,
``consume_link``, ``inspect_token_jti``) and translates the auth-layer
exceptions
(:class:`~app.auth.magic_link.InvalidToken` /
:class:`~app.auth.magic_link.PurposeMismatch` /
:class:`~app.auth.magic_link.TokenExpired` /
:class:`~app.auth.magic_link.AlreadyConsumed`) into the seam-level
equivalents declared on
:mod:`app.domain.identity.email_change_ports`.

The translation lives here (rather than at the domain call site)
because the seam contract is "the domain catches the seam-level
exceptions, the concretion does the bridging". Without this layer
the domain would have to ``except`` on the auth-layer types and the
cd-7qxh ``app.domain.identity.email_change -> app.auth.magic_link``
ignore_imports could not drop.

Throttle-layer exceptions (:class:`app.auth._throttle.RateLimited` /
:class:`~app.auth._throttle.ConsumeLockout`) propagate verbatim — the
throttle is shared infrastructure between the request and consume
paths and the router maps those types to the same vocabulary as the
other auth flows.

This file lives under :mod:`app.auth` (not :mod:`app.adapters`)
because :mod:`app.auth.magic_link` is itself an :mod:`app.auth.*`
module, and :mod:`app.adapters` is reserved for storage-backed
adapters (DB, mail, blob). The Port concretion is the seam between
the domain and the auth layer; siting it under ``app.auth`` keeps
the dependency arrow pointing at the right tree.

See ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 and
``docs/specs/03-auth-and-tokens.md`` §"Self-service email change".
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.adapters.mail.ports import Mailer
from app.auth import magic_link
from app.auth._throttle import Throttle
from app.config import Settings
from app.domain.identity.email_change_ports import (
    EmailChangeMagicLinkPurpose,
    MagicLinkAlreadyConsumed,
    MagicLinkHandle,
    MagicLinkInvalidToken,
    MagicLinkOutcome,
    MagicLinkPort,
    MagicLinkPurposeMismatch,
    MagicLinkTokenExpired,
)
from app.util.clock import Clock

__all__ = ["MagicLinkAdapter"]


def _translate_outcome(outcome: magic_link.MagicLinkOutcome) -> MagicLinkOutcome:
    """Project an auth-layer outcome onto the seam-level value object.

    Field-by-field copy — :class:`MagicLinkOutcome` is frozen so the
    domain never mutates the auth-layer instance through a shared
    reference, and the seam stays free of an
    :mod:`app.auth.magic_link` type leak.
    """
    return MagicLinkOutcome(
        purpose=outcome.purpose,
        subject_id=outcome.subject_id,
        email_hash=outcome.email_hash,
        ip_hash=outcome.ip_hash,
    )


class MagicLinkAdapter(MagicLinkPort):
    """Concrete :class:`MagicLinkPort` delegating to :mod:`app.auth.magic_link`.

    Holds the open SQLAlchemy ``Session`` as private state so the
    domain call site doesn't have to thread it as an extra argument
    on every Protocol method. The router constructs one instance per
    UoW (inside the ``with make_uow() as session:`` block) and passes
    it into the email-change service alongside the
    :class:`EmailChangeRepository`.

    The adapter is stateless beyond the session reference. The
    underlying functions take all their other dependencies (mailer,
    throttle, settings, clock) as keyword arguments forwarded by the
    seam call site.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

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
        # :func:`request_link` raises :class:`InvalidToken` on an
        # unknown purpose; the email-change service only ever passes
        # the two purposes the seam Literal pins, so the translation
        # below is defensive — a programming error elsewhere would
        # surface as the seam-level exception, not the auth-layer one.
        try:
            return magic_link.request_link(
                self._session,
                email=email,
                purpose=purpose,
                ip=ip,
                mailer=mailer,
                base_url=base_url,
                now=now,
                ttl=ttl,
                throttle=throttle,
                settings=settings,
                clock=clock,
                subject_id=subject_id,
                send_email=send_email,
            )
        except magic_link.InvalidToken as exc:  # pragma: no cover - defensive
            raise MagicLinkInvalidToken(str(exc)) from exc

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
        try:
            outcome = magic_link.peek_link(
                self._session,
                token=token,
                expected_purpose=expected_purpose,
                ip=ip,
                now=now,
                throttle=throttle,
                settings=settings,
                clock=clock,
            )
        except magic_link.InvalidToken as exc:
            raise MagicLinkInvalidToken(str(exc)) from exc
        except magic_link.PurposeMismatch as exc:
            raise MagicLinkPurposeMismatch(str(exc)) from exc
        except magic_link.TokenExpired as exc:
            raise MagicLinkTokenExpired(str(exc)) from exc
        except magic_link.AlreadyConsumed as exc:
            raise MagicLinkAlreadyConsumed(str(exc)) from exc
        return _translate_outcome(outcome)

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
        try:
            outcome = magic_link.consume_link(
                self._session,
                token=token,
                expected_purpose=expected_purpose,
                ip=ip,
                now=now,
                throttle=throttle,
                settings=settings,
                clock=clock,
            )
        except magic_link.InvalidToken as exc:
            raise MagicLinkInvalidToken(str(exc)) from exc
        except magic_link.PurposeMismatch as exc:
            raise MagicLinkPurposeMismatch(str(exc)) from exc
        except magic_link.TokenExpired as exc:
            raise MagicLinkTokenExpired(str(exc)) from exc
        except magic_link.AlreadyConsumed as exc:
            raise MagicLinkAlreadyConsumed(str(exc)) from exc
        return _translate_outcome(outcome)

    def inspect_token_jti(
        self,
        token: str,
        *,
        settings: Settings | None = None,
    ) -> str:
        try:
            return magic_link.inspect_token_jti(token, settings=settings)
        except magic_link.InvalidToken as exc:
            raise MagicLinkInvalidToken(str(exc)) from exc
