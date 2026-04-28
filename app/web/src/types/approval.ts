// crewday — JSON API types: agent approval gates, approval requests,
// and agent actions. The `AgentApprovalMode` belongs here because it
// drives how `ApprovalRequest.resolved_user_mode` is resolved.

// §11 — which layer of the gate fired and where its confirmation
// lands. `desk` rows live on /approvals only; `inline_chat` rows
// also render in the user's chat sidebar / PWA chat tab.
export type GateSource =
  | "workspace_always"
  | "workspace_configurable"
  | "user_auto_annotation"
  | "user_strict_mutation";
export type GateDestination = "desk" | "inline_chat";
export type InlineChannel =
  | "desk_only"
  | "web_owner_sidebar"
  | "web_worker_chat"
  | "offapp_whatsapp";

// §11 — per-user setting governing when the user's embedded chat
// agent pauses for an inline confirmation card before executing.
export type AgentApprovalMode = "bypass" | "auto" | "strict";

export interface ApprovalRequest {
  id: string;
  agent: string;
  action: string;
  target: string;
  reason: string;
  requested_at: string;
  risk: "low" | "medium" | "high";
  diff: string[];
  gate_source: GateSource;
  gate_destination: GateDestination;
  inline_channel: InlineChannel;
  card_summary: string;
  card_fields: [string, string][];
  for_user_id: string | null;
  resolved_user_mode: AgentApprovalMode | null;
}

export interface ApprovalRequestPayload {
  id: string;
  workspace_id: string;
  requester_actor_id: string | null;
  for_user_id: string | null;
  inline_channel: InlineChannel | null;
  resolved_user_mode: AgentApprovalMode | null;
  status: "pending" | "approved" | "rejected" | "timed_out";
  decided_by: string | null;
  decided_at: string | null;
  decision_note_md: string | null;
  expires_at: string | null;
  created_at: string;
  action_json: Record<string, unknown>;
  result_json: Record<string, unknown> | null;
}

export interface ApprovalsListResponse {
  data: ApprovalRequestPayload[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface AgentAction {
  id: string;
  title: string;
  detail: string;
  risk: "low" | "medium" | "high";
  card_summary: string;
  card_fields: [string, string][];
  gate_source: GateSource;
  inline_channel: "web_owner_sidebar" | "web_worker_chat" | "web_admin_sidebar";
}
