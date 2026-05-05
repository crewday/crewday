"""Test-only uvicorn app for cross-process SSE relay integration.

The production app factory brings a lot of auth, storage, scheduler, and
SPA wiring along for the ride. This fixture app keeps the surface small
while preserving the pieces under test: the default event bus, the
Postgres LISTEN/NOTIFY relay, and the workspace SSE router.
"""

from __future__ import annotations

import os
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from app.adapters.db.session import make_engine
from app.api.deps import current_workspace_context
from app.api.transport import sse as sse_mod
from app.events.bus import bus as default_bus
from app.events.relay import build_relay
from app.events.types import NotificationCreated, ShiftChanged
from app.tenancy.context import WorkspaceContext

_WORKSPACE_SLUG = "relay-sse"
_WORKSPACE_ID = "01HX00000000000000000WS0000"
_ACTOR_ID = "01HX00000000000000000USR000"
_CORRELATION_ID = "01HX00000000000000000COR000"
_OCCURRED_AT = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _ctx() -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=_WORKSPACE_ID,
        workspace_slug=_WORKSPACE_SLUG,
        actor_id=_ACTOR_ID,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=_CORRELATION_ID,
        principal_kind="session",
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    sse_mod._reset_for_tests()
    default_bus._reset_for_tests()

    database_url = os.environ["CREWDAY_DATABASE_URL"]
    engine = make_engine(database_url)
    relay = build_relay(engine=engine, bus=default_bus, mode="postgres")
    default_bus.set_relay(relay)
    app.state.relay_engine = engine
    app.state.relay = relay
    await relay.start()
    try:
        yield
    finally:
        await relay.stop()
        default_bus.set_relay(None)
        default_bus._reset_for_tests()
        sse_mod._reset_for_tests()
        engine.dispose()


app = FastAPI(lifespan=_lifespan)
app.dependency_overrides[current_workspace_context] = _ctx
app.include_router(sse_mod.router, prefix="/w/{slug}")


@app.get("/readyz")
def readyz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/test/publish/notification-created")
def publish_notification_created() -> dict[str, str]:
    event = NotificationCreated(
        workspace_id=_WORKSPACE_ID,
        actor_id=_ACTOR_ID,
        correlation_id=_CORRELATION_ID,
        occurred_at=_OCCURRED_AT,
        notification_id="01HX00000000000000000NOT000",
        kind="task_assigned",
        actor_user_id=_ACTOR_ID,
    )
    default_bus.publish(event)
    return {"kind": type(event).name, "notification_id": event.notification_id}


@app.post("/test/publish/time-shift-changed")
def publish_time_shift_changed() -> dict[str, str]:
    event = ShiftChanged(
        workspace_id=_WORKSPACE_ID,
        actor_id=_ACTOR_ID,
        correlation_id=_CORRELATION_ID,
        occurred_at=_OCCURRED_AT,
        shift_id="01HX00000000000000000SHF000",
        user_id=_ACTOR_ID,
        action="closed",
    )
    default_bus.publish(event)
    return {"kind": type(event).name, "shift_id": event.shift_id}


def main() -> None:
    port_file = Path(sys.argv[sys.argv.index("--port-file") + 1])
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.set_inheritable(True)
    port_file.write_text(str(sock.getsockname()[1]), encoding="ascii")

    config = uvicorn.Config(app, log_level="warning")
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
