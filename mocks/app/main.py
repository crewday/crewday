"""miployees — UI preview mocks (JSON API + SPA fallback).

Presentational only. Mutations are in-memory. A `role` cookie picks
employee vs manager; `/switch/<role>` toggles. `theme` cookie picks
light vs dark; `/theme/toggle` flips.

This module exposes:

- `/api/v1/*` — read/write JSON endpoints used by the Vite/React SPA
  under `mocks/web/`. Bodies are JSON, responses are JSON-serialised
  dataclasses. No Jinja templates anywhere.
- `/events` — Server-Sent Events stream emitting deterministic mock
  events so the SPA can prove its SSE + invalidation wiring.
- `/switch/<role>`, `/theme/toggle`, `/agent/sidebar/<state>` —
  cookie-setting endpoints preserved for atomicity (the server is
  authoritative for the preference cookie).
- SPA catch-all — any other GET falls through to
  `mocks/web/dist/index.html`, so deep-linking (/today, /dashboard, …)
  works in production.
- `/healthz`, `/readyz`, `/metrics` — unchanged.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from fastapi import Body, FastAPI, Request, Response
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from . import mock_data as md


BASE_DIR = Path(__file__).resolve().parent
# The SPA build lives outside the Python package so it can be produced
# by a separate Docker stage. In dev, Vite serves /src/* and proxies
# unknown paths here; in prod the Dockerfile copies dist/ to this path.
WEB_DIST = BASE_DIR.parent / "web" / "dist"

app = FastAPI(title="miployees mocks", docs_url=None, redoc_url=None, openapi_url=None)


ROLE_COOKIE = "miployees_role"
THEME_COOKIE = "miployees_theme"
AGENT_COLLAPSED_COOKIE = "miployees_agent_collapsed"
VALID_ROLES = {"employee", "manager"}
VALID_THEMES = {"light", "dark"}


def current_role(request: Request) -> str:
    r = request.cookies.get(ROLE_COOKIE)
    return r if r in VALID_ROLES else "employee"


def current_theme(request: Request) -> str:
    t = request.cookies.get(THEME_COOKIE)
    return t if t in VALID_THEMES else "light"


# ── JSON encoding helpers ─────────────────────────────────────────────

def _encode(obj: Any) -> Any:
    """Recursively serialise dataclasses + datetimes for JSONResponse.

    FastAPI's default encoder handles dataclasses but chokes on datetime
    values inside `dict` fields (e.g. `HOUSEHOLD_SETTINGS`); this keeps
    the output predictable for the SPA.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _encode(v) for k, v in asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_encode(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, time):
        return obj.isoformat(timespec="minutes")
    return obj


def ok(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(_encode(payload), status_code=status_code)


# ── SSE hub ───────────────────────────────────────────────────────────

class _EventHub:
    """In-process pub/sub for SSE. One queue per subscriber.

    Writes piggyback on regular HTTP mutations (`/api/v1/*` POSTs) so
    every connected SPA sees the change without an extra round-trip.
    A background ticker also emits a `tick` every 25s so the connection
    stays alive behind proxies.
    """

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[tuple[str, str]]] = set()

    def subscribe(self) -> asyncio.Queue[tuple[str, str]]:
        q: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=64)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[tuple[str, str]]) -> None:
        self._subs.discard(q)

    def publish(self, event: str, data: Any) -> None:
        payload = json.dumps(_encode(data))
        for q in list(self._subs):
            try:
                q.put_nowait((event, payload))
            except asyncio.QueueFull:
                # Slow subscriber; drop it so fast subscribers don't stall.
                self._subs.discard(q)


hub = _EventHub()


# ── Health / ops ──────────────────────────────────────────────────────

@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    return {"ok": True, "checks": {"db": "ok", "redis": "ok", "llm": "ok"}}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return (
        "# HELP miployees_tasks_completed_total Total tasks completed\n"
        "# TYPE miployees_tasks_completed_total counter\n"
        'miployees_tasks_completed_total{property="Villa Sud"} 1\n'
        'miployees_tasks_pending{property="Villa Sud"} 4\n'
        "miployees_shift_active 1\n"
    )


