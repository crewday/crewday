// Central key factory. Every TanStack Query key emitted by the SPA
// passes through `qk.*` so invalidations stay type-safe and SSE
// dispatch has one source of truth for the matching roots.
//
// Spec §14 "Workspace-scoped query keys" — every key's first segment
// is `["w", <slug>, ...]` so switching workspaces cannot accidentally
// serve one tenant's cache to another tenant's page. The slug is
// resolved lazily through the pluggable getter wired by
// `WorkspaceProvider`; when no workspace is selected we fall back to
// the `"_"` sentinel so pre-/me calls (login, workspace picker) stay
// in their own namespace and can never collide with a real tenant.

const NO_WORKSPACE_SENTINEL = "_";

let workspaceSlugGetter: () => string | null = () => null;

/**
 * Wire the active workspace slug source. Called by `WorkspaceProvider`
 * on mount. Exposed separately from `@/lib/api` so tests can stub one
 * without the other.
 */
export function registerQueryKeyWorkspaceGetter(getter: () => string | null): void {
  workspaceSlugGetter = getter;
}

/**
 * Test-only reset. Never call from product code.
 */
export function __resetQueryKeyGetterForTests(): void {
  workspaceSlugGetter = () => null;
}

function activeSlug(): string {
  return workspaceSlugGetter() ?? NO_WORKSPACE_SENTINEL;
}

// `WorkspacePrefix` is an alias so call-sites that want to assert
// against the shape in tests have a stable type to import.
export type WorkspacePrefix = readonly ["w", string];

function ws(): WorkspacePrefix {
  return ["w", activeSlug()] as const;
}

