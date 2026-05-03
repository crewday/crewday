// Single EventSource, shared for the whole app. The hook in
// SseContext subscribes on mount, routes events to TanStack Query
// invalidations or optimistic cache updates, and tears down on unmount.

import type { QueryClient } from "@tanstack/react-query";
import type {
  AgentMessage,
  AgentTurnScope,
  AssetAction,
  SseEvent,
  ExpenseStatus,
} from "@/types/api";
import { qk } from "./queryKeys";
import { withBase } from "./api";

type TypedEvent = { type: SseEvent["event"]; data: string };

// §14 "Agent turn indicator" — 60 s local safety net so a dropped
// `agent.turn.finished` can never leave the typing bubble stuck. Keyed
// per scope (per task for task-scoped threads) to match `qk.agentTyping`.
const TYPING_TIMEOUT_MS = 60_000;
const typingTimers = new Map<string, ReturnType<typeof setTimeout>>();

function typingKeySignature(scope: AgentTurnScope, taskId?: string): string {
  return scope === "task" && taskId ? `task:${taskId}` : scope;
}

function startTyping(client: QueryClient, scope: AgentTurnScope, taskId?: string): void {
  const sig = typingKeySignature(scope, taskId);
  client.setQueryData<boolean>(qk.agentTyping(scope, taskId), true);
  const prev = typingTimers.get(sig);
  if (prev) clearTimeout(prev);
  typingTimers.set(
    sig,
    setTimeout(() => {
      client.setQueryData<boolean>(qk.agentTyping(scope, taskId), false);
      typingTimers.delete(sig);
    }, TYPING_TIMEOUT_MS),
  );
}

function stopTyping(client: QueryClient, scope: AgentTurnScope, taskId?: string): void {
  const sig = typingKeySignature(scope, taskId);
  const prev = typingTimers.get(sig);
  if (prev) {
    clearTimeout(prev);
    typingTimers.delete(sig);
  }
  client.setQueryData<boolean>(qk.agentTyping(scope, taskId), false);
}

function clearAllTyping(client: QueryClient): void {
  for (const [sig, handle] of typingTimers) {
    clearTimeout(handle);
    const [prefix, taskId] = sig.split(":");
    if (prefix === "task" && taskId) {
      client.setQueryData<boolean>(qk.agentTyping("task", taskId), false);
    } else {
      client.setQueryData<boolean>(qk.agentTyping(prefix as AgentTurnScope), false);
    }
  }
  typingTimers.clear();
}

export function startEventStream(client: QueryClient): () => void {
  if (typeof EventSource === "undefined") return () => undefined;
  const es = new EventSource(withBase("/events"), { withCredentials: true });

  const handler = (evt: MessageEvent<string>): void => {
    dispatch(client, { type: (evt as unknown as { type: SseEvent["event"] }).type, data: evt.data });
  };

  // Every reconnect of the `EventSource` drops any stale typing state
  // from the previous session (§14 "Agent turn indicator" — clears on
  // SSE reconnect). `onopen` fires on first connect too; no-op then
  // since the timer map is empty.
  es.onopen = () => clearAllTyping(client);

  const events: SseEvent["event"][] = [
    "tick",
    "agent.message.appended",
    "agent.turn.started",
    "agent.turn.finished",
    "task.updated",
    "task.completed",
    "task.skipped",
    "task_template.upserted",
    "task_template.deleted",
    "approval.decided",
    "user_leave.upserted",
    "user_availability_override.upserted",
    "expense.approved",
    "expense.rejected",
    "expense.reimbursed",
    "asset_action.performed",
    "schedule_ruleset.upserted",
    "schedule_ruleset.deleted",
    "booking.created",
    "booking.amended",
    "booking.declined",
    "booking.approved",
    "booking.rejected",
    "booking.cancelled",
    "booking.reassigned",
    "llm.assignment.changed",
    "permission_group.upserted",
    "permission_group.deleted",
    "permission_group_member.added",
    "permission_group_member.removed",
    "permission_rule.upserted",
    "permission_rule.deleted",
    "role_grant.created",
    "role_grant.revoked",
  ];
  for (const ev of events) {
    es.addEventListener(ev, handler as EventListener);
  }

  return () => {
    for (const ev of events) {
      es.removeEventListener(ev, handler as EventListener);
    }
    es.close();
    clearAllTyping(client);
  };
}