# ── Preference endpoints (server-authoritative cookies) ───────────────

@app.get("/switch/{role}")
def switch_role(role: str) -> Response:
    if role not in VALID_ROLES:
        return JSONResponse({"ok": False}, status_code=400)
    resp = JSONResponse({"ok": True, "role": role})
    resp.set_cookie(ROLE_COOKIE, role, max_age=60 * 60 * 24 * 30, samesite="lax")
    return resp


@app.post("/theme/toggle")
@app.get("/theme/toggle")
def theme_toggle(request: Request) -> Response:
    new_theme = "dark" if current_theme(request) == "light" else "light"
    resp = JSONResponse({"ok": True, "theme": new_theme})
    resp.set_cookie(THEME_COOKIE, new_theme, max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@app.post("/agent/sidebar/{state}")
def agent_sidebar_set(state: str) -> Response:
    if state not in {"open", "collapsed"}:
        return JSONResponse({"ok": False}, status_code=400)
    resp = JSONResponse({"ok": True, "state": state})
    resp.set_cookie(
        AGENT_COLLAPSED_COOKIE,
        "1" if state == "collapsed" else "0",
        max_age=60 * 60 * 24 * 365,
        samesite="lax",
    )
    return resp


# ══════════════════════════════════════════════════════════════════════
# JSON API — reads
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/v1/me")
def api_me(request: Request) -> Response:
    emp = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    return ok({
        "role": current_role(request),
        "theme": current_theme(request),
        "agent_sidebar_collapsed": request.cookies.get(AGENT_COLLAPSED_COOKIE) == "1",
        "employee": emp,
        "manager_name": md.DEFAULT_MANAGER_NAME,
        "today": md.TODAY,
        "now": md.NOW,
    })


@app.get("/api/v1/properties")
def api_properties() -> Response:
    return ok(md.PROPERTIES)


@app.get("/api/v1/properties/{pid}")
def api_property(pid: str) -> Response:
    prop = md.property_by_id(pid)
    return ok({
        "property": prop,
        "property_tasks": [t for t in md.TASKS if t.property_id == pid],
        "stays": md.stays_for_property(pid),
        "inventory": md.inventory_for_property(pid),
        "instructions": [i for i in md.INSTRUCTIONS if i.property_id == pid or i.scope == "global"],
        "closures": md.closures_for_property(pid),
    })


@app.get("/api/v1/employees")
def api_employees() -> Response:
    return ok(md.EMPLOYEES)


@app.get("/api/v1/employees/{eid}")
def api_employee(eid: str) -> Response:
    emp = md.employee_by_id(eid)
    return ok({
        "subject": emp,
        "subject_tasks": md.tasks_for_employee(eid),
        "subject_expenses": md.expenses_for_employee(eid),
        "subject_leaves": md.leaves_for_employee(eid),
        "subject_payslips": md.payslips_for_employee(eid),
    })


@app.get("/api/v1/employees/{eid}/leaves")
def api_employee_leaves(eid: str) -> Response:
    return ok({"subject": md.employee_by_id(eid), "leaves": md.leaves_for_employee(eid)})


@app.get("/api/v1/tasks")
def api_tasks() -> Response:
    return ok(md.TASKS)


@app.get("/api/v1/tasks/{tid}")
def api_task(tid: str) -> Response:
    task = md.task_by_id(tid)
    if task is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return ok({
        "task": task,
        "property": md.property_by_id(task.property_id),
        "instructions": md.instructions_for_task(task),
    })


@app.get("/api/v1/today")
def api_today(request: Request) -> Response:
    emp = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    tasks = sorted(md.tasks_for_employee(emp.id), key=lambda t: t.scheduled_start)
    today_tasks = [t for t in tasks if t.scheduled_start.date() == md.TODAY]
    now_task = next((t for t in today_tasks if t.status in {"pending", "in_progress"}), None)
    upcoming = [t for t in today_tasks if t is not now_task and t.status in {"pending", "in_progress"}]
    completed = [t for t in today_tasks if t.status == "completed"]
    _ = request  # quiet linters
    return ok({"now_task": now_task, "upcoming": upcoming, "completed": completed,
               "properties": md.PROPERTIES})


@app.get("/api/v1/week")
def api_week() -> Response:
    emp = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    return ok({
        "tasks": sorted(md.tasks_for_employee(emp.id), key=lambda t: t.scheduled_start),
        "properties": md.PROPERTIES,
    })


@app.get("/api/v1/dashboard")
def api_dashboard() -> Response:
    on_shift = [e for e in md.EMPLOYEES if e.clocked_in_at]
    today_tasks = [t for t in md.TASKS if t.scheduled_start.date() == md.TODAY]
    by_status = {
        "completed":   [t for t in today_tasks if t.status == "completed"],
        "in_progress": [t for t in today_tasks if t.status == "in_progress"],
        "pending":     [t for t in today_tasks if t.status == "pending"],
    }
    return ok({
        "on_shift": on_shift,
        "by_status": by_status,
        "pending_approvals": md.APPROVALS,
        "pending_expenses": [x for x in md.EXPENSES if x.status == "submitted"],
        "pending_leaves": [lv for lv in md.LEAVES if lv.approved_at is None],
        "open_issues": [i for i in md.ISSUES if i.status != "resolved"],
        "stays_today": [s for s in md.STAYS if s.check_in <= md.TODAY <= s.check_out],
        "properties": md.PROPERTIES,
        "employees": md.EMPLOYEES,
    })


@app.get("/api/v1/expenses")
def api_expenses(mine: bool = False) -> Response:
    if mine:
        return ok(md.expenses_for_employee(md.DEFAULT_EMPLOYEE_ID))
    return ok(md.EXPENSES)


@app.get("/api/v1/issues")
def api_issues() -> Response:
    return ok(md.ISSUES)


@app.get("/api/v1/stays")
def api_stays() -> Response:
    return ok({
        "stays": sorted(md.STAYS, key=lambda s: s.check_in),
        "closures": md.CLOSURES,
        "leaves": [lv for lv in md.LEAVES if lv.approved_at is not None],
    })


@app.get("/api/v1/property_closures")
def api_property_closures(property_id: str) -> Response:
    return ok({
        "property": md.property_by_id(property_id),
        "closures": md.closures_for_property(property_id),
        "stays": md.stays_for_property(property_id),
    })


@app.get("/api/v1/templates")
def api_templates() -> Response:
    return ok(md.TEMPLATES)


@app.get("/api/v1/schedules")
def api_schedules() -> Response:
    return ok({
        "schedules": md.SCHEDULES,
        "templates_by_id": {t.id: t for t in md.TEMPLATES},
    })


@app.get("/api/v1/instructions")
def api_instructions() -> Response:
    return ok(md.INSTRUCTIONS)


@app.get("/api/v1/instructions/{iid}")
def api_instruction(iid: str) -> Response:
    instr = next((i for i in md.INSTRUCTIONS if i.id == iid), None)
    if instr is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return ok(instr)


@app.get("/api/v1/inventory")
def api_inventory() -> Response:
    return ok(md.INVENTORY)


@app.get("/api/v1/payslips")
def api_payslips() -> Response:
    current = [p for p in md.PAYSLIPS if p.period_starts.month == 4]
    previous = [p for p in md.PAYSLIPS if p.period_starts.month == 3]
    return ok({"current": current, "previous": previous})


@app.get("/api/v1/leaves")
def api_leaves() -> Response:
    return ok({
        "pending": [lv for lv in md.LEAVES if lv.approved_at is None],
        "approved": [lv for lv in md.LEAVES if lv.approved_at is not None],
    })


@app.get("/api/v1/approvals")
def api_approvals() -> Response:
    return ok(md.APPROVALS)


@app.get("/api/v1/audit")
def api_audit() -> Response:
    return ok(md.AUDIT)


@app.get("/api/v1/webhooks")
def api_webhooks() -> Response:
    return ok(md.WEBHOOKS)


@app.get("/api/v1/llm/assignments")
def api_llm_assignments() -> Response:
    total_spent = sum(a.spent_24h_usd for a in md.LLM_ASSIGNMENTS)
    total_budget = sum(a.daily_budget_usd for a in md.LLM_ASSIGNMENTS)
    total_calls = sum(a.calls_24h for a in md.LLM_ASSIGNMENTS)
    return ok({
        "assignments": md.LLM_ASSIGNMENTS,
        "total_spent": total_spent,
        "total_budget": total_budget,
        "total_calls": total_calls,
    })


@app.get("/api/v1/llm/calls")
def api_llm_calls() -> Response:
    return ok(md.LLM_CALLS)


@app.get("/api/v1/settings")
def api_settings() -> Response:
    return ok({
        "meta": md.WORKSPACE_META,
        "defaults": md.WORKSPACE_SETTINGS,
        "policy": md.WORKSPACE_POLICY,
    })


@app.get("/api/v1/settings/catalog")
def api_settings_catalog() -> Response:
    return ok(md.SETTINGS_CATALOG)


@app.get("/api/v1/settings/resolved")
def api_settings_resolved(entity_kind: str = "", entity_id: str = "") -> Response:
    prop_override: dict[str, Any] | None = None
    emp_override: dict[str, Any] | None = None
    task_override: dict[str, Any] | None = None
    if entity_kind == "property":
        prop = md.property_by_id(entity_id)
        prop_override = prop.settings_override
    elif entity_kind == "employee":
        emp = md.employee_by_id(entity_id)
        emp_override = emp.settings_override
        # Also pick the first property for context.
        if emp.properties:
            try:
                prop = md.property_by_id(emp.properties[0])
                prop_override = prop.settings_override
            except StopIteration:
                pass
    elif entity_kind == "task":
        task = md.task_by_id(entity_id)
        if task:
            task_override = task.settings_override
            try:
                prop = md.property_by_id(task.property_id)
                prop_override = prop.settings_override
            except StopIteration:
                pass
            try:
                emp = md.employee_by_id(task.assignee_id)
                emp_override = emp.settings_override
            except StopIteration:
                pass
    resolved = md.resolve_settings(
        md.WORKSPACE_SETTINGS,
        property_override=prop_override,
        employee_override=emp_override,
        task_override=task_override,
    )
    return ok({"entity_kind": entity_kind, "entity_id": entity_id, "settings": resolved})


@app.get("/api/v1/properties/{pid}/settings")
def api_property_settings(pid: str) -> Response:
    prop = md.property_by_id(pid)
    resolved = md.resolve_settings(md.WORKSPACE_SETTINGS, property_override=prop.settings_override)
    return ok({"overrides": prop.settings_override, "resolved": resolved})


@app.get("/api/v1/employees/{eid}/settings")
def api_employee_settings(eid: str) -> Response:
    emp = md.employee_by_id(eid)
    prop_override: dict[str, Any] | None = None
    if emp.properties:
        try:
            prop = md.property_by_id(emp.properties[0])
            prop_override = prop.settings_override
        except StopIteration:
            pass
    resolved = md.resolve_settings(
        md.WORKSPACE_SETTINGS,
        property_override=prop_override,
        employee_override=emp.settings_override,
    )
    return ok({"overrides": emp.settings_override, "resolved": resolved})


@app.get("/api/v1/agent/employee/log")
def api_agent_employee_log() -> Response:
    return ok(md.EMPLOYEE_CHAT_LOG)


@app.get("/api/v1/agent/manager/log")
def api_agent_manager_log() -> Response:
    return ok(md.MANAGER_AGENT_LOG)


@app.get("/api/v1/agent/manager/actions")
def api_agent_manager_actions() -> Response:
    return ok(md.MANAGER_AGENT_ACTIONS)


@app.get("/api/v1/guest")
def api_guest() -> Response:
    stay = md.stay_by_id(md.GUEST_STAY_ID)
    turnover_task = next((t for t in md.TASKS if t.turnover_bundle_id == "tb-apt-3b-18"), None)
    guest_checklist = [c for c in (turnover_task.checklist if turnover_task else []) if c.get("guest_visible")]
    return ok({
        "stay": stay,
        "property": md.property_by_id(stay.property_id) if stay else None,
        "guest_checklist": guest_checklist,
    })


@app.get("/api/v1/history")
def api_history(tab: str = "tasks") -> Response:
    if tab not in {"tasks", "chats", "expenses", "leaves"}:
        tab = "tasks"
    emp = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    return ok({
        "tab": tab,
        "tasks": [t for t in md.tasks_for_employee(emp.id) if t.status in {"completed", "skipped"}],
        "expenses": [x for x in md.expenses_for_employee(emp.id) if x.status in {"approved", "reimbursed", "rejected"}],
        "leaves": [lv for lv in md.leaves_for_employee(emp.id) if lv.approved_at is not None and lv.ends_on < md.TODAY],
        "chats": md.HISTORY.get("chats", []),
    })


# ══════════════════════════════════════════════════════════════════════
# JSON API — writes
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/v1/shifts/toggle")
def api_shifts_toggle() -> Response:
    emp = md.employee_by_id(md.DEFAULT_EMPLOYEE_ID)
    emp.clocked_in_at = None if emp.clocked_in_at else md.NOW
    return ok(emp)


@app.post("/api/v1/tasks/{tid}/check/{idx}")
def api_task_check(tid: str, idx: int) -> Response:
    task = md.task_by_id(tid)
    if task is None or idx < 0 or idx >= len(task.checklist):
        return JSONResponse({"detail": "not found"}, status_code=404)
    task.checklist[idx]["done"] = not task.checklist[idx].get("done", False)
    hub.publish("task.updated", {"task": task})
    return ok(task)


@app.post("/api/v1/tasks/{tid}/complete")
def api_task_complete(tid: str) -> Response:
    task = md.task_by_id(tid)
    if task is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    task.status = "completed"
    hub.publish("task.updated", {"task": task})
    return ok(task)


@app.post("/api/v1/tasks/{tid}/skip")
def api_task_skip(tid: str, payload: dict[str, Any] = Body(default_factory=dict)) -> Response:
    task = md.task_by_id(tid)
    if task is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    task.status = "skipped"
    _ = payload.get("reason")  # preserved in a real system; ignored here
    hub.publish("task.updated", {"task": task})
    return ok(task)


@app.post("/api/v1/expenses")
def api_expenses_create(payload: dict[str, Any] = Body(...)) -> Response:
    try:
        cents = int(round(float(payload.get("amount", 0)) * 100))
    except (TypeError, ValueError):
        cents = 0
    x = md.Expense(
        id=f"x-{len(md.EXPENSES) + 1}",
        employee_id=md.DEFAULT_EMPLOYEE_ID,
        amount_cents=cents,
        currency="EUR",
        merchant=str(payload.get("merchant") or "Unknown"),
        submitted_at=datetime.now(),
        status="submitted",
        note=str(payload.get("note") or ""),
        ocr_confidence=None,
    )
    md.EXPENSES.insert(0, x)
    return ok(x, status_code=201)


@app.post("/api/v1/expenses/{xid}/{decision}")
def api_expenses_decide(xid: str, decision: str) -> Response:
    mapping = {"approve": "approved", "reject": "rejected", "reimburse": "reimbursed"}
    new_status = mapping.get(decision)
    if new_status is None:
        return JSONResponse({"detail": "bad decision"}, status_code=400)
    for x in md.EXPENSES:
        if x.id == xid:
            x.status = new_status  # type: ignore[assignment]
            hub.publish("expense.decided", {"id": xid, "status": new_status})
            return ok(x)
    return JSONResponse({"detail": "not found"}, status_code=404)


@app.post("/api/v1/issues")
def api_issues_create(payload: dict[str, Any] = Body(...)) -> Response:
    issue = md.Issue(
        id=f"iss-{len(md.ISSUES) + 1}",
        reported_by=md.DEFAULT_EMPLOYEE_ID,
        property_id=str(payload.get("property_id") or md.PROPERTIES[0].id),
        area=str(payload.get("area") or "—"),
        severity=str(payload.get("severity") or "medium"),  # type: ignore[arg-type]
        category=str(payload.get("category") or "other"),   # type: ignore[arg-type]
        title=str(payload.get("title") or "Untitled"),
        body=str(payload.get("body") or ""),
        reported_at=datetime.now(),
        status="open",
    )
    md.ISSUES.insert(0, issue)
    return ok(issue, status_code=201)


@app.post("/api/v1/leaves/{lid}/{decision}")
def api_leaves_decide(lid: str, decision: str) -> Response:
    for lv in md.LEAVES:
        if lv.id == lid:
            if decision == "approve":
                lv.approved_at = datetime.now()
                return ok(lv)
            if decision == "reject":
                md.LEAVES.remove(lv)
                return ok({"ok": True, "id": lid})
            return JSONResponse({"detail": "bad decision"}, status_code=400)
    return JSONResponse({"detail": "not found"}, status_code=404)


@app.post("/api/v1/approvals/{aid}/{decision}")
def api_approvals_decide(aid: str, decision: str) -> Response:
    if decision not in {"approve", "reject"}:
        return JSONResponse({"detail": "bad decision"}, status_code=400)
    md.APPROVALS[:] = [a for a in md.APPROVALS if a.id != aid]
    hub.publish("approval.decided", {"id": aid, "decision": decision})
    return ok({"ok": True, "id": aid, "decision": decision})


@app.post("/api/v1/agent/employee/message")
def api_agent_employee_message(payload: dict[str, Any] = Body(...)) -> Response:
    body = str(payload.get("body") or "").strip()[:500]
    if not body:
        return JSONResponse({"detail": "empty"}, status_code=400)
    msg = md.AgentMessage(at=datetime.now(), kind="user", body=body)
    md.EMPLOYEE_CHAT_LOG.append(msg)
    hub.publish("agent.message.appended", {"scope": "employee", "message": msg})
    return ok(msg)


@app.post("/api/v1/agent/manager/message")
def api_agent_manager_message(payload: dict[str, Any] = Body(...)) -> Response:
    body = str(payload.get("body") or "").strip()[:500]
    if not body:
        return JSONResponse({"detail": "empty"}, status_code=400)
    msg = md.AgentMessage(at=datetime.now(), kind="user", body=body)
    md.MANAGER_AGENT_LOG.append(msg)
    hub.publish("agent.message.appended", {"scope": "manager", "message": msg})
    return ok(msg)


@app.post("/api/v1/agent/manager/action/{aid}/{decision}")
def api_agent_manager_action(aid: str, decision: str) -> Response:
    action = next((a for a in md.MANAGER_AGENT_ACTIONS if a.id == aid), None)
    if action is None or decision not in {"approve", "deny"}:
        return JSONResponse({"detail": "bad request"}, status_code=400)
    md.MANAGER_AGENT_ACTIONS[:] = [a for a in md.MANAGER_AGENT_ACTIONS if a.id != aid]
    verb = "Approved" if decision == "approve" else "Denied"
    user_msg = md.AgentMessage(at=datetime.now(), kind="user", body=f"{verb}: {action.title}")
    md.MANAGER_AGENT_LOG.append(user_msg)
    hub.publish("agent.message.appended", {"scope": "manager", "message": user_msg})
    if decision == "approve":
        agent_msg = md.AgentMessage(
            at=datetime.now(), kind="agent",
            body=f"Done — {action.title.lower()} is in the audit log.",
        )
        md.MANAGER_AGENT_LOG.append(agent_msg)
        hub.publish("agent.message.appended", {"scope": "manager", "message": agent_msg})
    return ok({"ok": True, "id": aid, "decision": decision})


@app.post("/api/v1/chat/action/{idx}/{decision}")
def api_chat_action_decide(idx: int, decision: str) -> Response:
    if idx < 0 or idx >= len(md.EMPLOYEE_CHAT_LOG) or decision not in {"approve", "details"}:
        return JSONResponse({"detail": "bad request"}, status_code=400)
    msg = md.EMPLOYEE_CHAT_LOG[idx]
    if msg.kind != "action":
        return JSONResponse({"detail": "not an action"}, status_code=400)
    if decision == "approve":
        md.EMPLOYEE_CHAT_LOG[idx] = md.AgentMessage(
            at=msg.at, kind="agent", body=f"{msg.body} — approved.",
        )
    else:
        md.EMPLOYEE_CHAT_LOG.append(md.AgentMessage(
            at=datetime.now(), kind="agent",
            body="Here are the details — receipt attached, merchant Carrefour, €12.40.",
        ))
    return ok(md.EMPLOYEE_CHAT_LOG)


# ══════════════════════════════════════════════════════════════════════
# SSE
# ══════════════════════════════════════════════════════════════════════

def _sse_format(event: str, data: str) -> bytes:
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")


async def _tick_loop() -> None:
    while True:
        await asyncio.sleep(25)
        hub.publish("tick", {"now": datetime.now()})


_tick_task: asyncio.Task[None] | None = None


@app.on_event("startup")
async def _on_startup() -> None:
    global _tick_task
    loop = asyncio.get_running_loop()
    _tick_task = loop.create_task(_tick_loop())


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    if _tick_task is not None:
        _tick_task.cancel()


@app.get("/events")
async def events_stream(request: Request) -> StreamingResponse:
    q = hub.subscribe()

    async def stream() -> AsyncIterator[bytes]:
        # Initial handshake so EventSource considers the stream open.
        yield _sse_format("tick", json.dumps({"now": datetime.now().isoformat()}))
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=30)
                except asyncio.TimeoutError:
                    # Keep-alive comment; most proxies drop idle SSE at 60s.
                    yield b": keep-alive\n\n"
                    continue
                yield _sse_format(event, data)
        finally:
            hub.unsubscribe(q)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