// Helper keeping the tuple-variance readable for TS inference. Each
// factory spreads `ws()` then appends its own stable prefix + params,
// which preserves tuple literal types all the way down.
export const qk = {
  authMe: () => ["auth", "me"] as const,
  me: () => [...ws(), "me"] as const,
  properties: () => [...ws(), "properties"] as const,
  property: (pid: string) => [...ws(), "property", pid] as const,
  propertyClosures: (pid: string) => [...ws(), "property", pid, "closures"] as const,
  employees: () => [...ws(), "employees"] as const,
  employee: (eid: string) => [...ws(), "employee", eid] as const,
  employeeLeaves: (eid: string) => [...ws(), "employee", eid, "leaves"] as const,
  tasks: () => [...ws(), "tasks"] as const,
  task: (tid: string) => [...ws(), "task", tid] as const,
  taskInstructions: (tid: string) => [...ws(), "task", tid, "instructions"] as const,
  today: () => [...ws(), "today"] as const,
  week: () => [...ws(), "week"] as const,
  mySchedule: (fromIso: string, toIso: string) =>
    [...ws(), "my-schedule", fromIso, toIso] as const,
  meOverrides: () => [...ws(), "me", "availability_overrides"] as const,
  dashboard: () => [...ws(), "dashboard"] as const,
  expenses: (scope: "all" | "mine") => [...ws(), "expenses", scope] as const,
  expensesPendingReimbursement: (userId: "me" | string) =>
    [...ws(), "expenses", "pending_reimbursement", userId] as const,
  workEngagementActive: (userId: string) =>
    [...ws(), "work_engagements", "active", userId] as const,
  exchangeRates: () => [...ws(), "exchange_rates"] as const,
  issues: () => [...ws(), "issues"] as const,
  stays: () => [...ws(), "stays"] as const,
  taskTemplates: () => [...ws(), "task_templates"] as const,
  workRoles: () => [...ws(), "work_roles"] as const,
  schedules: () => [...ws(), "schedules"] as const,
  scheduleRulesets: () => [...ws(), "schedule_rulesets"] as const,
  schedulerCalendar: (fromIso: string, toIso: string) =>
    [...ws(), "scheduler-calendar", fromIso, toIso] as const,
  instructions: () => [...ws(), "instructions"] as const,
  instruction: (iid: string) => [...ws(), "instruction", iid] as const,
  inventory: () => [...ws(), "inventory"] as const,
  payslips: () => [...ws(), "payslips"] as const,
  leaves: () => [...ws(), "leaves"] as const,
  approvals: () => [...ws(), "approvals"] as const,
  audit: () => [...ws(), "audit"] as const,
  webhooks: () => [...ws(), "webhooks"] as const,
  webhookDeliveries: (wid: string) => [...ws(), "webhooks", wid, "deliveries"] as const,
  apiTokens: () => [...ws(), "api_tokens"] as const,
  apiTokenAudit: (tid: string) => [...ws(), "api_tokens", tid, "audit"] as const,
  meApiTokens: () => [...ws(), "me", "api_tokens"] as const,
  llmAssignments: () => [...ws(), "llm", "assignments"] as const,
  llmCalls: () => [...ws(), "llm", "calls"] as const,
  settings: () => [...ws(), "settings"] as const,
  settingsCatalog: () => [...ws(), "settings", "catalog"] as const,
  settingsResolved: (kind: string, id: string) => [...ws(), "settings", "resolved", kind, id] as const,
  propertySettings: (pid: string) => [...ws(), "property", pid, "settings"] as const,
  employeeSettings: (eid: string) => [...ws(), "employee", eid, "settings"] as const,
  history: (tab: string) => [...ws(), "history", tab] as const,
  agentEmployeeLog: () => [...ws(), "agent", "employee", "log"] as const,
  agentManagerLog: () => [...ws(), "agent", "manager", "log"] as const,
  agentManagerActions: () => [...ws(), "agent", "manager", "actions"] as const,
  agentTaskChat: (tid: string) => [...ws(), "agent", "task", tid, "log"] as const,
  agentApprovalMode: () => [...ws(), "me", "agent_approval_mode"] as const,
  // §14 "Agent turn indicator" — whether a turn is currently in
  // flight for the given scope. Cache value is `true`/`false`. The
  // SSE dispatcher flips it on the §11 `agent.turn.{started,finished}`
  // pair; the task scope is keyed per task id so two open task chats
  // don't share a single indicator.
  agentTyping: (scope: "employee" | "manager" | "admin" | "task", taskId?: string) =>
    scope === "task" && taskId
      ? ([...ws(), "agent", "typing", "task", taskId] as const)
      : ([...ws(), "agent", "typing", scope] as const),
  bookings: () => [...ws(), "bookings"] as const,
  booking: (bid: string) => [...ws(), "booking", bid] as const,
  guest: (token?: string) => [...ws(), "guest", token ?? ""] as const,
  assetTypes: () => [...ws(), "asset_types"] as const,
  assets: () => [...ws(), "assets"] as const,
  asset: (aid: string) => [...ws(), "asset", aid] as const,
  documents: () => [...ws(), "documents"] as const,
  users: (workspaceId?: string) => [...ws(), "users", workspaceId ?? "all"] as const,
  workspaces: () => [...ws(), "workspaces"] as const,
  clientPortfolio: () => [...ws(), "client", "portfolio"] as const,
  clientQuotes: () => [...ws(), "client", "quotes"] as const,
  organizations: (workspaceId?: string) => [...ws(), "organizations", workspaceId ?? "active"] as const,
  organization: (oid: string) => [...ws(), "organization", oid] as const,
  workOrders: (workspaceId?: string) => [...ws(), "work_orders", workspaceId ?? "active"] as const,
  workOrder: (woid: string) => [...ws(), "work_order", woid] as const,
  vendorInvoices: (workspaceId?: string) => [...ws(), "vendor_invoices", workspaceId ?? "active"] as const,
  bookingBillings: (clientOrgId?: string) => [...ws(), "booking_billings", clientOrgId ?? "all"] as const,
  clientRates: (clientOrgId?: string) => [...ws(), "client_rates", clientOrgId ?? "all"] as const,
  propertyWorkspaces: (propertyId?: string, workspaceId?: string) =>
    [...ws(), "property_workspaces", propertyId ?? "all", workspaceId ?? "all"] as const,
  propertyWorkspaceInvites: (propertyId?: string, direction: "in" | "out" | "any" = "out") =>
    [...ws(), "property_workspace_invites", propertyId ?? "all", direction] as const,
  propertyWorkspaceInvite: (tokenOrId: string) =>
    [...ws(), "property_workspace_invite", tokenOrId] as const,
  permissionGroups: (scopeKind?: string, scopeId?: string) =>
    [...ws(), "permission_groups", scopeKind ?? "all", scopeId ?? "all"] as const,
  permissionGroupMembers: (gid: string) => [...ws(), "permission_group_members", gid] as const,
  permissionRules: (scopeKind?: string, scopeId?: string) =>
    [...ws(), "permission_rules", scopeKind ?? "all", scopeId ?? "all"] as const,
  actionCatalog: () => [...ws(), "action_catalog"] as const,
  permissionResolved: (userId: string, actionKey: string, scopeKind: string, scopeId: string) =>
    [...ws(), "permissions", "resolved", userId, actionKey, scopeKind, scopeId] as const,
  chatChannels: () => [...ws(), "chat", "channels"] as const,
  chatChannelProviders: () => [...ws(), "chat", "channels", "providers"] as const,
  agentPrefs: (scope: "workspace" | "property" | "me", id?: string) =>
    [...ws(), "agent_preferences", scope, id ?? ""] as const,
  workspaceUsage: () => [...ws(), "workspace", "usage"] as const,
  // §14 — /admin shell. The admin surface is deployment-scope, not
  // workspace-scope (§14 "Admin shell"), so admin keys live outside
  // the workspace namespace. `adminNs()` keeps the first segment
  // stable (`["admin", ...]`) so `queryClient.invalidateQueries({
  // queryKey: ["admin"] })` still matches every admin entry.
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
  adminSettings: () => ["admin", "settings"] as const,
  adminAdmins: () => ["admin", "admins"] as const,
  adminAudit: () => ["admin", "audit"] as const,
  adminAgentLog: () => ["admin", "agent", "log"] as const,
  adminAgentActions: () => ["admin", "agent", "actions"] as const,
  runtimeInfo: () => ["runtime", "info"] as const,
  // §11 — Agent knowledge tools.
  documentExtraction: (did: string) => [...ws(), "document", did, "extraction"] as const,
  documentExtractionPages: (did: string) =>
    [...ws(), "document", did, "extraction", "pages"] as const,
  documentExtractionPage: (did: string, page: number) =>
    [...ws(), "document", did, "extraction", "pages", page] as const,
  kbSearch: (q: string) => [...ws(), "kb", "search", q] as const,
  kbDoc: (kind: "instruction" | "document", id: string, page: number = 1) =>
    [...ws(), "kb", "doc", kind, id, page] as const,
  kbSystemDocs: (role?: string) => [...ws(), "kb", "system_docs", role ?? "all"] as const,
  kbSystemDoc: (slug: string) => [...ws(), "kb", "system_docs", slug] as const,
  adminAgentDocs: () => ["admin", "agent_docs"] as const,
  adminAgentDoc: (slug: string) => ["admin", "agent_docs", slug] as const,
} as const;
