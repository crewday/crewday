"""Standalone worker entrypoint — ``python -m app.worker``.

Spec §16 "Worker process" — the ``worker`` services in Recipes B and
D run the background scheduler in its own container so signup
throttling, SSE fan-out, and LLM calls on the web container don't
contend. This module is the single entrypoint that container
invokes; the web-container lifespan hook in
:mod:`app.api.factory` uses the same :func:`register_jobs` seam so
the two paths can never drift.

Control flow:

1. :func:`setup_logging` — identical shape to the factory's logging
   config so worker logs land in the same JSON stream operators
   already parse.
2. :func:`create_scheduler` + :func:`register_jobs` — build the
   scheduler and wire every job the deployment ships.
3. :func:`start` — kick off the AsyncIO loop via
   :meth:`AsyncIOScheduler.start`.
4. Wait on a pair of signal-triggered futures so SIGTERM / SIGINT
   (Docker's usual stop sequence) shut the scheduler down cleanly
   without a :class:`KeyboardInterrupt` traceback on the console.
5. :func:`stop` — graceful shutdown; the wait flag is True here
   (unlike the web lifespan's immediate shutdown) because a
   dedicated worker container has no HTTP deadline to beat.

Run with ``python -m app.worker`` from any shell where the
``crewday`` package is importable. CI, the Recipe B / D compose
files, and ``crewday-server worker`` (once the CLI wrapper exists,
cd-follow-up) all land here.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from app.config import get_settings
from app.util.logging import setup_logging
from app.worker.scheduler import (
    create_scheduler,
    register_jobs,
    start,
    stop,
)

__all__ = ["main"]

_log = logging.getLogger(__name__)


async def _run() -> None:
    """Main AsyncIO loop — build, start, wait-for-signal, stop.

    We do NOT call :meth:`asyncio.get_event_loop` — :func:`asyncio.run`
    creates the loop and passes it implicitly; reaching into
    :mod:`asyncio` internals would make the test seam harder.
    Instead, :meth:`asyncio.get_running_loop` inside the coroutine
    returns the correct loop for the signal-handler registration.
    """
    settings = get_settings()
    setup_logging(level=settings.log_level)

    from app.adapters.db.session import make_engine
    from app.domain.llm.invalidation_bridge import (
        build_llm_assignment_invalidation_bridge,
    )
    from app.events.bus import bus as default_event_bus

    llm_bridge_engine = make_engine()
    llm_bridge = build_llm_assignment_invalidation_bridge(
        engine=llm_bridge_engine,
        bus=default_event_bus,
    )
    await llm_bridge.start()

    scheduler = create_scheduler()
    register_jobs(scheduler, settings=settings)
    start(scheduler)
    _log.info(
        "worker process started",
        extra={"event": "worker.process.started"},
    )

    loop = asyncio.get_running_loop()
    stop_signal: asyncio.Future[int] = loop.create_future()

    def _handle_signal(signum: int) -> None:
        # Signal handlers fire on the main thread; set the Future's
        # result so the awaiting coroutine wakes up. ``set_result``
        # is a no-op if something else already resolved it (e.g. a
        # second SIGTERM hitting a process that's already shutting
        # down).
        if not stop_signal.done():
            stop_signal.set_result(signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        # ``loop.add_signal_handler`` is POSIX-only; Windows would
        # need an alternate path. v1 targets Linux containers
        # (§16 "Image") so the POSIX path is enough — guard for the
        # ``NotImplementedError`` a Windows dev shell would raise to
        # keep the import importable anywhere.
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:  # pragma: no cover — Windows only
            _log.warning(
                "signal handler unsupported; Ctrl+C will raise KeyboardInterrupt",
                extra={
                    "event": "worker.signal.unsupported",
                    "signal": int(sig),
                },
            )

    try:
        signum = await stop_signal
        _log.info(
            "worker process stopping",
            extra={"event": "worker.process.stopping", "signal": signum},
        )
    finally:
        # ``wait=True`` — the standalone worker has no HTTP deadline, so
        # let pending jobs finish (bounded by their own timeouts).
        stop(scheduler, wait=True)
        await llm_bridge.stop()
        llm_bridge_engine.dispose()
        _log.info(
            "worker process stopped",
            extra={"event": "worker.process.stopped"},
        )


def main() -> int:
    """Synchronous wrapper — matches the ``python -m`` convention.

    Returns an int exit code (0 on a clean shutdown, 1 on a fatal
    error during boot). Tests that want to drive the coroutine
    directly import :func:`_run` instead.
    """
    try:
        asyncio.run(_run())
    except Exception:
        _log.exception(
            "worker process crashed",
            extra={"event": "worker.process.crashed"},
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via ``python -m``
    sys.exit(main())
