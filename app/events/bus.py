"""In-process synchronous event bus.

v1 delivers events inline within the publisher's unit of work. If a
subscriber raises, the exception propagates to the publisher and the
surrounding UoW rolls back — that's the boundary contract (spec
§"Boundary rules" #3). Later transports (queue, websocket fan-out)
keep this same ``publish`` shape; only the dispatcher body changes.

A cross-worker relay (cd-nusy) sits behind the bus: every locally
published event is also forwarded to sibling workers so an SSE
subscriber connected to a different uvicorn worker can observe it.
The relay is best-effort and runs *after* local fan-out — see
:mod:`app.events.relay`.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

from app.events.registry import Event

if TYPE_CHECKING:
    from app.events.relay import EventRelay

__all__ = ["EventBus", "Handler", "bus"]

_log = logging.getLogger(__name__)


E = TypeVar("E", bound=Event)

# A handler takes an event of some concrete subtype and returns nothing;
# return values from handlers are ignored on purpose (events are fire-
# and-forget from the publisher's perspective).
Handler = Callable[[E], None]


class EventBus:
    """In-process registry of ``event_name → [handlers]``.

    Not a singleton type — tests spin up fresh instances — but the
    module exposes a :data:`bus` singleton that production code
    subscribes against.
    """

    def __init__(self) -> None:
        # ``defaultdict(list)`` keeps insertion order per event name,
        # which the spec requires for deterministic dispatch.
        self._subscribers: dict[str, list[Handler[Event]]] = defaultdict(list)
        # Serialises subscribe/publish/reset against each other. Handler
        # invocation happens *outside* the lock so a handler that itself
        # publishes (common once the agent runtime lands) doesn't
        # deadlock.
        self._lock = threading.Lock()
        # Optional cross-worker relay. ``None`` keeps the bus in pure
        # in-process mode (the default for tests + SQLite deploys).
        # Set via :meth:`set_relay` from the application factory.
        self._relay: EventRelay | None = None

    def subscribe(self, event_type: type[E]) -> Callable[[Handler[E]], Handler[E]]:
        """Register a handler for ``event_type``.

        Used as a decorator::

            @bus.subscribe(TaskCompleted)
            def on_task_completed(event: TaskCompleted) -> None:
                ...

        Handlers fire in subscription order when a matching event is
        published. Subscribing the same handler twice is allowed (and
        will fire it twice) — dedup is the caller's responsibility; the
        bus is a plain fan-out.
        """
        name = event_type.name
        if not name:
            raise ValueError(
                f"{event_type.__name__} has no ``name`` ClassVar; subscribe "
                "to a concrete registered Event subclass."
            )

        def _decorator(handler: Handler[E]) -> Handler[E]:
            # Wrap in a shim typed as ``Handler[Event]`` so the stored
            # list is homogeneous. ``Callable`` is contravariant in its
            # argument, so a handler taking a concrete subclass is not
            # a subtype of one taking ``Event`` — instead of paper-over
            # casts, the shim narrows the event via ``isinstance`` and
            # re-raises on a type mismatch. The dispatch lookup by name
            # keeps this branch unreachable in practice.
            def _shim(event: Event) -> None:
                if not isinstance(event, event_type):
                    raise TypeError(
                        f"Handler registered for {event_type.__name__} "
                        f"was dispatched an incompatible {type(event).__name__}."
                    )
                handler(event)

            with self._lock:
                self._subscribers[name].append(_shim)
            return handler

        return _decorator

    def publish(self, event: Event) -> None:
        """Deliver ``event`` to every subscriber synchronously.

        Subscribers fire in insertion order. **If a handler raises, the
        exception propagates immediately and no later handler runs.**
        That is deliberate: the publisher's UoW is still open, and the
        bus must not swallow failures that should roll the transaction
        back.

        After local fan-out completes, the configured cross-worker
        relay (if any) is invoked to mirror the event to sibling
        workers. The relay is best-effort and never raises — a relay
        failure does not unwind the publisher's UoW (spec §16
        "Multi-worker behaviour"). Re-publication on the receiving
        worker goes through :meth:`publish_local`, which deliberately
        skips the relay step to avoid an infinite loop.
        """
        self.publish_local(event)
        relay = self._relay
        if relay is not None:
            try:
                relay.forward(event)
            except Exception as exc:
                # ``EventRelay.forward`` already swallows backend
                # errors and logs them. This catch is the
                # belt-and-braces guard against a future relay
                # implementation that forgets the contract.
                _log.warning(
                    "events.bus: relay.forward raised on %s: %s",
                    type(event).name,
                    exc,
                    extra={
                        "event": "events.bus.relay_raised",
                        "kind": type(event).name,
                    },
                )

    def publish_local(self, event: Event) -> None:
        """Deliver ``event`` to local subscribers only.

        The relay receive loop calls this path when re-publishing a
        cross-worker NOTIFY so the originating worker's relay does
        not re-NOTIFY the same event back out (which would loop). All
        local subscriber semantics (insertion order, propagation on
        raise) match :meth:`publish`.
        """
        name = type(event).name
        with self._lock:
            # Snapshot under the lock so a concurrent subscribe/reset
            # can't mutate the list mid-iteration.
            handlers = list(self._subscribers.get(name, ()))
        for handler in handlers:
            handler(event)

    def set_relay(self, relay: EventRelay | None) -> None:
        """Attach (or detach) a cross-worker relay.

        Idempotent — re-setting the same relay is a no-op. Replacing
        an existing relay does NOT call ``stop`` on it; the caller
        owns the relay's lifecycle (the application factory's
        lifespan is the canonical owner).
        """
        self._relay = relay

    @property
    def relay(self) -> EventRelay | None:
        """Return the currently attached relay (or ``None``).

        Exposed for the lifespan and tests; production code should
        publish through :meth:`publish` and let the relay routing be
        invisible.
        """
        return self._relay

    def _reset_for_tests(self) -> None:
        """Drop every subscription and detach any relay.

        Tests use this to isolate cases. The relay reset is
        belt-and-braces — most tests build a fresh ``EventBus()``
        instead of mutating the singleton.
        """
        with self._lock:
            self._subscribers.clear()
        self._relay = None


# Production singleton. Tests either use a fresh ``EventBus()`` or call
# ``bus._reset_for_tests()`` between cases.
bus: EventBus = EventBus()
