// PLACEHOLDER — real impl lands in cd-qdsl. DO NOT USE FOR PRODUCTION
// DECISIONS.
//
// Copied verbatim from `mocks/web/src/lib/agentTyping.ts` so the
// AgentSidebar component port compiles. The hook is already final-shape;
// cd-qdsl's lib port just lifts it into the production module tree.
//
// §14 "Agent turn indicator" — subscribe to the typing cache flag
// written by the SSE dispatcher (see `lib/sse.ts`). Returns `true`
// while an agent turn is in flight for the given scope. The cache
// key is stable across components, so two mounts of the same chat
// surface stay in sync on a single boolean.

import { useQuery } from "@tanstack/react-query";
import { qk } from "./queryKeys";
import type { AgentTurnScope } from "@/types/api";
import type { AgentActivityState } from "./sse";

export function useAgentActivity(
  scope: AgentTurnScope,
  taskId?: string,
): AgentActivityState {
  const q = useQuery({
    queryKey: qk.agentTyping(scope, taskId),
    queryFn: (): AgentActivityState => ({ typing: false }),
    staleTime: Infinity,
    gcTime: Infinity,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  if (typeof q.data === "boolean") return { typing: q.data };
  return q.data ?? { typing: false };
}

export function useAgentTyping(scope: AgentTurnScope, taskId?: string): boolean {
  return useAgentActivity(scope, taskId).typing;
}
