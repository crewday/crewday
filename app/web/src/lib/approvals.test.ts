import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import {
  approvalRequestFromPayload,
  fetchApprovals,
} from "@/lib/approvals";
import type { ApprovalRequestPayload } from "@/types/api";

interface FakeResponse {
  status?: number;
  body: unknown;
}

function installFetch(responses: FakeResponse[]): {
  calls: Array<{ url: string; init: RequestInit }>;
  restore: () => void;
} {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    const next = responses.shift();
    if (!next) throw new Error(`Unexpected fetch call: ${resolved}`);
    const status = next.status ?? 200;
    const ok = status >= 200 && status < 300;
    const text =
      typeof next.body === "string"
        ? next.body
        : next.body === null || next.body === undefined
          ? ""
          : JSON.stringify(next.body);
    return {
      ok,
      status,
      statusText: ok ? "OK" : "Error",
      text: async () => text,
    } as unknown as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function payload(overrides: Partial<ApprovalRequestPayload> = {}): ApprovalRequestPayload {
  return {
    id: "appr-1",
    workspace_id: "ws-1",
    requester_actor_id: "agent-user-1",
    for_user_id: "manager-1",
    inline_channel: "web_owner_sidebar",
    resolved_user_mode: "strict",
    status: "pending",
    decided_by: null,
    decided_at: null,
    decision_note_md: null,
    expires_at: "2026-04-30T09:00:00Z",
    created_at: "2026-04-28T09:00:00Z",
    action_json: {
      tool_name: "payroll.issue_payslip",
      tool_input: { employee_id: "emp-1", period: "2026-04" },
      card_summary: "Issue Jean's April payslip.",
      card_risk: "high",
      pre_approval_source: "workspace_always",
    },
    result_json: null,
    ...overrides,
  };
}

beforeEach(() => {
  __resetApiProvidersForTests();
  registerWorkspaceSlugGetter(() => "acme");
  document.cookie = "crewday_csrf=; path=/; max-age=0";
});

afterEach(() => {
  __resetApiProvidersForTests();
});

describe("approvalRequestFromPayload", () => {
  it("projects the API envelope row into the manager approvals card shape", () => {
    const out = approvalRequestFromPayload(payload());

    expect(out).toMatchObject({
      id: "appr-1",
      agent: "Agent",
      action: "payroll.issue_payslip",
      target: "employee_id: emp-1",
      reason: "Issue Jean's April payslip.",
      requested_at: "2026-04-28T09:00:00Z",
      risk: "high",
      gate_source: "workspace_always",
      gate_destination: "inline_chat",
      inline_channel: "web_owner_sidebar",
      card_summary: "Issue Jean's April payslip.",
      card_fields: [],
      for_user_id: "manager-1",
      resolved_user_mode: "strict",
    });
  });

  it("uses stable fallbacks for legacy action payloads", () => {
    const out = approvalRequestFromPayload(
      payload({
        inline_channel: null,
        resolved_user_mode: null,
        action_json: {
          pre_approval_source: "manual",
          card_risk: "urgent",
        },
      }),
    );

    expect(out.action).toBe("agent action");
    expect(out.target).toBe("Approval request");
    expect(out.reason).toBe("Review proposed agent action");
    expect(out.risk).toBe("low");
    expect(out.gate_source).toBe("workspace_configurable");
    expect(out.gate_destination).toBe("desk");
    expect(out.inline_channel).toBe("desk_only");
  });
});

describe("fetchApprovals", () => {
  it("unwraps every production list page", async () => {
    const transport = installFetch([
      {
        body: {
          data: [payload({ id: "appr-1" })],
          next_cursor: "appr-1",
          has_more: true,
        },
      },
      {
        body: {
          data: [payload({ id: "appr-2" })],
          next_cursor: null,
          has_more: false,
        },
      },
    ]);

    try {
      const out = await fetchApprovals();

      expect(transport.calls).toHaveLength(2);
      expect(transport.calls[0]?.url).toBe("/w/acme/api/v1/approvals");
      expect(transport.calls[1]?.url).toBe("/w/acme/api/v1/approvals?cursor=appr-1");
      expect(out.map((approval) => approval.id)).toEqual(["appr-1", "appr-2"]);
    } finally {
      transport.restore();
    }
  });
});
