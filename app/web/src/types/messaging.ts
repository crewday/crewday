// crewday — JSON API types: chat messages, chat gateway bindings,
// and gateway providers (web + off-app channels).

export type ChatChannelKind = "offapp_whatsapp" | "offapp_telegram";

export interface AgentNavigationLink {
  rel?: string;
  label: string;
  route: string;
  href: string;
}

export interface AgentLinksPayload {
  links?: AgentNavigationLink[];
  items?: Array<{
    index?: number;
    links?: AgentNavigationLink[];
  }>;
  warnings?: Array<{
    rel?: string;
    reason: string;
  }>;
}

export interface AgentMessage {
  at: string;
  kind: "agent" | "user" | "action";
  body: string;
  /** §23 chat gateway — channel the turn traversed; null/undefined = web. */
  channel_kind?: ChatChannelKind | null;
  /** §11 agent handoff links — GET-only navigation affordances. */
  links?: AgentNavigationLink[] | null;
  agent_links?: AgentNavigationLink[] | AgentLinksPayload | null;
}

export interface ChatChannelBinding {
  id: string;
  user_id: string;
  user_display_name: string;
  channel_kind: ChatChannelKind;
  address: string;
  display_label: string;
  state: "pending" | "active" | "revoked";
  verified_at: string | null;
  last_message_at: string | null;
  revoked_at: string | null;
  revoke_reason: "user" | "stop_keyword" | "user_archived" | "admin" | "provider_error" | null;
}

export interface ChatGatewayProvider {
  channel_kind: ChatChannelKind;
  provider: string;
  status: "connected" | "pending" | "error" | "not_configured";
  display_stub: string;
  last_webhook_at: string | null;
  templates: string[];
}

export interface NotificationPayload {
  id: string;
  workspace_id: string;
  recipient_user_id: string;
  kind: string;
  subject: string;
  body_md: string | null;
  payload: Record<string, unknown>;
  read_at: string | null;
  created_at: string;
}

export interface NotificationListResponse {
  data: NotificationPayload[];
  next_cursor: string | null;
  has_more: boolean;
  total_estimate: number;
}

export interface BroadcastRecipientPayload {
  user_id: string;
  token: string;
  display_name: string;
  email: string | null;
}

export interface BroadcastAudienceGroupPayload {
  token: string;
  label: string;
  kind: "everyone" | "workspace_role" | "work_role";
  resolved_recipient_count: number;
}

export interface BroadcastRecipientsResponse {
  people: BroadcastRecipientPayload[];
  groups: BroadcastAudienceGroupPayload[];
  data: BroadcastRecipientPayload[];
  total: number;
}

export interface BroadcastSendRequest {
  audience_tokens: string[];
  confirmed_recipient_count: number;
  subject: string;
  body_md: string;
}

export interface BroadcastSendResponse {
  status: "sent" | "pending_approval";
  recipient_count: number;
  notification_ids: string[];
  approval_request_id: string | null;
  expires_at: string | null;
}
