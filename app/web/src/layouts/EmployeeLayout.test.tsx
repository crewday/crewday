import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import { type ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setAuthenticated } from "@/auth/authStore";
import { __resetAuthStoreForTests } from "@/auth/useAuth";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { installFetch, jsonResponse } from "@/test/helpers";
import type { AuthMe } from "@/auth/types";
import type { Me } from "@/types/api";
import EmployeeLayout from "./EmployeeLayout";

vi.mock("@/components/AgentSidebar", () => ({
  default: function MockAgentSidebar({ role }: { role: string }): ReactElement {
    return <aside data-testid="agent-sidebar">agent:{role}</aside>;
  },
}));

vi.mock("@/lib/offlineQueue", () => ({
  usePendingMutationCount: () => 0,
}));

function authMe(): AuthMe {
  return {
    user_id: "usr_worker",
    display_name: "Worker User",
    email: "worker@example.com",
    current_workspace_id: "ws_1",
    is_deployment_admin: false,
    available_workspaces: [
      {
        workspace: {
          id: "ws_1",
          name: "Acme",
          timezone: "UTC",
          default_currency: "USD",
          default_country: "US",
          default_locale: "en",
        },
        grant_role: "worker",
        binding_org_id: null,
        source: "workspace_grant",
      },
    ],
  };
}

function workspaceMe(): Me {
  return {
    role: "employee",
    theme: "system",
    agent_sidebar_collapsed: false,
    employee: {
      id: "emp_1",
      name: "Worker User",
      roles: ["housekeeper"],
      properties: [],
      avatar_initials: "WU",
      avatar_file_id: null,
      avatar_url: null,
      phone: "",
      email: "worker@example.com",
      started_on: "2026-01-01",
      capabilities: {},
      workspaces: ["ws_1"],
      villas: [],
      language: "en",
      weekly_availability: {},
      evidence_policy: "inherit",
      preferred_locale: "en",
      settings_override: {},
    },
    manager_name: "",
    today: "2026-05-09",
    now: "2026-05-09T08:00:00Z",
    user_id: "usr_worker",
    agent_approval_mode: "strict",
    current_workspace_id: "ws_1",
    available_workspaces: authMe().available_workspaces,
    client_binding_org_ids: [],
    is_deployment_admin: false,
    is_deployment_owner: false,
  };
}

function renderEmployeeLayout(path = "/w/ws_1/today"): void {
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("ws_1");
  setAuthenticated(authMe());
  installFetch(({ url }) => {
    const parsed = new URL(url, "http://crewday.test");
    if (parsed.pathname === "/w/ws_1/api/v1/me") return jsonResponse(workspaceMe());
    if (parsed.pathname === "/w/ws_1/api/v1/bookings") return jsonResponse([]);
    throw new Error(`Unscripted fetch: ${url}`);
  });

  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <WorkspaceProvider>
          <Routes>
            <Route element={<EmployeeLayout />}>
              <Route path="/w/ws_1/today" element={<main>Today</main>} />
              <Route path="/w/ws_1/schedule" element={<main>Schedule</main>} />
              <Route path="/w/ws_1/chat" element={<main data-testid="chat-view">Chat</main>} />
            </Route>
          </Routes>
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<EmployeeLayout>", () => {
  it("keeps the phone Chat tab and desktop agent sidebar in the worker shell", async () => {
    renderEmployeeLayout();

    const bottomNav = screen.getByRole("navigation", { name: "Bottom navigation" });
    expect(within(bottomNav).getByRole("link", { name: /Chat/i })).toHaveAttribute("href", "/w/ws_1/chat");
    expect(screen.getByTestId("agent-sidebar")).toHaveTextContent("agent:employee");
  });

  it("marks /chat as the full-screen phone chat surface", async () => {
    renderEmployeeLayout("/w/ws_1/chat");

    expect(await screen.findByTestId("chat-view")).toBeInTheDocument();
    expect(screen.getByTestId("chat-view").closest(".phone")).toHaveClass("phone--chat");
  });

  it("marks exact-match employee side nav links active on prefixed routes", async () => {
    renderEmployeeLayout("/w/ws_1/schedule");

    const mainNav = screen.getByRole("complementary", { name: "Main navigation" });
    const schedule = within(mainNav).getByRole("link", { name: "Schedule" });
    expect(schedule).toHaveAttribute("href", "/w/ws_1/schedule");
    expect(schedule).toHaveClass("nav-link--active");
  });
});
