import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { installFetchRouteHandlers } from "@/test/helpers";
import DashboardPage from "./DashboardPage";

const EMPLOYEE = {
  id: "emp_1",
  name: "Maya Santos",
  roles: ["housekeeper"],
  properties: [],
  avatar_initials: "MS",
  avatar_file_id: null,
  avatar_url: null,
  phone: "+351 555 0100",
  email: "maya@example.com",
  started_on: "2026-01-01",
  capabilities: {},
  workspaces: ["ws_1"],
  villas: [],
  language: "en",
  weekly_availability: {},
  evidence_policy: "inherit",
  preferred_locale: null,
  settings_override: {},
};

function installFetch() {
  return installFetchRouteHandlers([
    {
      path: "/api/v1/auth/me",
      respond: {
        body: {
          user_id: "usr_1",
          display_name: "Mina",
          email: "mina@example.com",
          available_workspaces: [],
          current_workspace_id: "ws_1",
        },
      },
    },
    {
      path: "/api/v1/me/workspaces",
      respond: {
        body: [
          {
            workspace_id: "ws_1",
            slug: "acme",
            name: "Acme",
            current_role: "manager",
            last_seen_at: null,
            settings_override: {},
          },
        ],
      },
    },
    {
      path: "/w/acme/api/v1/me",
      respond: {
        body: {
          role: "manager",
          theme: "system",
          agent_sidebar_collapsed: false,
          employee: EMPLOYEE,
          manager_name: "Mina Patel",
          today: "2026-04-29",
          now: "2026-04-29T10:00:00Z",
          user_id: "usr_1",
          agent_approval_mode: "ask",
          current_workspace_id: "ws_1",
          available_workspaces: [],
          client_binding_org_ids: [],
          is_deployment_admin: false,
          is_deployment_owner: false,
        },
      },
    },
    {
      path: "/w/acme/api/v1/dashboard",
      respond: {
        body: {
          on_booking: [],
          by_status: { completed: [], in_progress: [], pending: [] },
          pending_approvals: [],
          pending_expenses: [],
          pending_leaves: [],
          open_issues: [],
          stays_today: [],
          properties: [],
          employees: [EMPLOYEE],
        },
      },
    },
    { path: "/w/acme/api/v1/properties", respond: { body: [] } },
  ]);
}

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WorkspaceProvider>
          <DashboardPage />
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  });
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<DashboardPage>", () => {
  it("opens the real new-task dialog and disables broadcast messaging", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "+ New task" }));
      expect(await screen.findByRole("heading", { name: "New task" })).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "More actions" }));
      expect(screen.getByRole("menuitem", { name: /Broadcast message/ })).toBeDisabled();
      expect(screen.getByText("Broadcast messaging is not implemented yet.")).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });
});
