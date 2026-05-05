"""Two-uvicorn-process SSE relay coverage for Postgres LISTEN/NOTIFY."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.pg_only]

_APP_MODULE = "integration.events._relay_uvicorn_app"
_SLUG = "relay-sse"
_WORKSPACE_ID = "01HX00000000000000000WS0000"


@dataclass(frozen=True)
class _Worker:
    base_url: str
    process: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_path: Path


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _start_worker(
    *,
    db_url: str,
    log_dir: Path,
    name: str,
) -> _Worker:
    stdout_path = log_dir / f"{name}.stdout.log"
    stderr_path = log_dir / f"{name}.stderr.log"
    port_path = log_dir / f"{name}.port"
    stdout = stdout_path.open("wb")
    stderr = stderr_path.open("wb")
    env = os.environ.copy()
    env["CREWDAY_DATABASE_URL"] = db_url
    env["CREWDAY_EVENTS_RELAY"] = "postgres"
    env.setdefault("CREWDAY_PROFILE", "test")
    env["PYTHONPATH"] = os.pathsep.join((str(Path.cwd() / "tests"), os.getcwd()))
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            _APP_MODULE,
            "--port-file",
            str(port_path),
        ],
        cwd=os.getcwd(),
        env=env,
        stdout=stdout,
        stderr=stderr,
    )
    stdout.close()
    stderr.close()
    try:
        port = _wait_for_port_file(
            process=process,
            port_path=port_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        worker = _Worker(
            base_url=f"http://127.0.0.1:{port}",
            process=process,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        _wait_until_ready(worker)
    except Exception:
        _terminate_process(process)
        raise
    return worker


def _wait_for_port_file(
    *,
    process: subprocess.Popen[bytes],
    port_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout: float = 5.0,
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"uvicorn worker exited before publishing its port "
                f"with {process.returncode}\n"
                f"stdout:\n{stdout_path.read_text(encoding='utf-8')}\n"
                f"stderr:\n{stderr_path.read_text(encoding='utf-8')}"
            )
        if port_path.exists():
            raw_port = port_path.read_text(encoding="ascii").strip()
            return int(raw_port)
        time.sleep(0.05)
    raise AssertionError("uvicorn worker did not publish its bound port")


def _wait_until_ready(worker: _Worker, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if worker.process.poll() is not None:
            raise AssertionError(
                f"uvicorn worker exited early with {worker.process.returncode}\n"
                f"stdout:\n{worker.stdout_path.read_text(encoding='utf-8')}\n"
                f"stderr:\n{worker.stderr_path.read_text(encoding='utf-8')}"
            )
        try:
            response = httpx.get(f"{worker.base_url}/readyz", timeout=0.5)
            if response.status_code == 200:
                return
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
        time.sleep(0.05)
    raise AssertionError(
        f"uvicorn worker did not become ready at {worker.base_url}: {last_error}"
    )


def _stop_worker(worker: _Worker) -> None:
    _terminate_process(worker.process)


@pytest.fixture
def uvicorn_workers(db_url: str, tmp_path: Path) -> Iterator[tuple[_Worker, _Worker]]:
    worker_a: _Worker | None = None
    worker_b: _Worker | None = None
    try:
        worker_a = _start_worker(
            db_url=db_url,
            log_dir=tmp_path,
            name="worker-a",
        )
        worker_b = _start_worker(
            db_url=db_url,
            log_dir=tmp_path,
            name="worker-b",
        )
        # The health route proves lifespan completed; give each relay
        # listener one short beat to finish the async LISTEN registration
        # before the first NOTIFY is sent.
        time.sleep(0.5)
        yield worker_a, worker_b
    finally:
        if worker_b is not None:
            _stop_worker(worker_b)
        if worker_a is not None:
            _stop_worker(worker_a)


def _next_sse_frame(
    lines: Iterator[str],
    *,
    timeout: float = 5.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    frame_lines: list[str] = []
    while time.monotonic() < deadline:
        try:
            line = next(lines)
        except httpx.ReadTimeout:
            continue
        if line == "":
            if not frame_lines:
                continue
            event_name: str | None = None
            data: str | None = None
            for frame_line in frame_lines:
                if frame_line.startswith("event: "):
                    event_name = frame_line[len("event: ") :]
                elif frame_line.startswith("data: "):
                    data = frame_line[len("data: ") :]
            return {
                "event": event_name,
                "data": json.loads(data) if data is not None else None,
                "raw": tuple(frame_lines),
            }
        if line.startswith(":"):
            continue
        frame_lines.append(line)
    raise AssertionError(f"timed out waiting for SSE frame; partial={frame_lines!r}")


def _wait_for_retry(lines: Iterator[str], *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = next(lines)
        except httpx.ReadTimeout:
            continue
        if line.startswith("retry:"):
            return
    raise AssertionError("SSE stream did not emit retry hint")


def _assert_cross_worker_frame(
    *,
    worker_a: _Worker,
    worker_b: _Worker,
    publish_path: str,
    event_name: str,
    expected_field: str,
    expected_value: str,
) -> None:
    with (
        httpx.Client(timeout=httpx.Timeout(5.0, read=0.25)) as client,
        client.stream("GET", f"{worker_b.base_url}/w/{_SLUG}/events") as stream,
    ):
        stream.raise_for_status()
        lines = stream.iter_lines()
        _wait_for_retry(lines)

        response = client.post(f"{worker_a.base_url}{publish_path}")
        response.raise_for_status()

        frame = _next_sse_frame(lines)
        assert frame["event"] == event_name
        payload = frame["data"]
        assert isinstance(payload, dict)
        assert payload["kind"] == event_name
        assert payload["workspace_id"] == _WORKSPACE_ID
        assert payload[expected_field] == expected_value


def test_notification_created_published_on_worker_a_reaches_worker_b_sse(
    uvicorn_workers: tuple[_Worker, _Worker],
) -> None:
    worker_a, worker_b = uvicorn_workers

    _assert_cross_worker_frame(
        worker_a=worker_a,
        worker_b=worker_b,
        publish_path="/test/publish/notification-created",
        event_name="notification.created",
        expected_field="notification_id",
        expected_value="01HX00000000000000000NOT000",
    )


def test_time_shift_changed_published_on_worker_a_reaches_worker_b_sse(
    uvicorn_workers: tuple[_Worker, _Worker],
) -> None:
    worker_a, worker_b = uvicorn_workers

    _assert_cross_worker_frame(
        worker_a=worker_a,
        worker_b=worker_b,
        publish_path="/test/publish/time-shift-changed",
        event_name="time.shift.changed",
        expected_field="shift_id",
        expected_value="01HX00000000000000000SHF000",
    )
