// Single EventSource, shared for the whole app. The hook in
// SseContext subscribes on mount, routes events to TanStack Query
// invalidations or optimistic cache updates, and tears down on unmount.

import type { QueryClient } from "@tanstack/react-query";
import type { AgentMessage, SseEvent, Task, ExpenseStatus } from "@/types/api";
import { qk } from "./queryKeys";

type TypedEvent = { type: SseEvent["event"]; data: string };

export function startEventStream(client: QueryClient): () => void {
  if (typeof EventSource === "undefined") return () => undefined;
  const es = new EventSource("/events", { withCredentials: true });

  const handler = (evt: MessageEvent<string>): void => {
    dispatch(client, { type: (evt as unknown as { type: SseEvent["event"] }).type, data: evt.data });
  };

  const events: SseEvent["event"][] = [
    "tick",
    "agent.message.appended",
    "task.updated",
    "approval.resolved",
    "expense.decided",
  ];
  for (const ev of events) {
    es.addEventListener(ev, handler as EventListener);
  }

  return () => {
    for (const ev of events) {
      es.removeEventListener(ev, handler as EventListener);
    }
    es.close();
  };
}

function dispatch(client: QueryClient, evt: TypedEvent): void {
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
      const payload = data as { scope: "employee" | "manager"; message: AgentMessage };
      const key = payload.scope === "employee" ? qk.agentEmployeeLog() : qk.agentManagerLog();
      client.setQueryData<AgentMessage[]>(key, (prev) =>
        prev ? [...prev, payload.message] : [payload.message],
      );
      return;
    }
    case "task.updated": {
      const payload = data as { task: Task };
      client.setQueryData(qk.task(payload.task.id), payload.task);
      client.invalidateQueries({ queryKey: qk.tasks() });
      client.invalidateQueries({ queryKey: qk.today() });
      client.invalidateQueries({ queryKey: qk.dashboard() });
      return;
    }
    case "approval.resolved":
      client.invalidateQueries({ queryKey: qk.approvals() });
      client.invalidateQueries({ queryKey: qk.dashboard() });
      return;
    case "expense.decided": {
      const _payload = data as { id: string; status: ExpenseStatus };
      void _payload;
      client.invalidateQueries({ queryKey: qk.expenses("all") });
      client.invalidateQueries({ queryKey: qk.expenses("mine") });
      client.invalidateQueries({ queryKey: qk.dashboard() });
      return;
    }
  }
}
