// crewday — JSON API types: core primitives.
// Shapes mirror the dataclasses in mocks/app/mock_data.py. The FastAPI
// layer serializes via dataclasses.asdict, so dates arrive as ISO-8601
// strings and enums as their literal string values.

export type Role = "employee" | "manager" | "client" | "admin";
export type Theme = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

export interface User {
  id: string;
  email: string;
  display_name: string;
  timezone: string;
  languages: string[];
  preferred_locale: string | null;
  avatar_file_id: string | null;
  primary_workspace_id: string | null;
  phone_e164: string | null;
  notes_md: string;
  archived_at: string | null;
}

export interface Workspace {
  id: string;
  name: string;
  timezone: string;
  default_currency: string;
  default_country: string;
  default_locale: string;
}

export interface AuditEntry {
  at: string;
  // v1 collapses to user|agent|system; the surface grant under
  // which a user acted lives in actor_grant_role (§02). The
  // separate actor_was_owner_member bit captures whether the
  // actor held ``owners`` permission-group membership at the
  // time — so reviewers can tell governance actions apart from
  // ordinary administration.
  actor_kind: "user" | "agent" | "system";
  actor: string;
  action: string;
  target: string;
  via: "web" | "api" | "cli" | "worker";
  reason: string | null;
  actor_grant_role: "manager" | "worker" | "client" | "guest" | "admin" | null;
  actor_was_owner_member: boolean | null;
  actor_action_key: string | null;
  actor_id: string | null;
  agent_label: string | null;
  entity_kind?: string;
  entity_id?: string;
  correlation_id?: string;
  diff?: unknown;
}

export interface AuditListResponse {
  data: AuditEntry[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface Webhook {
  id: string;
  name: string;
  url: string;
  events: string[];
  active: boolean;
  paused_reason: string | null;
  paused_at: string | null;
  secret_last_4: string;
  last_delivery_status: string | number | null;
  last_delivery_at: string | null;
  created_at: string;
  updated_at: string;
  secret?: string | null;
}

export interface WebhookDelivery {
  id: string;
  subscription_id?: string;
  event: string;
  status: string;
  attempt: number;
  response_status?: number | null;
  error?: string | null;
  last_status_code?: number | null;
  last_error?: string | null;
  created_at: string;
  next_attempt_at: string | null;
}
