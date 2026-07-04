import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation, useParams } from "react-router-dom";
import { ChevronDown } from "lucide-react";
import { fetchJson, toDisplayError } from "@/lib/api";
import {
  fetchApprovals,
  projectInlineApprovals,
  type InlineApprovalChannel,
} from "@/lib/approvals";
import { qk } from "@/lib/queryKeys";
import { initialAgentCollapsed, persistAgentCollapsed } from "@/lib/preferences";
import { useAgentActivity } from "@/lib/agentTyping";
import AgentActionCards from "@/components/chat/AgentActionCards";
import AgentMessageLinks from "@/components/chat/AgentMessageLinks";
import ChatComposer from "@/components/chat/ChatComposer";
import ChatMessageBody from "@/components/chat/ChatMessageBody";
import DateTime from "@/components/DateTime";
import InlineErrorAlert from "@/components/InlineErrorAlert";
import type { AgentAction, AgentMessage, AgentTurnScope, Role } from "@/types/api";

// CRITICAL: AgentSidebar MUST mount as a SIBLING of <Outlet /> in
// EmployeeLayout and ManagerLayout, never inside a route's subtree.
// React Router only remounts the outlet's subtree on navigation;
// siblings survive. That's what gives us a persistent chat log
// (scrollTop, composer draft, EventSource-fed cache) across page
// changes.
//
// Above 720px the rail renders inline (collapsed or expanded, see
// `initialAgentCollapsed`). Below 720px the rail is hidden by CSS;
// both shells route their bottom Chat tab to /chat instead. `role`
// selects the per-role log/message endpoints and the inline-approval
// channel: admin reads its own action queue, managers/workers read the
// server-scoped /approvals list (cd-uu806) — the manager rail via
// `web_owner_sidebar`, the worker rail via `web_worker_chat`.
interface AgentSidebarProps {
  agentRole: Role;
}