export function dispatch(client: QueryClient, evt: TypedEvent): void {
  let data: unknown;
  try {
    data = JSON.parse(evt.data);
  } catch {
    return;
  }
  switch (evt.type) {
    case "tick":
      // heartbeat; nothing to do
      return;
    case "agent.message.appended": {
      const payload = data as {
        scope: AgentTurnScope;
        task_id?: string;
        message: AgentMessage;
      };
      const key =
        payload.scope === "task" && payload.task_id
          ? qk.agentTaskChat(payload.task_id)
          : payload.scope === "admin"
          ? qk.adminAgentLog()
          : payload.scope === "employee"
          ? qk.agentEmployeeLog()
          : qk.agentManagerLog();
      client.setQueryData<AgentMessage[]>(key, (prev) =>
        prev ? [...prev, payload.message] : [payload.message],
      );
      // A reply arriving means the turn resolved into a message;
      // drop the typing indicator on the same scope even if the
      // paired `agent.turn.finished` hasn't dispatched yet.
      stopTyping(client, payload.scope, payload.task_id);
      return;
    }
    case "agent.turn.started": {
      const payload = data as {
        scope: AgentTurnScope;
        task_id?: string;
        started_at: string;
      };
      startTyping(client, payload.scope, payload.task_id);
      return;
    }
    case "agent.turn.finished": {
      const payload = data as {
        scope: AgentTurnScope;
        task_id?: string;
        outcome: "replied" | "action" | "error" | "timeout";
      };
      stopTyping(client, payload.scope, payload.task_id);
      return;
    }
    case "task.updated":
    case "task.completed":
    case "task.skipped": {
      // The canonical events
      // (`app.events.types.{TaskUpdated,TaskCompleted,TaskSkipped}`)
      // carry `{task_id, ...}` only — never a rendered `Task` object
      // (cd-m0hz). Treat each kind as a pure invalidation signal:
      // invalidate the per-row detail key alongside the list / today /
      // dashboard surfaces, and any mounted page refetches via REST
      // under the normal per-row authz path.
      const payload = data as { task_id: string };
      client.invalidateQueries({ queryKey: qk.task(payload.task_id) });
      client.invalidateQueries({ queryKey: qk.tasks() });
      client.invalidateQueries({ queryKey: qk.today() });
      client.invalidateQueries({ queryKey: qk.dashboard() });
      return;
    }
    case "task_template.upserted":
      // cd-wyq5 — drop the catalog list so the manager template
      // surface refetches.
      client.invalidateQueries({ queryKey: qk.taskTemplates() });
      return;
    case "task_template.deleted":
      // cd-wyq5 — catalog list AND schedules: a deleted template
      // prunes previously-derived future occurrences from the worker
      // schedule view.
      client.invalidateQueries({ queryKey: qk.taskTemplates() });
      client.invalidateQueries({ queryKey: qk.schedules() });
      return;
    case "approval.decided":
      client.invalidateQueries({ queryKey: qk.approvals() });
      client.invalidateQueries({ queryKey: qk.dashboard() });
      return;
    case "user_leave.upserted":
      // §14 cd-93wp — worker self-create or manager edit of a
      // previously-decided leave row. `approval.decided` misses both
      // branches; refresh the worker schedule + the leaves list.
      // Schedule keys are `["my-schedule", from, to]`, so invalidate by
      // root prefix to catch every mounted window.
      client.invalidateQueries({ queryKey: ["my-schedule"] });
      client.invalidateQueries({ queryKey: qk.leaves() });
      return;
    case "user_availability_override.upserted":
      // §14 cd-93wp — same fan-out story as user_leave.upserted but
      // for availability overrides; the override list lives under
      // `qk.meOverrides()`.
      client.invalidateQueries({ queryKey: ["my-schedule"] });
      client.invalidateQueries({ queryKey: qk.meOverrides() });
      return;
    case "expense.approved":
    case "expense.rejected":
    case "expense.reimbursed": {
      const _payload = data as { id: string; status: ExpenseStatus };
      void _payload;
      client.invalidateQueries({ queryKey: qk.expenses("all") });
      client.invalidateQueries({ queryKey: qk.expenses("mine") });
      client.invalidateQueries({ queryKey: qk.dashboard() });
      return;
    }
    case "asset_action.performed": {
      const payload = data as { asset_id: string; action: AssetAction };
      client.invalidateQueries({ queryKey: qk.asset(payload.asset_id) });
      client.invalidateQueries({ queryKey: qk.assets() });
      return;
    }
    case "schedule_ruleset.upserted":
    case "schedule_ruleset.deleted":
      client.invalidateQueries({ queryKey: qk.scheduleRulesets() });
      client.invalidateQueries({ queryKey: ["scheduler-calendar"] });
      return;
    case "booking.created":
    case "booking.amended":
    case "booking.declined":
    case "booking.approved":
    case "booking.rejected":
    case "booking.cancelled":
    case "booking.reassigned":
      // §09 booking lifecycle. `/schedule` keys include a window
      // (`["my-schedule", from, to]`), so invalidate by the root
      // prefix to catch every currently-mounted window.
      client.invalidateQueries({ queryKey: ["my-schedule"] });
      client.invalidateQueries({ queryKey: qk.bookings() });
      client.invalidateQueries({ queryKey: qk.dashboard() });
      return;
    case "llm.assignment.changed":
      // §11 LLM router (`app/domain/llm/router.py`) drops its
      // workspace-scoped resolver cache on this event; the admin
      // `/admin/llm` graph reads `qk.adminLlmGraph()` for the
      // assignment chain + capability inheritance. Whole-workspace
      // invalidation matches the backend posture (the event payload
      // does not name the affected capability, so narrowing here
      // would miss inheritance ripples).
      client.invalidateQueries({ queryKey: qk.adminLlmGraph() });
      return;
    case "permission_group.upserted":
    case "permission_group.deleted":
    case "permission_group_member.added":
    case "permission_group_member.removed": {
      // §02 / §05 permissions catalog + roster. The Permissions page
      // reads `qk.permissionGroups(...)` for the group list,
      // `qk.permissionGroupMembers(gid)` for the per-group roster,
      // and `qk.permissionResolved(...)` for the live resolver verdict
      // (group membership feeds the §02 step 5 default-allow walk).
      // The payload carries FK ids only — re-fetch via REST under the
      // per-row authz path.
      const payload = data as { group_id?: string };
      client.invalidateQueries({ queryKey: ["permission_groups"] });
      if (payload.group_id) {
        client.invalidateQueries({
          queryKey: qk.permissionGroupMembers(payload.group_id),
        });
      } else {
        client.invalidateQueries({ queryKey: ["permission_group_members"] });
      }
      // Resolution depends on group capabilities + membership — drop
      // the entire `permissions/resolved/...` family rather than a
      // per-tuple key.
      client.invalidateQueries({ queryKey: ["permissions", "resolved"] });
      return;
    }
    case "permission_rule.upserted":
    case "permission_rule.deleted":
      // §02 step 3+4 — rule writes can flip the resolver verdict.
      client.invalidateQueries({ queryKey: ["permission_rules"] });
      client.invalidateQueries({ queryKey: ["permissions", "resolved"] });
      return;
    case "role_grant.created":
    case "role_grant.revoked":
      // §05 — role grants seed the §02 step 5 default-allow walk and
      // the owners-group derived membership; both feed the resolver,
      // and the Permissions page surfaces the affected user's grants
      // via the same group catalog.
      client.invalidateQueries({ queryKey: ["permission_groups"] });
      client.invalidateQueries({ queryKey: ["permissions", "resolved"] });
      return;
  }
}
