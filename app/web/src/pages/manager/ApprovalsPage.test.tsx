import { cleanup, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import ApprovalsPage from "@/pages/manager/ApprovalsPage";
import AgentSidebar from "@/components/AgentSidebar";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import {
  __resetQueryKeyGetterForTests,
  registerQueryKeyWorkspaceGetter,
} from "@/lib/queryKeys";
import { makeTestQueryClient, renderWithProviders } from "@/test/render";
import { installFetchRouteHandlers } from "@/test/helpers";

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  registerWorkspaceSlugGetter(() => "crewday");
  registerQueryKeyWorkspaceGetter(() => "crewday");
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("ApprovalsPage + manager AgentSidebar co-mount (cd-tifg4)", () => {
  // The manager /approvals desk (full ApprovalRequest[] list) and the manager
  // rail (web_owner_sidebar-filtered AgentAction[] cards) both subscribe to
  // qk.approvals(). They must share ONE raw-list cache blob: the desk shows
  // every row (desk-only + channel) and the rail derives only its own channel.
  it("desk shows desk-only + channel rows while the rail shows only its channel", async () => {
    const env = installFetchRouteHandlers([
      {
        path: "/w/crewday/api/v1/agent/manager/log",
        respond: { body: [] },
      },
      {
        path: "/w/crewday/api/v1/approvals",
        respond: {
          body: {
            data: [
              {
                // Desk-only row: for_user_id IS NULL, no inline channel. The
                // desk's primary content; must NOT leak into the rail.
                id: "appr_desk",
                workspace_id: "ws_1",
                requester_actor_id: null,
                for_user_id: null,
                inline_channel: null,
                resolved_user_mode: null,
                status: "pending",
                decided_by: null,
                decided_at: null,
                decision_note_md: null,
                expires_at: null,
                created_at: "2026-05-10T10:00:00Z",
                action_json: {
                  tool_name: "payroll.run",
                  tool_input: { period: "2026-03" },
                  card_summary: "Run March payroll",
                  card_risk: "high",
                },
                result_json: null,
              },
              {
                // Channel row destined for the manager rail.
                id: "appr_inline",
                workspace_id: "ws_1",
                requester_actor_id: null,
                for_user_id: "u_mgr",
                inline_channel: "web_owner_sidebar",
                resolved_user_mode: "strict",
                status: "pending",
                decided_by: null,
                decided_at: null,
                decision_note_md: null,
                expires_at: null,
                created_at: "2026-05-10T10:01:00Z",
                action_json: {
                  tool_name: "properties.create",
                  tool_input: { name: "Oak House" },
                  card_summary: "Create Oak House?",
                  card_risk: "medium",
                },
                result_json: null,
              },
            ],
            next_cursor: null,
            has_more: false,
          },
        },
      },
    ]);

    try {
      renderWithProviders(
        <MemoryRouter initialEntries={["/w/crewday/approvals"]}>
          <ApprovalsPage />
          <AgentSidebar agentRole="manager" />
        </MemoryRouter>,
        { queryClient: makeTestQueryClient() },
      );

      // Desk renders both rows as list items (desk-only is not dropped).
      await waitFor(() => {
        expect(screen.getAllByRole("listitem")).toHaveLength(2);
      });
      expect(screen.getByText("payroll.run")).toBeInTheDocument();
      expect(screen.getByText("properties.create")).toBeInTheDocument();

      // The desk-only row's summary appears exactly once — on the desk only.
      // (The rail projects it away, so there is no second copy.)
      expect(screen.getByText("Run March payroll")).toBeInTheDocument();

      const rail = screen.getByRole("complementary", { name: "Agent sidebar" });
      // Rail excludes the desk-only row entirely.
      expect(within(rail).queryByText("Run March payroll")).toBeNull();
      expect(within(rail).queryByText("payroll.run")).toBeNull();
      // Rail shows only its own channel row (card_summary + detail).
      expect(within(rail).getByText("Pending approvals")).toBeInTheDocument();
      expect(
        within(rail).getAllByText("Create Oak House?").length,
      ).toBeGreaterThanOrEqual(1);

      // Exactly one channel card in the rail.
      expect(within(rail).getByText("1")).toBeInTheDocument();
    } finally {
      env.restore();
    }
  });
});
