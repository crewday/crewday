import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Navigate } from "react-router-dom";
import { activeWorkspaceGrantRole, useAuth } from "@/auth";
import { useWorkspace } from "@/context/WorkspaceContext";
import { fetchJson, toDisplayError } from "@/lib/api";
import {
  fetchApprovals,
  projectInlineApprovals,
  type InlineApprovalChannel,
} from "@/lib/approvals";
import { qk } from "@/lib/queryKeys";
import { useAgentActivity } from "@/lib/agentTyping";
import type { AgentAction, AgentMessage, AgentTurnScope } from "@/types/api";
import AgentActionCards from "@/components/chat/AgentActionCards";
import ChatLog from "@/components/chat/ChatLog";
import ChatComposer from "@/components/chat/ChatComposer";
import InlineErrorAlert from "@/components/InlineErrorAlert";
import { Loading } from "@/components/common";

interface ChatScopeConfig {
  scope: Extract<AgentTurnScope, "employee" | "manager">;
  logKey: ReturnType<typeof qk.agentEmployeeLog> | ReturnType<typeof qk.agentManagerLog>;
  logUrl: string;
  messageUrl: string;
}

function employeeChatConfig(): ChatScopeConfig {
  return {
    scope: "employee",
    logKey: qk.agentEmployeeLog(),
    logUrl: "/api/v1/agent/employee/log",
    messageUrl: "/api/v1/agent/employee/message",
  };
}

function chatScopeForGrantRole(grantRole: string | null): ChatScopeConfig | null {
  if (grantRole === "worker") {
    return employeeChatConfig();
  }
  if (grantRole === "manager" || grantRole === "admin") {
    return {
      scope: "manager",
      logKey: qk.agentManagerLog(),
      logUrl: "/api/v1/agent/manager/log",
      messageUrl: "/api/v1/agent/manager/message",
    };
  }
  return null;
}

export default function ChatPage() {
  const qc = useQueryClient();
  const [draft, setDraft] = useState("");
  const { user } = useAuth();
  const { workspaceId } = useWorkspace();
  const config = chatScopeForGrantRole(activeWorkspaceGrantRole(user, workspaceId));
  const activeConfig = config ?? employeeChatConfig();
  const activity = useAgentActivity(activeConfig.scope);
  // §11 "Inline approval UX" — the phone /chat surface is the full-screen
  // presentation of the role's rail. The /approvals list is server-scoped
  // to the caller (cd-uu806): whatever it returns is actionable, so the
  // query is always enabled for an embedded chat agent. Managers read desk
  // + own-conversation rows via `web_owner_sidebar`; workers read their own
  // agent's inline cards via `web_worker_chat`.
  const approvalChannel: InlineApprovalChannel =
    activeConfig.scope === "manager" ? "web_owner_sidebar" : "web_worker_chat";

  const q = useQuery({
    queryKey: activeConfig.logKey,
    queryFn: () => fetchJson<AgentMessage[]>(activeConfig.logUrl),
    enabled: Boolean(config),
  });

  // §11 "Inline approval UX" — cache the raw approval list ONCE under
  // qk.approvals() (the same key + queryFn the manager desk uses) and derive
  // this channel's cards client-side via `select`, so the phone chat surface
  // and the desk share one cache blob instead of clobbering each other with
  // divergent shapes (cd-tifg4).
  const approvalsQuery = useQuery({
    queryKey: qk.approvals(),
    queryFn: fetchApprovals,
    select: (data): AgentAction[] => projectInlineApprovals(data, approvalChannel),
    enabled: Boolean(config),
  });

  const send = useMutation({
    mutationFn: (body: string) =>
      fetchJson<AgentMessage>(activeConfig.messageUrl, {
        method: "POST", body: { body },
      }),
    onMutate: async (body) => {
      await qc.cancelQueries({ queryKey: activeConfig.logKey });
      const prev = qc.getQueryData<AgentMessage[]>(activeConfig.logKey) ?? [];
      const optimistic: AgentMessage = { at: new Date().toISOString(), kind: "user", body };
      qc.setQueryData<AgentMessage[]>(activeConfig.logKey, [...prev, optimistic]);
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(activeConfig.logKey, ctx.prev);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: activeConfig.logKey }),
  });

  // §11 HITL — approval decisions ride the shared `/approvals/{id}/{decision}`
  // contract keyed by the stable approval id (never the log index). No
  // explicit `onError`: the global mutation-error toast (queryClient
  // `MutationCache`) surfaces the failure, and adding a local handler would
  // suppress it.
  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "deny" }) =>
      fetchJson<unknown>(
        "/api/v1/approvals/" + id + "/" + (decision === "approve" ? "approve" : "deny"),
        { method: "POST" },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.approvals() });
      qc.invalidateQueries({ queryKey: activeConfig.logKey });
    },
  });

  const handleSubmit = (trimmed: string) => {
    send.mutate(trimmed);
    setDraft("");
  };

  if (!config) return <Navigate to="/" replace />;

  return (
    <>
      <section className="chat-screen">
        {q.isPending ? (
          <div className="chat-screen__state">
            <Loading />
          </div>
        ) : q.isError ? (
          <div className="chat-screen__state">
            <InlineErrorAlert error={toDisplayError(q.error)} />
            <button
              type="button"
              className="btn btn--secondary"
              onClick={() => void q.refetch()}
            >
              Retry
            </button>
          </div>
        ) : (
          <ChatLog
            messages={q.data}
            variant="screen"
            typing={activity.typing}
            activity={activity}
          />
        )}
        {approvalsQuery.data && (
          <AgentActionCards
            actions={approvalsQuery.data}
            onDecide={(id, decision) => decide.mutate({ id, decision })}
          />
        )}
      </section>

      <ChatComposer
        value={draft}
        onChange={setDraft}
        onSubmit={handleSubmit}
      />
    </>
  );
}
