// Central key factory so invalidations stay type-safe.
// Every query reads through this; SSE invalidations reference the
// same roots so a missing key is a typo rather than a silent miss.

// Filter shape for the deployment-audit page (§14 `/admin/audit`).
// ``actor_kind`` is applied client-side over the loaded page (the
// backend does not yet accept it as a query param — see
// `app.api.admin.audit.list_audit`); the rest map 1-for-1 onto the
// server's `?actor_id=&action=&since=&until=`.
export interface AdminAuditFilter {
  actor_kind?: "user" | "agent" | "system";
  actor_id?: string;
  action?: string;
  since?: string;
  until?: string;
}

function hasAdminAuditFilter(filter: AdminAuditFilter): boolean {
  return Boolean(
    filter.actor_kind ||
      filter.actor_id ||
      filter.action ||
      filter.since ||
      filter.until,
  );
}

export const qk = {
  me: () => ["me"] as const,
  properties: () => ["properties"] as const,
  property: (pid: string) => ["property", pid] as const,
  propertyClosures: (pid: string) => ["property", pid, "closures"] as const,
  employees: () => ["employees"] as const,
  employee: (eid: string) => ["employee", eid] as const,
  employeeLeaves: (eid: string) => ["employee", eid, "leaves"] as const,
  tasks: () => ["tasks"] as const,
  task: (tid: string) => ["task", tid] as const,
  taskInstructions: (tid: string) => ["task", tid, "instructions"] as const,
  today: () => ["today"] as const,
  week: () => ["week"] as const,
  mySchedule: (fromIso: string, toIso: string) =>
    ["my-schedule", fromIso, toIso] as const,
  meOverrides: () => ["me", "availability_overrides"] as const,
  dashboard: () => ["dashboard"] as const,
  expenses: (scope: "all" | "mine") => ["expenses", scope] as const,
  expensesPendingReimbursement: (userId: "me" | string) =>
    ["expenses", "pending_reimbursement", userId] as const,
  exchangeRates: () => ["exchange_rates"] as const,
  issues: () => ["issues"] as const,
  stays: () => ["stays"] as const,
  taskTemplates: () => ["task_templates"] as const,
  workRoles: () => ["work_roles"] as const,
  schedules: () => ["schedules"] as const,
  scheduleRulesets: () => ["schedule_rulesets"] as const,
  schedulerCalendar: (fromIso: string, toIso: string) =>
    ["scheduler-calendar", fromIso, toIso] as const,
  instructions: () => ["instructions"] as const,
  instruction: (iid: string) => ["instruction", iid] as const,
  inventory: () => ["inventory"] as const,
  inventoryItem: (iid: string) => ["inventory", iid] as const,
  inventoryMovements: (iid: string) => ["inventory", iid, "movements"] as const,
  propertyStocktakes: (pid: string) => ["property", pid, "stocktakes"] as const,
  payslips: () => ["payslips"] as const,
  leaves: () => ["leaves"] as const,
  approvals: () => ["approvals"] as const,
  audit: () => ["audit"] as const,
  webhooks: () => ["webhooks"] as const,
  apiTokens: () => ["api_tokens"] as const,
  apiTokenAudit: (tid: string) => ["api_tokens", tid, "audit"] as const,
  meApiTokens: () => ["me", "api_tokens"] as const,
  llmAssignments: () => ["llm", "assignments"] as const,
  llmCalls: () => ["llm", "calls"] as const,
  settings: () => ["settings"] as const,
  settingsCatalog: () => ["settings", "catalog"] as const,
  settingsResolved: (kind: string, id: string) => ["settings", "resolved", kind, id] as const,
  propertySettings: (pid: string) => ["property", pid, "settings"] as const,
  employeeSettings: (eid: string) => ["employee", eid, "settings"] as const,
  history: (tab: string) => ["history", tab] as const,
  agentEmployeeLog: () => ["agent", "employee", "log"] as const,
  agentManagerLog: () => ["agent", "manager", "log"] as const,
  agentManagerActions: () => ["agent", "manager", "actions"] as const,
  agentTaskChat: (tid: string) => ["agent", "task", tid, "log"] as const,
  agentApprovalMode: () => ["me", "agent_approval_mode"] as const,
  // §14 "Agent turn indicator" — whether a turn is currently in
  // flight for the given scope. Cache value is `true`/`false`. The
  // SSE dispatcher flips it on the §11 `agent.turn.{started,finished}`
  // pair; the task scope is keyed per task id so two open task chats
  // don't share a single indicator.
  agentTyping: (scope: "employee" | "manager" | "admin" | "task", taskId?: string) =>
    scope === "task" && taskId
      ? (["agent", "typing", "task", taskId] as const)
      : (["agent", "typing", scope] as const),
  bookings: () => ["bookings"] as const,
  booking: (bid: string) => ["booking", bid] as const,
  guest: () => ["guest"] as const,
  assetTypes: () => ["asset_types"] as const,
  assets: () => ["assets"] as const,
  asset: (aid: string) => ["asset", aid] as const,
  documents: () => ["documents"] as const,
  users: (workspaceId?: string) => ["users", workspaceId ?? "all"] as const,
  workspaces: () => ["workspaces"] as const,
  // `/api/v1/me/workspaces` switcher rows. Distinct slot from
  // `workspaces()` so pages that map raw rows into a richer
  // `Workspace` shape (PropertiesPage, PortfolioPage) don't collide
  // with pages that consume the switcher payload directly
  // (PermissionsPage).
  meWorkspaces: () => ["me", "workspaces"] as const,
  organizations: (workspaceId?: string) => ["organizations", workspaceId ?? "active"] as const,
  organization: (oid: string) => ["organization", oid] as const,
  workOrders: (workspaceId?: string) => ["work_orders", workspaceId ?? "active"] as const,
  workOrder: (woid: string) => ["work_order", woid] as const,
  vendorInvoices: (workspaceId?: string) => ["vendor_invoices", workspaceId ?? "active"] as const,
  bookingBillings: (clientOrgId?: string) => ["booking_billings", clientOrgId ?? "all"] as const,
  clientRates: (clientOrgId?: string) => ["client_rates", clientOrgId ?? "all"] as const,
  propertyWorkspaces: (propertyId?: string, workspaceId?: string) =>
    ["property_workspaces", propertyId ?? "all", workspaceId ?? "all"] as const,
  propertyWorkspaceInvites: (propertyId?: string, direction: "in" | "out" | "any" = "out") =>
    ["property_workspace_invites", propertyId ?? "all", direction] as const,
  propertyWorkspaceInvite: (tokenOrId: string) =>
    ["property_workspace_invite", tokenOrId] as const,
  permissionGroups: (scopeKind?: string, scopeId?: string) =>
    ["permission_groups", scopeKind ?? "all", scopeId ?? "all"] as const,
  permissionGroupMembers: (gid: string) => ["permission_group_members", gid] as const,
  permissionRules: (scopeKind?: string, scopeId?: string) =>
    ["permission_rules", scopeKind ?? "all", scopeId ?? "all"] as const,
  actionCatalog: () => ["action_catalog"] as const,
  permissionResolved: (userId: string, actionKey: string, scopeKind: string, scopeId: string) =>
    ["permissions", "resolved", userId, actionKey, scopeKind, scopeId] as const,
  chatChannels: () => ["chat", "channels"] as const,
  chatChannelProviders: () => ["chat", "channels", "providers"] as const,
  agentPrefs: (scope: "workspace" | "property" | "me", id?: string) =>
    ["agent_preferences", scope, id ?? ""] as const,
  workspaceUsage: () => ["workspace", "usage"] as const,
  // §14 — /admin shell.
  adminMe: () => ["admin", "me"] as const,
  adminWorkspaces: () => ["admin", "workspaces"] as const,
  adminUsageSummary: () => ["admin", "usage", "summary"] as const,
  adminUsageWorkspaces: () => ["admin", "usage", "workspaces"] as const,
  adminLlmGraph: () => ["admin", "llm", "graph"] as const,
  adminLlmCalls: () => ["admin", "llm", "calls"] as const,
  adminLlmPrompts: () => ["admin", "llm", "prompts"] as const,
  adminChatProviders: () => ["admin", "chat", "providers"] as const,
  adminChatOverrides: () => ["admin", "chat", "overrides"] as const,
  adminSignup: () => ["admin", "signup"] as const,
  adminSignups: () => ["admin", "signups"] as const,
  adminSettings: () => ["admin", "settings"] as const,
  adminAdmins: () => ["admin", "admins"] as const,
  // §14 `/admin/audit`. The bare call returns the prefix tuple so the
  // dashboard's "recent audit" slot and the SSE invalidator
  // (`admin.audit.appended`) keep matching every filtered variant —
  // TanStack Query invalidates by prefix by default. Filtered lookups
  // append the filter object as the third tuple slot so each filter
  // combination caches independently.
  adminAudit: (filter?: AdminAuditFilter) =>
    filter && hasAdminAuditFilter(filter)
      ? (["admin", "audit", filter] as const)
      : (["admin", "audit"] as const),
  adminAgentLog: () => ["admin", "agent", "log"] as const,
  adminAgentActions: () => ["admin", "agent", "actions"] as const,
  // §11 — Agent knowledge tools.
  documentExtraction: (did: string) => ["document", did, "extraction"] as const,
  kbSearch: (q: string) => ["kb", "search", q] as const,
  kbDoc: (kind: "instruction" | "document", id: string, page: number = 1) =>
    ["kb", "doc", kind, id, page] as const,
  kbSystemDocs: (role?: string) => ["kb", "system_docs", role ?? "all"] as const,
  kbSystemDoc: (slug: string) => ["kb", "system_docs", slug] as const,
  adminAgentDocs: () => ["admin", "agent_docs"] as const,
  adminAgentDoc: (slug: string) => ["admin", "agent_docs", slug] as const,
} as const;
