"""Stay-domain worker fan-out bodies."""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import select

from app.adapters.db.session import make_uow
from app.util.clock import Clock
from app.worker.jobs.common import _demo_expired_workspace_ids, _system_actor_context

_log = logging.getLogger("app.worker.scheduler")


def _make_poll_ical_fanout_body(clock: Clock) -> Callable[[], None]:
    """Build the 15 min iCal poller fan-out body (cd-d48).

    Mirror of :func:`_make_overdue_fanout_body` for the iCal poller:
    enumerate every live workspace, bind a system-actor
    :class:`WorkspaceContext`, run
    :func:`~app.worker.tasks.poll_ical.poll_ical` per tenant inside a
    SAVEPOINT so a single broken workspace does not roll back its
    siblings' updates. Demo-expired workspaces are skipped (same §24
    rationale the generator fan-out cites).

    The body lazily constructs a single
    :class:`~app.adapters.storage.envelope.Aes256GcmEnvelope` per tick
    from :attr:`Settings.root_key`. The encryptor is purpose-keyed at
    every call site (``ical_feed.url``), so reuse across workspaces is
    safe and the construction cost is negligible — but a tick where
    no workspace owns an iCal feed never builds the encryptor at all.
    A deployment running without ``CREWDAY_ROOT_KEY`` (the dev /
    test default) skips the tick at WARN level and lets the next
    tick try again — the same posture the budget refresh + idempotency
    sweep take when they cannot reach a required dependency.

    Structured-log emission:

    * ``event="worker.poll_ical.workspace.tick"`` (INFO) — per workspace,
      with ``workspace_id``, ``workspace_slug``, plus every
      ``feeds_*`` / ``reservations_*`` / ``closures_created`` counter
      from :class:`~app.worker.tasks.poll_ical.PollReport`. The
      per-workspace payload operator dashboards key on for "which
      tenants are accumulating iCal errors / not-modified hits?".
    * ``event="worker.poll_ical.workspace.failed"`` (WARNING) — per
      workspace, with ``workspace_id`` + the exception class name.
    * ``event="worker.poll_ical.tick.summary"`` (INFO) — once per tick,
      with ``total_workspaces``, ``total_workspaces_skipped`` (demo-
      expired), ``total_workspaces_failed``, plus the same
      ``total_feeds_*`` / ``total_reservations_*`` / ``total_closures_created``
      sums of the per-workspace :class:`PollReport` counters.

    The :func:`poll_ical` import is deferred into the closure body so
    module import order stays robust — same pattern the sibling
    overdue / generator fan-outs use. The
    :class:`Aes256GcmEnvelope` import is likewise deferred so the
    standalone worker entrypoint still boots when the cryptography
    extras are present but the iCal feature is disabled.
    """

    def _body() -> None:
        from sqlalchemy.orm import Session as _Session

        from app.adapters.db.secrets.repositories import (
            SqlAlchemySecretEnvelopeRepository,
        )
        from app.adapters.db.session import bind_active_session
        from app.adapters.db.workspace.models import Workspace
        from app.adapters.storage.envelope import Aes256GcmEnvelope
        from app.config import get_settings
        from app.tenancy import tenant_agnostic
        from app.tenancy.current import reset_current, set_current
        from app.worker.tasks.poll_ical import poll_ical

        now = clock.now()

        settings = get_settings()
        if settings.root_key is None:
            # No root key wired — the iCal feed URLs encrypted at rest
            # cannot be decrypted, so the poller would emit
            # ``ical_url_malformed`` for every feed. Skip the tick at
            # WARNING so a misconfigured deployment is loud (the
            # heartbeat still bumps because the wrap_job wrapper sees
            # this body return cleanly), and let the next tick retry
            # once the operator wires ``CREWDAY_ROOT_KEY``.
            _log.warning(
                "worker.poll_ical.skipped_no_root_key",
                extra={"event": "worker.poll_ical.skipped_no_root_key"},
            )
            return

        # Cipher is constructed inside the session block below so the
        # cd-znv4 row-backed mode can wire the
        # ``SqlAlchemySecretEnvelopeRepository`` against the live
        # session. Decrypt-only callsites (the iCal poller's
        # ``get_plaintext_url`` walk) need the repository on the
        # cipher to resolve ``0x02`` pointer-tagged blobs; legacy
        # ``0x01`` inline blobs decrypt without it.

        total_workspaces = 0
        total_workspaces_skipped = 0
        total_workspaces_failed = 0
        total_feeds_walked = 0
        total_feeds_polled = 0
        total_feeds_not_modified = 0
        total_feeds_rate_limited = 0
        total_feeds_errored = 0
        total_feeds_skipped = 0
        total_reservations_created = 0
        total_reservations_updated = 0
        total_reservations_cancelled = 0
        total_closures_created = 0

        with make_uow() as session:
            assert isinstance(session, _Session)

            envelope = Aes256GcmEnvelope(
                settings.root_key,
                repository=SqlAlchemySecretEnvelopeRepository(session),
                clock=clock,
            )

            with tenant_agnostic():
                rows = list(session.execute(select(Workspace.id, Workspace.slug)).all())
                workspace_ids = [row.id for row in rows]
                expired_ids = _demo_expired_workspace_ids(
                    session, workspace_ids, now=now
                )

            for row in rows:
                workspace_id = row.id
                workspace_slug = row.slug
                total_workspaces += 1

                if workspace_id in expired_ids:
                    total_workspaces_skipped += 1
                    continue

                ctx = _system_actor_context(
                    workspace_id=workspace_id,
                    workspace_slug=workspace_slug,
                )
                token = set_current(ctx)
                try:
                    try:
                        with session.begin_nested(), bind_active_session(session):
                            # ``bind_active_session`` exposes the
                            # publishing :class:`Session` to the
                            # synchronous stays subscribers wired by
                            # the FastAPI factory's
                            # ``_register_stays_subscriptions`` so the
                            # ReservationUpserted handlers can read /
                            # write through the same UoW (cd-87u7m).
                            report = poll_ical(
                                ctx,
                                session=session,
                                envelope=envelope,
                                now=now,
                                clock=clock,
                                allow_private_addresses=(
                                    settings.ical_allow_private_addresses
                                ),
                            )
                    except Exception as exc:
                        total_workspaces_failed += 1
                        _log.warning(
                            "worker.poll_ical.workspace.failed",
                            extra={
                                "event": "worker.poll_ical.workspace.failed",
                                "workspace_id": workspace_id,
                                "workspace_slug": workspace_slug,
                                "error": type(exc).__name__,
                            },
                        )
                        continue
                finally:
                    reset_current(token)

                total_feeds_walked += report.feeds_walked
                total_feeds_polled += report.feeds_polled
                total_feeds_not_modified += report.feeds_not_modified
                total_feeds_rate_limited += report.feeds_rate_limited
                total_feeds_errored += report.feeds_errored
                total_feeds_skipped += report.feeds_skipped
                total_reservations_created += report.reservations_created
                total_reservations_updated += report.reservations_updated
                total_reservations_cancelled += report.reservations_cancelled
                total_closures_created += report.closures_created

                _log.info(
                    "worker.poll_ical.workspace.tick",
                    extra={
                        "event": "worker.poll_ical.workspace.tick",
                        "workspace_id": workspace_id,
                        "workspace_slug": workspace_slug,
                        "feeds_walked": report.feeds_walked,
                        "feeds_polled": report.feeds_polled,
                        "feeds_not_modified": report.feeds_not_modified,
                        "feeds_rate_limited": report.feeds_rate_limited,
                        "feeds_errored": report.feeds_errored,
                        "feeds_skipped": report.feeds_skipped,
                        "reservations_created": report.reservations_created,
                        "reservations_updated": report.reservations_updated,
                        "reservations_cancelled": report.reservations_cancelled,
                        "closures_created": report.closures_created,
                    },
                )

        _log.info(
            "worker.poll_ical.tick.summary",
            extra={
                "event": "worker.poll_ical.tick.summary",
                "total_workspaces": total_workspaces,
                "total_workspaces_skipped": total_workspaces_skipped,
                "total_workspaces_failed": total_workspaces_failed,
                "total_feeds_walked": total_feeds_walked,
                "total_feeds_polled": total_feeds_polled,
                "total_feeds_not_modified": total_feeds_not_modified,
                "total_feeds_rate_limited": total_feeds_rate_limited,
                "total_feeds_errored": total_feeds_errored,
                "total_feeds_skipped": total_feeds_skipped,
                "total_reservations_created": total_reservations_created,
                "total_reservations_updated": total_reservations_updated,
                "total_reservations_cancelled": total_reservations_cancelled,
                "total_closures_created": total_closures_created,
            },
        )

    return _body