export default function AgentSidebar({ agentRole: role }: AgentSidebarProps) {
  const [collapsed, setCollapsed] = useState<boolean>(() => initialAgentCollapsed());
  const [draft, setDraft] = useState("");
  const logRef = useRef<HTMLDivElement>(null);
  const qc = useQueryClient();
  const { pathname } = useLocation();
  const params = useParams();

  const isAdmin = role === "admin";
  const isManager = role === "manager";
  const isEmployee = role === "employee";
  // The /approvals list is server-scoped to the caller (cd-uu806):
  // whatever it returns is actionable, so every agent surface but the
  // read-only `client` role renders decide cards. Admin uses its own
  // action queue; managers see their desk + own-conversation rows;
  // workers see their own agent's inline cards.
  const showActions = isAdmin || isManager || isEmployee;
  const inlineChannel: InlineApprovalChannel = isManager
    ? "web_owner_sidebar"
    : "web_worker_chat";
  const typingScope: AgentTurnScope = isAdmin ? "admin" : isManager ? "manager" : "employee";
  const activity = useAgentActivity(typingScope);

  // Query keys and endpoints are scoped per role. The admin agent
  // lives under /admin/api/v1/... with its own log/actions. Workspace
  // chat logs live under /api/v1/agent/{manager|employee}/..., while
  // manager/worker approval cards come from the shared /approvals queue.
  const logKey = isAdmin
    ? qk.adminAgentLog()
    : isManager ? qk.agentManagerLog() : qk.agentEmployeeLog();
  const actionsKey = isAdmin ? qk.adminAgentActions() : qk.approvals();
  const logUrl = isAdmin
    ? "/admin/api/v1/agent/log"
    : isManager ? "/api/v1/agent/manager/log" : "/api/v1/agent/employee/log";
  const messageUrl = isAdmin
    ? "/admin/api/v1/agent/message"
    : isManager ? "/api/v1/agent/manager/message" : "/api/v1/agent/employee/message";
  const decideUrlFor = (id: string, decision: "approve" | "deny") =>
    isAdmin
      ? `/admin/api/v1/agent/action/${id}/${decision}`
      : `/api/v1/approvals/${id}/${decision === "approve" ? "approve" : "deny"}`;

  const log = useQuery({
    queryKey: logKey,
    queryFn: () => fetchJson<AgentMessage[]>(logUrl),
  });
  // Admin reads its own action queue under a dedicated key; managers/workers
  // read the shared /approvals list. Both feed AgentAction[] cards, but the
  // non-admin path caches the RAW ApprovalRequest[] under qk.approvals() (the
  // same key + queryFn the manager desk uses) and derives this channel's cards
  // via `select`, so the rail and the desk share one cache blob rather than
  // registering divergent shapes under one key (cd-tifg4). Only the active
  // role's query is enabled; the other stays idle.
  const adminActions = useQuery({
    queryKey: qk.adminAgentActions(),
    queryFn: () => fetchJson<AgentAction[]>("/admin/api/v1/agent/actions"),
    enabled: showActions && isAdmin,
  });
  const inlineActions = useQuery({
    queryKey: qk.approvals(),
    queryFn: fetchApprovals,
    select: (data): AgentAction[] => projectInlineApprovals(data, inlineChannel),
    enabled: showActions && !isAdmin,
  });
  const actions = isAdmin ? adminActions : inlineActions;

  // §12 "Agent audit headers", every message carries the route the
  // user is on so the agent can resolve "this workspace" / "this
  // capability" without the user naming it. Admin context also
  // encodes known entity params (ws, capability) lifted from the URL.
  const pageHeader = buildAgentPageHeader(pathname, params, role);

  const sendMessage = useMutation({
    mutationFn: (body: string) =>
      fetchJson<AgentMessage>(messageUrl, {
        method: "POST",
        headers: { "X-Agent-Page": pageHeader },
        body: { body },
      }),
    onMutate: async (body) => {
      await qc.cancelQueries({ queryKey: logKey });
      const prev = qc.getQueryData<AgentMessage[]>(logKey) ?? [];
      const optimistic: AgentMessage = { at: new Date().toISOString(), kind: "user", body };
      qc.setQueryData<AgentMessage[]>(logKey, [...prev, optimistic]);
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(logKey, ctx.prev);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: logKey }),
  });
  const showTyping = activity.typing || sendMessage.isPending;

  const decideAction = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "deny" }) =>
      fetchJson<{ ok: true }>(decideUrlFor(id, decision), {
        method: "POST",
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: actionsKey });
      qc.invalidateQueries({ queryKey: logKey });
    },
  });

  // Scroll-to-bottom on new messages or when the typing bubble toggles.
  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [log.data?.length, showTyping]);

  const toggle = useCallback(() => {
    setCollapsed((c) => {
      const next = !c;
      persistAgentCollapsed(next ? "collapsed" : "open");
      // Mirror state onto either layout's root for grid recalculation
      // in browsers without :has() support.
      const host = document.querySelector(".desk, .phone");
      if (host) host.setAttribute("data-agent-collapsed", next ? "true" : "false");
      return next;
    });
  }, []);

  const handleSend = useCallback(
    (trimmed: string) => {
      sendMessage.mutate(trimmed);
      setDraft("");
    },
    [sendMessage],
  );

  const className = "desk__agent" + (collapsed ? " desk__agent--collapsed" : "");

  return (
    <aside className={className} aria-label="Agent sidebar">
      <button
        type="button"
        className="desk__agent-head"
        onClick={toggle}
        aria-expanded={!collapsed}
        aria-controls="agent-body"
      >
        <span className="desk__agent-title">Agent</span>
        <span className="desk__agent-status">
          <span className="desk__agent-dot" aria-hidden="true" />
          <span>online</span>
        </span>
        <span className="desk__agent-chevron" aria-hidden="true">
          <ChevronDown size={14} strokeWidth={2} />
        </span>
      </button>

      <div className="desk__agent-body" id="agent-body">
        <div className="agent-log" ref={logRef} role="log" aria-live="polite">
          {log.isError && (
            <InlineErrorAlert error={toDisplayError(log.error)} />
          )}
          {log.data?.map((msg, i) => {
            const completedActivityLabel = activity.label;
            const showCompletedActivity =
              completedActivityLabel &&
              !showTyping &&
              msg.kind === "agent" &&
              i === log.data.length - 1;
            return (
              <Fragment key={msg.at + "|" + msg.kind + "|" + msg.body}>
                {showCompletedActivity && (
                  <AgentActivityLine label={completedActivityLabel} />
                )}
                <div className={"agent-msg agent-msg--" + msg.kind}>
                  {msg.kind === "agent" ? (
                    <>
                      <ChatMessageBody body={msg.body} className="agent-msg__body" />
                      <AgentMessageLinks message={msg} />
                    </>
                  ) : (
                    <span className="agent-msg__body">{msg.body}</span>
                  )}
                  <DateTime value={msg.at} showTime className="agent-msg__time" />
                </div>
              </Fragment>
            );
          })}
          {showTyping && activity.label && (
            <AgentActivityLine label={activity.label} live />
          )}
          {showTyping && (
            <div className="agent-msg agent-msg--agent agent-msg--typing">
              <span className="agent-msg__body">
                <span className="chat-typing" aria-hidden="true">
                  <span className="chat-typing__dot" />
                  <span className="chat-typing__dot" />
                  <span className="chat-typing__dot" />
                </span>
                <span className="sr-only">Agent is typing</span>
              </span>
            </div>
          )}
        </div>

        {showActions && actions.isError && (
          <div className="agent-actions" aria-label="Pending agent actions">
            <InlineErrorAlert error={toDisplayError(actions.error)} />
          </div>
        )}
        {showActions && actions.data && (
          <AgentActionCards
            actions={actions.data}
            onDecide={(id, decision) => decideAction.mutate({ id, decision })}
          />
        )}

        <ChatComposer
          variant="inline"
          value={draft}
          onChange={setDraft}
          onSubmit={handleSend}
          placeholder={isAdmin ? "Ask the admin agent…" : "Ask the agent…"}
          ariaLabel="Message agent"
          textareaMaxHeight={null}
        />
      </div>
    </aside>
  );
}

function AgentActivityLine({ label, live = false }: { label: string; live?: boolean }) {
  return (
    <div className="agent-activity">
      <span aria-hidden="true">{label}</span>
      {live && (
        <output className="sr-only" aria-live="polite">
          {label}
        </output>
      )}
    </div>
  );
}

// §12 "Agent audit headers" → X-Agent-Page.
// Shape: "route=<pattern>; params=<k>=<v>,…". The server parses it
// into a system-prompt section so the agent can act on "this
// workspace" or "this capability" without the user naming it.
function buildAgentPageHeader(
  pathname: string,
  params: Record<string, string | undefined>,
  role: Role,
): string {
  const kvParts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    if (value) kvParts.push(`${key}=${value}`);
  }
  const kv = kvParts.join(",");
  const pieces: string[] = [];
  pieces.push(`route=${pathname}`);
  if (kv) pieces.push(`params=${kv}`);
  pieces.push(`surface=${role}`);
  return pieces.join("; ");
}
