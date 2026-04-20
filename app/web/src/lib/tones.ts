// Semantic tone lookups shared across manager desks. Keep each map
// narrow and typed against the source enum in types/api.ts so the
// compiler catches an enum drift. A design-system rename (e.g.
// "good" → "sky") is one edit here instead of six.

import type {
  ApprovalRequest,
  AssetCondition,
  AssetStatus,
  AuditEntry,
  ExpenseStatus,
  Instruction,
  Issue,
  Task,
} from "@/types/api";

export type Tone = "moss" | "rust" | "sand" | "sky" | "ghost";

type Risk = "low" | "medium" | "high";

export const ASSET_CONDITION_TONE: Record<AssetCondition, "moss" | "sand" | "rust"> = {
  new: "moss",
  good: "moss",
  fair: "sand",
  poor: "rust",
  needs_replacement: "rust",
};

export const ASSET_STATUS_TONE: Record<AssetStatus, "moss" | "sand" | "rust" | "ghost"> = {
  active: "moss",
  in_repair: "sand",
  decommissioned: "ghost",
  disposed: "rust",
};

// draft / submitted never reach the table that renders these, so the
// map is keyed on the decided statuses only.
export const EXPENSE_STATUS_TONE: Record<Exclude<ExpenseStatus, "draft" | "submitted">, "moss" | "rust" | "sky"> = {
  approved: "moss",
  rejected: "rust",
  reimbursed: "sky",
};

export const RISK_TONE: Record<Risk, "sky" | "sand" | "rust"> = {
  low: "sky",
  medium: "sand",
  high: "rust",
};

// Runtime-validated alias for ApprovalRequest.risk — same underlying
// enum, named for its call site so the import reads cleanly.
export const APPROVAL_RISK_TONE: Record<ApprovalRequest["risk"], "sky" | "sand" | "rust"> = RISK_TONE;

export const TASK_STATUS_TONE: Record<Task["status"], "moss" | "sky" | "ghost" | "rust" | "sand"> = {
  scheduled: "ghost",
  pending: "ghost",
  in_progress: "sky",
  completed: "moss",
  skipped: "rust",
  cancelled: "rust",
  overdue: "sand",
};

export const ISSUE_SEVERITY_TONE: Record<Issue["severity"], "ghost" | "sand" | "rust"> = {
  low: "ghost",
  normal: "sand",
  high: "rust",
  urgent: "rust",
};

export const ISSUE_STATUS_TONE: Record<Issue["status"], "sand" | "sky" | "moss" | "ghost"> = {
  open: "sand",
  in_progress: "sky",
  resolved: "moss",
  wont_fix: "ghost",
};

export const ACTOR_KIND_TONE: Record<AuditEntry["actor_kind"], "moss" | "sky" | "ghost"> = {
  user: "moss",
  agent: "sky",
  system: "ghost",
};

export const GRANT_ROLE_TONE: Record<NonNullable<AuditEntry["actor_grant_role"]>, "moss" | "sand" | "sky" | "ghost" | "rust"> = {
  manager: "moss",
  worker: "sand",
  client: "sky",
  guest: "ghost",
  admin: "rust",
};

export const INSTRUCTION_SCOPE_TONE: Record<Instruction["scope"], "sky" | "moss" | "sand"> = {
  global: "sky",
  property: "moss",
  area: "sand",
};
