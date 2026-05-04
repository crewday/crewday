// crewday — JSON API types: permission model (§02, §05) and API
// tokens (§03). Kept together because role grants, permission rules,
// and API tokens all turn on the same ScopeKind / GrantRole taxonomy.

import type { Workspace } from "./core";

// ── Permission model (§02, §05) ───────────────────────────────────

export type ScopeKind = "workspace" | "property" | "organization" | "deployment";
export type RuleEffect = "allow" | "deny";
export type GrantRole = "manager" | "worker" | "client" | "guest" | "admin";

// §02 — workspaces the current user has access to, with the
// highest-privilege grant role they hold there. Returned by /auth/me
// so WorkspaceGate can render without a second call.
export interface AvailableWorkspace {
  workspace_id?: string | null;
  workspace: Workspace;
  grant_role: GrantRole | null;
  binding_org_id: string | null;
  source: "workspace_grant" | "property_grant" | "org_grant" | "work_engagement";
}

export interface RoleGrant {
  id: string;
  user_id: string;
  scope_kind: ScopeKind;
  scope_id: string;
  grant_role: GrantRole;
  binding_org_id: string | null;
  started_on: string | null;
  ended_on: string | null;
  granted_by_user_id: string | null;
  revoked_at: string | null;
  revoke_reason: string | null;
}

// Mirrors `app.api.v1.permission_groups.PermissionGroupResponse`. The
// `group_kind` / `is_derived` projections are not in v1 — the router
// emits a flat `system: bool` and the derived/role-grant joining is
// deferred (cd-zkr). Re-add once the router does (paired follow-up
// task referenced below by id in the consuming components).
export interface PermissionGroup {
  id: string;
  slug: string;
  name: string;
  system: boolean;
  capabilities: Record<string, unknown>;
  created_at: string;
}

// Mirrors `PermissionGroupMemberResponse` — one row per explicit
// (group, user) pair. v1 derived groups carry no rows.
export interface PermissionGroupMember {
  group_id: string;
  user_id: string;
  added_at: string;
  added_by_user_id: string | null;
}

// Mirrors `PermissionRuleResponse`. The router never returns
// `revoke_reason` — revoked rows are filtered out at the SQL layer.
export interface PermissionRule {
  id: string;
  scope_kind: ScopeKind;
  scope_id: string;
  action_key: string;
  subject_kind: "user" | "group";
  subject_id: string;
  effect: RuleEffect;
  created_at: string;
  created_by_user_id: string | null;
  revoked_at: string | null;
}

// Mirrors `ActionCatalogEntryResponse`. v1 omits `description` /
// `spec` — the catalog is identified solely by its action `key`;
// human-readable copy lives in spec docs, not on the wire.
export interface ActionCatalogEntry {
  key: string;
  valid_scope_kinds: ScopeKind[];
  default_allow: string[];
  root_only: boolean;
  root_protected_deny: boolean;
}

export interface ResolvedPermission {
  effect: RuleEffect;
  source_layer: string;
  source_rule_id: string | null;
  matched_groups: string[];
}

// §03 API tokens — three kinds. The wire shape is a single type
// because the list endpoint mixes scoped/delegated rows (for
// managers) and the /me endpoint filters to `personal` only. Field
// names mirror the backend `TokenSummaryResponse` Pydantic shape so
// there is one canonical wire vocabulary across the workspace
// (`/auth/tokens`) and identity (`/me/tokens`) routers.
export type ApiTokenKind = "scoped" | "delegated" | "personal";

export interface ApiToken {
  /** ULID — stable id used in every URL (`…/{key_id}/revoke` etc.). */
  key_id: string;
  /** Human-readable label set at mint time. */
  label: string;
  kind: ApiTokenKind;
  /** `mip_<key_id>` — the public half of the token. Full secret
   *  only returned once at creation time via `ApiTokenCreated`. */
  prefix: string;
  /** §03 flat `{action_key: true}` mapping. Empty for delegated
   *  tokens (authority resolves through the delegator's grants). */
  scopes: Record<string, unknown>;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  /** Set on delegated rows only — the user whose grants the token
   *  inherits at request time. `null` for scoped + personal. */
  delegate_for_user_id?: string | null;
}

export interface ApiTokenCreated {
  /** The `mip_<key_id>_<secret>` plaintext. Shown once, then never
   *  returned again — we store an argon2id hash, not the secret. */
  token: string;
  key_id: string;
  prefix: string;
  expires_at: string | null;
  kind: ApiTokenKind;
}

// §12 cursor envelope for `GET /auth/tokens` (cd-msu2). Same shape
// as `SignupsListResponse` and every other paginated listing.
export interface ApiTokenListResponse {
  data: ApiToken[];
  next_cursor: string | null;
  has_more: boolean;
}

// §03 per-token audit timeline — lifecycle events only on v1
// (`api_token.minted` / `rotated` / `revoked` / `revoked_noop`).
// A sibling per-request log lands later as a follow-up.
export interface ApiTokenAuditEntry {
  at: string;
  /** Domain action key (e.g. `api_token.minted`). */
  action: string;
  /** ULID of the user / system actor who performed the action. */
  actor_id: string;
  correlation_id: string;
}

// ── Invites (§03 "Additional users") ──────────────────────────────
//
// Mirrors `app.api.v1.auth.invite.InviteIntrospectionResponse` /
// `AcceptResponse`. The grants + group_memberships dicts are
// validated server-side via `_validate_grants(...)` and round-tripped
// through `invite_row.grants_json` — keys are stable but the dict
// stays open-ended to keep the wire forward-compatible.
export type InviteKind = "new_user" | "existing_user" | "needs_sign_in";

export interface InviteGrantPreview {
  scope_kind: ScopeKind;
  scope_id: string;
  grant_role: GrantRole;
  scope_property_id?: string | null;
  binding_org_id?: string | null;
  [extension: string]: unknown;
}

export interface InvitePermissionGroupMembershipPreview {
  group_id: string;
  group_slug?: string;
  group_name?: string;
  [extension: string]: unknown;
}

// `GET /api/v1/invites/{token}` — read-only preview, does not burn
// the magic-link nonce. The page renders this before the user clicks
// Accept.
export interface InviteIntrospection {
  kind: Exclude<InviteKind, "needs_sign_in">;
  invite_id: string;
  workspace_id: string;
  workspace_slug: string;
  workspace_name: string;
  inviter_display_name: string;
  email_lower: string;
  expires_at: string;
  grants: InviteGrantPreview[];
  permission_group_memberships: InvitePermissionGroupMembershipPreview[];
}

// `POST /api/v1/invites/{token}/accept` — the union response. The
// server fills different field subsets per `kind`; we keep them all
// nullable so a single shape covers both branches.
export interface InviteAcceptResponse {
  kind: InviteKind;
  invite_id: string;
  // Populated on the `new_user` branch.
  user_id?: string | null;
  email_lower?: string | null;
  display_name?: string | null;
  // Populated on the `existing_user` branch.
  workspace_id?: string | null;
  workspace_slug?: string | null;
  workspace_name?: string | null;
  grants?: InviteGrantPreview[] | null;
  permission_group_memberships?: InvitePermissionGroupMembershipPreview[] | null;
}
