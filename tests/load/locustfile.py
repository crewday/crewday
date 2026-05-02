"""Locust harness for the §17 crew.day load scenarios.

Run after seeding a local dev stack:

    uv run python scripts/seed_load.py
    locust -f tests/load/locustfile.py --headless -u 100 -r 10 --run-time 5m
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Final

from locust import HttpUser, between, events, task
from locust.env import Environment

DEFAULT_HOST: Final[str] = "http://127.0.0.1:8100"
DEFAULT_WORKSPACE_SLUG: Final[str] = "load"

CLOCK_IN_P95_BUDGET_MS: Final[int] = 500
OCCURRENCES_LIST_P95_BUDGET_MS: Final[int] = 250
# §00 does not pin an upload latency number; this harness keeps a named
# release gate until the spec grows an explicit budget.
PHOTO_UPLOAD_P95_BUDGET_MS: Final[int] = 2_000

_PNG_5_MIB: Final[bytes] = b"\x89PNG\r\n\x1a\n" + (b"\0" * (5 * 1024 * 1024 - 8))


@dataclass(frozen=True, slots=True)
class LoadConfig:
    """Environment-backed values shared by every Locust user."""

    host: str
    workspace_slug: str
    session_cookie: str | None
    bearer_token: str | None
    property_id: str | None
    worker_ids: tuple[str, ...]
    occurrence_ids: tuple[str, ...]
    list_limit: int

    @classmethod
    def from_env(cls) -> LoadConfig:
        return cls(
            host=os.environ.get("CREWDAY_LOAD_HOST", DEFAULT_HOST).rstrip("/"),
            workspace_slug=os.environ.get(
                "CREWDAY_LOAD_WORKSPACE", DEFAULT_WORKSPACE_SLUG
            ),
            session_cookie=os.environ.get("CREWDAY_LOAD_SESSION_COOKIE"),
            bearer_token=os.environ.get("CREWDAY_LOAD_BEARER_TOKEN"),
            property_id=os.environ.get("CREWDAY_LOAD_PROPERTY_ID"),
            worker_ids=_split_env("CREWDAY_LOAD_WORKER_IDS"),
            occurrence_ids=_split_env("CREWDAY_LOAD_OCCURRENCE_IDS"),
            list_limit=int(os.environ.get("CREWDAY_LOAD_LIST_LIMIT", "50")),
        )

    @property
    def api_prefix(self) -> str:
        return f"/w/{self.workspace_slug}/api/v1"

    @property
    def tasks_prefix(self) -> str:
        return f"{self.api_prefix}/tasks"

    @property
    def time_prefix(self) -> str:
        return f"{self.api_prefix}/time/shifts"

    @property
    def auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.session_cookie:
            headers["Cookie"] = self.session_cookie
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers


def _split_env(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


CONFIG = LoadConfig.from_env()


class CrewdayLoadUser(HttpUser):
    """Common authenticated workspace client."""

    host = CONFIG.host
    abstract = True
    wait_time = between(0.5, 2.0)

    @property
    def api_prefix(self) -> str:
        return CONFIG.api_prefix

    @property
    def tasks_prefix(self) -> str:
        return CONFIG.tasks_prefix

    @property
    def time_prefix(self) -> str:
        return CONFIG.time_prefix

    @property
    def headers(self) -> dict[str, str]:
        return CONFIG.auth_headers


class ClockingInUser(CrewdayLoadUser):
    """Workers clocking in/opening shifts."""

    @task
    def open_shift(self) -> None:
        worker_id = random.choice(CONFIG.worker_ids) if CONFIG.worker_ids else None
        payload = {
            "user_id": worker_id,
            "property_id": CONFIG.property_id,
            "source": "manual",
            "notes_md": "locust clock-in",
            "client_lat": None,
            "client_lon": None,
            "gps_accuracy_m": None,
        }
        with self.client.post(
            f"{self.time_prefix}/open",
            json=payload,
            headers=self.headers,
            name="clock_in",
            catch_response=True,
        ) as response:
            if response.status_code in {201, 409}:
                response.success()
            else:
                response.failure(f"unexpected status {response.status_code}")


class OccurrencesListUser(CrewdayLoadUser):
    """Paginate through a large task/occurrence history."""

    _next_cursor: str | None

    def on_start(self) -> None:
        self._next_cursor = None

    @task
    def list_occurrences(self) -> None:
        params: dict[str, str | int] = {"limit": CONFIG.list_limit}
        if CONFIG.property_id:
            params["property_id"] = CONFIG.property_id
        if self._next_cursor is not None:
            params["cursor"] = self._next_cursor
        with self.client.get(
            self.tasks_prefix,
            params=params,
            headers=self.headers,
            name="occurrences_list",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                payload = response.json()
                if bool(payload.get("has_more")):
                    next_cursor = payload.get("next_cursor")
                    self._next_cursor = (
                        next_cursor if isinstance(next_cursor, str) else None
                    )
                else:
                    self._next_cursor = None
                response.success()
            else:
                response.failure(f"unexpected status {response.status_code}")


class TurnoverDayUser(CrewdayLoadUser):
    """Turnover-day completions with photo evidence uploads."""

    @task
    def upload_photo_evidence(self) -> None:
        task_id = (
            random.choice(CONFIG.occurrence_ids) if CONFIG.occurrence_ids else "missing"
        )
        with self.client.post(
            f"{self.tasks_prefix}/{task_id}/evidence",
            data={"kind": "photo"},
            files={"file": ("evidence.png", _PNG_5_MIB, "image/png")},
            headers=self.headers,
            name="photo_upload",
            catch_response=True,
        ) as response:
            if response.status_code in {201, 409}:
                response.success()
            else:
                response.failure(f"unexpected status {response.status_code}")


_BUDGETS: Final[dict[tuple[str, str], int]] = {
    ("POST", "clock_in"): CLOCK_IN_P95_BUDGET_MS,
    ("GET", "occurrences_list"): OCCURRENCES_LIST_P95_BUDGET_MS,
    ("POST", "photo_upload"): PHOTO_UPLOAD_P95_BUDGET_MS,
}


def enforce_latency_budgets(environment: Environment, **_: object) -> None:
    """Fail headless/CI runs when a scenario breaches its p95 budget."""

    failures: list[str] = []
    for (method, name), budget_ms in _BUDGETS.items():
        stats = environment.stats.get(name, method)
        if stats.num_requests == 0:
            failures.append(f"{method} {name}: no requests recorded")
            continue
        p95 = stats.get_response_time_percentile(0.95)
        if p95 > budget_ms:
            failures.append(f"{method} {name}: p95 {p95:.0f}ms > {budget_ms}ms")
    if failures:
        environment.process_exit_code = 1
        for failure in failures:
            print(f"load budget failed: {failure}")


# Locust's event hook is untyped; keep the ignore at the third-party boundary.
events.quitting.add_listener(enforce_latency_budgets)  # type: ignore[no-untyped-call]
