"""GA journey 5: delegated CLI task lifecycle plus HITL approval.

Spec: ``docs/specs/17-testing-quality.md`` §"End-to-end" journey 5,
``docs/specs/13-cli.md`` and ``docs/specs/11-llm-and-agents.md``
§"Agent action approval".
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
from playwright.sync_api import BrowserContext, expect

from app.auth.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.auth.session_cookie import DEV_SESSION_COOKIE_NAME
from tests.e2e._helpers.auth import login_with_dev_session

JOURNEY_SLUG_PREFIX: Final[str] = "e2e-agent-task-lifecycle"


def test_agent_cli_drives_task_lifecycle_and_manager_approves_action(
    context: BrowserContext,
    base_url: str,
    tmp_path: Path,
) -> None:
    run_id = secrets.token_hex(3)
    workspace_slug = f"{JOURNEY_SLUG_PREFIX}-{run_id}"
    email = f"{JOURNEY_SLUG_PREFIX}-{run_id}@dev.local"
    login = login_with_dev_session(
        context,
        base_url=base_url,
        email=email,
        workspace_slug=workspace_slug,
        role="owner",
    )
    api = _SessionApi(
        base_url=base_url,
        workspace_slug=workspace_slug,
        session_cookie=login.cookie_value,
    )
    me = api.get("/api/v1/me")
    actor_id = _require_str(me, "user_id")
    property_id = _ensure_property(api)
    role_id = _ensure_work_role(api)
    cli_token = api.post(
        f"/w/{workspace_slug}/api/v1/auth/tokens",
        {
            "label": f"GA journey 5 delegated CLI {secrets.token_hex(4)}",
            "delegate": True,
            "scopes": {},
            "expires_at_days": 1,
        },
    )["token"]
    if not isinstance(cli_token, str) or not cli_token:
        raise AssertionError("token mint response did not include plaintext token")

    cli = _CrewdayCli(
        base_url=base_url,
        workspace_slug=workspace_slug,
        token=cli_token,
        config_home=tmp_path / "xdg",
    )

    scheduled_for = (datetime.now(UTC) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    created = cli.run_json(
        "tasks",
        "create",
        "--body-file",
        _write_json(
            tmp_path / "task-create.json",
            {
                "title": f"GA journey 5 CLI task {secrets.token_hex(3)}",
                "property_id": property_id,
                "expected_role_id": role_id,
                "scheduled_for_local": scheduled_for,
                "duration_minutes": 30,
                "photo_evidence": "disabled",
                "is_personal": False,
            },
        ),
    )
    task_id = _require_str(created, "id")

    assigned = cli.run_json(
        "tasks",
        "assign",
        "--task-id",
        task_id,
        "--field",
        f"assignee_user_id={actor_id}",
    )
    assert assigned["assigned_user_id"] == actor_id

    completed = cli.run_json(
        "tasks", "complete", task_id, "--note", "Completed by GA journey 5"
    )
    assert completed["task_id"] == task_id
    assert completed["state"] == "completed"

    approval_task = cli.run_json(
        "tasks",
        "create",
        "--body-file",
        _write_json(
            tmp_path / "approval-task-create.json",
            {
                "title": f"GA journey 5 approval task {secrets.token_hex(3)}",
                "property_id": property_id,
                "expected_role_id": role_id,
                "scheduled_for_local": scheduled_for,
                "duration_minutes": 30,
                "photo_evidence": "disabled",
                "is_personal": False,
            },
        ),
    )
    approval_task_id = _require_str(approval_task, "id")
    api.put(f"/w/{workspace_slug}/api/v1/me/agent_approval_mode", {"mode": "strict"})
    pending_cancel = cli.run_expect_approval_pending(
        "tasks",
        "cancel",
        "--task-id",
        approval_task_id,
        "--field",
        "reason_md=manager_cancelled",
    )
    assert pending_cancel.returncode == 3

    queued = _pending_approval_for_tool(cli, "cancel_task", approval_task_id)
    approval_id = _require_str(queued, "id")
    assert queued["id"] == approval_id
    assert queued["status"] == "pending"

    page = context.new_page()
    page.goto(f"{base_url}/w/{workspace_slug}/approvals", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="Agent approvals")).to_be_visible()
    approval_row = page.locator(".approval", has_text=approval_id).or_(
        page.locator(".approval", has_text="cancel_task")
    )
    expect(approval_row.first).to_be_visible()
    approval_row.first.get_by_role("button", name="Approve").click()

    decided = _poll_approval(cli, approval_id, status="approved")
    assert decided["status"] == "approved"
    assert decided["result_json"]["mutated"] is True


class _SessionApi:
    def __init__(
        self, *, base_url: str, workspace_slug: str, session_cookie: str
    ) -> None:
        csrf = secrets.token_urlsafe(24)
        self._client = httpx.Client(
            base_url=base_url,
            cookies={
                DEV_SESSION_COOKIE_NAME: session_cookie,
                CSRF_COOKIE_NAME: csrf,
            },
            headers={CSRF_HEADER_NAME: csrf},
            timeout=15.0,
            follow_redirects=False,
        )
        self.workspace_slug = workspace_slug

    def get(self, path: str) -> dict[str, Any]:
        resp = self._client.get(path)
        _raise_api_error(resp, method="GET", path=path)
        body = resp.json()
        if not isinstance(body, dict):
            raise AssertionError(f"GET {path} returned non-object JSON: {body!r}")
        return body

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post(path, json=payload)
        _raise_api_error(resp, method="POST", path=path)
        body = resp.json()
        if not isinstance(body, dict):
            raise AssertionError(f"POST {path} returned non-object JSON: {body!r}")
        return body

    def put(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.put(path, json=payload)
        _raise_api_error(resp, method="PUT", path=path)
        body = resp.json()
        if not isinstance(body, dict):
            raise AssertionError(f"PUT {path} returned non-object JSON: {body!r}")
        return body


class _CrewdayCli:
    def __init__(
        self,
        *,
        base_url: str,
        workspace_slug: str,
        token: str,
        config_home: Path,
    ) -> None:
        self._env = {
            **os.environ,
            "XDG_CONFIG_HOME": str(config_home),
            "CREWDAY_PROFILE": "e2e",
            "CREWDAY_NO_COLOR": "1",
        }
        profile_dir = config_home / "crewday"
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "profiles.toml").write_text(
            textwrap.dedent(
                f"""
                default = "e2e"

                [profile.e2e]
                base_url = "{base_url}"
                token = "env:CREWDAY_E2E_CLI_TOKEN"
                default_workspace = "{workspace_slug}"
                output = "json"
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self._env["CREWDAY_E2E_CLI_TOKEN"] = token

    def run_json(self, *args: str) -> dict[str, Any]:
        proc = subprocess.run(
            ["uv", "run", "crewday", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=self._env,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"crewday {' '.join(args)} failed with exit {proc.returncode}\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        stdout = proc.stdout.strip()
        if "\n{" in stdout:
            stdout = "{" + stdout.rsplit("\n{", 1)[1]
        try:
            body = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"crewday {' '.join(args)} did not emit JSON: {stdout!r}"
            ) from exc
        if not isinstance(body, dict):
            raise AssertionError(
                f"crewday {' '.join(args)} emitted non-object JSON: {body!r}"
            )
        return body

    def run_expect_approval_pending(
        self, *args: str
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["uv", "run", "crewday", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=self._env,
        )
        if proc.returncode != 3:
            raise AssertionError(
                f"crewday {' '.join(args)} should have queued approval, "
                f"got exit {proc.returncode}\nstdout:\n{proc.stdout}"
                f"\nstderr:\n{proc.stderr}"
            )
        return proc


def _ensure_property(api: _SessionApi) -> str:
    created = api.post(
        f"/w/{api.workspace_slug}/api/v1/properties",
        {
            "name": f"GA Journey Villa {secrets.token_hex(3)}",
            "kind": "residence",
            "address": "1 Loopback Lane",
            "country": "US",
            "locale": "en-US",
            "default_currency": "USD",
            "timezone": "UTC",
            "tags_json": [],
            "welcome_defaults_json": {},
            "property_notes_md": "",
        },
    )
    return _require_str(created, "id")


def _ensure_work_role(api: _SessionApi) -> str:
    created = api.post(
        f"/w/{api.workspace_slug}/api/v1/work_roles",
        {
            "key": f"ga-journey-{secrets.token_hex(3)}",
            "name": "GA Journey Cleaner",
            "description_md": "",
            "default_settings_json": {},
            "icon_name": "clipboard-check",
        },
    )
    return _require_str(created, "id")


def _pending_approval_for_tool(
    cli: _CrewdayCli, tool_name: str, task_id: str
) -> dict[str, Any]:
    listed = cli.run_json("approvals", "list")
    rows = listed.get("data")
    if not isinstance(rows, list):
        raise AssertionError(f"approvals list returned malformed envelope: {listed!r}")
    for row in rows:
        if not isinstance(row, dict):
            continue
        action = row.get("action_json")
        if not isinstance(action, dict):
            continue
        tool_input = action.get("tool_input")
        if not isinstance(tool_input, dict):
            continue
        if (
            action.get("tool_name") == tool_name
            and tool_input.get("task_id") == task_id
        ):
            return row
    raise AssertionError(
        f"did not find pending {tool_name!r} approval for task {task_id!r}: {listed!r}"
    )


def _poll_approval(
    cli: _CrewdayCli, approval_id: str, *, status: str
) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = cli.run_json("approvals", "show", "--approval-request-id", approval_id)
        if last.get("status") == status:
            return last
        time.sleep(0.25)
    raise AssertionError(
        f"approval {approval_id!r} did not reach {status!r}; last={last!r}"
    )


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AssertionError(f"expected non-empty string {key!r} in {payload!r}")
    return value


def _raise_api_error(resp: httpx.Response, *, method: str, path: str) -> None:
    if resp.is_success:
        return
    raise AssertionError(
        f"{method} {path} failed with {resp.status_code}\nbody:\n{resp.text}"
    )
