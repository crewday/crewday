import type { ApiToken } from "@/types/api";

export type TokenStatus = "active" | "expiring" | "expired" | "revoked";

export type TokenKind = "scoped" | "delegated";

// §03 API tokens — manager surface. Scoped + delegated workspace
// tokens live here. Personal access tokens (kind === "personal")
// are deliberately hidden — they live on /me, revocable only by
// the subject.
export const WORKSPACE_SCOPES: string[] = [
  "tasks:read", "tasks:write", "tasks:complete",
  "users:read", "properties:read", "stays:read",
  "inventory:read", "inventory:adjust", "time:read",
  "expenses:read", "expenses:approve",
  "payroll:read", "payroll:run",
  "instructions:read", "messaging:read", "llm:call",
];

export function statusOf(tok: ApiToken): TokenStatus {
  if (tok.revoked_at) return "revoked";
  const exp = tok.expires_at ? new Date(tok.expires_at).getTime() : null;
  if (exp !== null && exp < Date.now()) return "expired";
  if (exp !== null && exp - Date.now() < 14 * 864e5) return "expiring";
  return "active";
}

export const STATUS_LABEL: Record<TokenStatus, string> = {
  active: "Active",
  expiring: "Expires soon",
  expired: "Expired",
  revoked: "Revoked",
};
