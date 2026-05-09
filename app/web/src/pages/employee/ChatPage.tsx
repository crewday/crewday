import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Navigate } from "react-router-dom";
import { activeWorkspaceGrantRole, useAuth } from "@/auth";
import { useWorkspace } from "@/context/WorkspaceContext";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useAgentTyping } from "@/lib/agentTyping";
import type { AgentMessage, AgentTurnScope } from "@/types/api";
import ChatLog from "@/components/chat/ChatLog";
import ChatComposer from "@/components/chat/ChatComposer";

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
  const typing = useAgentTyping(activeConfig.scope);

  const q = useQuery({
    queryKey: activeConfig.logKey,
    queryFn: () => fetchJson<AgentMessage[]>(activeConfig.logUrl),
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

  const decide = useMutation({
    mutationFn: ({ idx, decision }: { idx: number; decision: "approve" | "details" }) =>
      fetchJson<AgentMessage[]>("/api/v1/chat/action/" + idx + "/" + decision, { method: "POST" }),
    onSuccess: (log) => qc.setQueryData(activeConfig.logKey, log),
  });

  const handleSubmit = (trimmed: string) => {
    send.mutate(trimmed);
    setDraft("");
  };

  if (!config) return <Navigate to="/" replace />;

  return (
    <>
      <section className="chat-screen">
        <ChatLog
          messages={q.data}
          onDecideAction={(idx, decision) => decide.mutate({ idx, decision })}
          variant="screen"
          typing={typing}
        />
      </section>

      <ChatComposer
        value={draft}
        onChange={setDraft}
        onSubmit={handleSubmit}
      />
    </>
  );
}
