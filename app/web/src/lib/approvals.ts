import { fetchJson } from "@/lib/api";
import type {
  ApprovalRequest,
  ApprovalRequestPayload,
  ApprovalsListResponse,
  GateSource,
} from "@/types/api";

type ApprovalRisk = ApprovalRequest["risk"];

function isApprovalRisk(value: unknown): value is ApprovalRisk {
  return value === "low" || value === "medium" || value === "high";
}

function riskFromAction(action: Record<string, unknown>): ApprovalRisk {
  const value = action.card_risk;
  return isApprovalRisk(value) ? value : "low";
}

function stringFromAction(action: Record<string, unknown>, key: string): string | null {
  const value = action[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

function gateSourceFromAction(action: Record<string, unknown>): GateSource {
  const value = action.pre_approval_source;
  if (
    value === "workspace_always" ||
    value === "workspace_configurable" ||
    value === "user_auto_annotation" ||
    value === "user_strict_mutation"
  ) {
    return value;
  }
  return "workspace_configurable";
}

function targetFromAction(action: Record<string, unknown>): string {
  const toolInput = action.tool_input;
  if (typeof toolInput === "object" && toolInput !== null && !Array.isArray(toolInput)) {
    for (const [key, value] of Object.entries(toolInput)) {
      if (typeof value === "string" || typeof value === "number") {
        return `${key}: ${value}`;
      }
    }
  }
  return stringFromAction(action, "card_summary") ?? "Approval request";
}

export function approvalRequestFromPayload(payload: ApprovalRequestPayload): ApprovalRequest {
  const action = payload.action_json;
  const summary = stringFromAction(action, "card_summary") ?? "Review proposed agent action";
  const toolName = stringFromAction(action, "tool_name") ?? "agent action";
  const inlineChannel = payload.inline_channel ?? "desk_only";
  return {
    id: payload.id,
    agent: "Agent",
    action: toolName,
    target: targetFromAction(action),
    reason: summary,
    requested_at: payload.created_at,
    risk: riskFromAction(action),
    diff: [],
    gate_source: gateSourceFromAction(action),
    gate_destination: inlineChannel === "desk_only" ? "desk" : "inline_chat",
    inline_channel: inlineChannel,
    card_summary: summary,
    card_fields: [],
    for_user_id: payload.for_user_id,
    resolved_user_mode: payload.resolved_user_mode,
  };
}

export async function fetchApprovals(): Promise<ApprovalRequest[]> {
  const approvals: ApprovalRequest[] = [];
  let cursor: string | null = null;
  do {
    const path: string = cursor
      ? `/api/v1/approvals?${new URLSearchParams({ cursor }).toString()}`
      : "/api/v1/approvals";
    const response: ApprovalsListResponse = await fetchJson<ApprovalsListResponse>(path);
    approvals.push(...response.data.map(approvalRequestFromPayload));
    cursor = response.has_more ? response.next_cursor : null;
  } while (cursor);
  return approvals;
}