# ══════════════════════════════════════════════════════════════════════
# SPA fallback
# ══════════════════════════════════════════════════════════════════════

# Mount built assets (JS, CSS, fonts) under Vite's default /assets path.
if (WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIST / "assets")), name="assets")


_SPA_PASSTHROUGH: Iterable[str] = (
    "/api",
    "/events",
    "/switch",
    "/theme",
    "/agent/sidebar",
    "/healthz",
    "/readyz",
    "/metrics",
    "/assets",
)


@app.get("/{full_path:path}")
def spa_fallback(full_path: str) -> Response:
    """Serve the SPA's index.html for any non-API GET.

    FastAPI matches specific routes first, so `/api/v1/...`, `/events`,
    and cookie endpoints never reach here. We still guard a few prefix
    checks in case of path weirdness.
    """
    path = "/" + full_path
    for prefix in _SPA_PASSTHROUGH:
        if path.startswith(prefix):
            return JSONResponse({"detail": "not found"}, status_code=404)

    # Top-level static files (favicon, grain.svg, manifest) copied by
    # Vite directly under dist/.
    candidate = WEB_DIST / full_path
    if full_path and candidate.is_file() and WEB_DIST in candidate.resolve().parents:
        return FileResponse(candidate)

    index = WEB_DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    # Until the SPA has been built (e.g. in dev without a build), return
    # a stub so curl/healthcheck can distinguish.
    return PlainTextResponse(
        "SPA bundle not built. Run `npm --prefix mocks/web run build` or "
        "use the `dev` compose profile with Vite on :5173.",
        status_code=503,
    )
