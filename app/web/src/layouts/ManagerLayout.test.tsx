import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { type ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setAuthenticated } from "@/auth/authStore";
import { __resetAuthStoreForTests } from "@/auth/useAuth";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests, qk, registerQueryKeyWorkspaceGetter } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { installFetch, jsonResponse } from "@/test/helpers";
import type { AuthMe } from "@/auth/types";
import type { Me } from "@/types/api";
import PageHeader from "@/components/PageHeader";
import ManagerLayout from "./ManagerLayout";

const EXPECTED_NAV_ACTIONS = [
  "employees.read",
  "properties.read",
  "stays.read",
  "tasks.create",
  "availability_overrides.view_others",
  "scope.view",
  "instructions.edit",
  "assets.manage_documents",
  "approvals.read",
  "leaves.view_others",
  "expenses.approve",
  "payroll.view_other",
  "permissions.edit_rules",
  "audit_log.view",
  "scope.edit_settings",
  "api_tokens.manage",
];

vi.mock("@/components/AgentSidebar", () => ({
  default: function MockAgentSidebar({ agentRole }: { agentRole: string }): ReactElement {
    return <aside data-testid="agent-sidebar">agent:{agentRole}</aside>;
  },
}));

function authMe(): AuthMe {
  // code-health: ignore[nloc] Manager layout tests keep the full auth grant fixture beside permission-filtered navigation assertions.
  return {
    user_id: "usr_manager",
    display_name: "Manager User",
    email: "manager@example.com",
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
        grant_role: "manager",
        binding_org_id: null,
        source: "workspace_grant",
      },
    ],
  };
}

function workspaceMe(overrides: Partial<Me> = {}): Me {
  return {
    role: "manager",
    theme: "system",
    agent_sidebar_collapsed: false,
    employee: {
      id: "emp_1",
      name: "Manager User",
      roles: ["property_manager"],
      properties: [],
      avatar_initials: "MU",
      avatar_file_id: null,
      avatar_url: null,
      phone: "",
      email: "manager@example.com",
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
    manager_name: "Manager User",
    today: "2026-05-06",
    now: "2026-05-06T00:00:00Z",
    user_id: "usr_manager",
    agent_approval_mode: "strict",
    current_workspace_id: "ws_1",
    available_workspaces: authMe().available_workspaces,
    client_binding_org_ids: [],
    is_deployment_admin: false,
    is_deployment_owner: false,
    ...overrides,
  };
}

function renderManagerLayout(
  allowed: Set<string>,
  initialPath = "/w/ws_1/dashboard",
  options: {
    workspaceMeResponse?: Me;
    deferredWorkspaceMeResponse?: Promise<Response>;
    deferWorkspaceMe?: boolean;
    cachedWorkspaceMe?: unknown;
    cachedAuthMe?: AuthMe;
    onWorkspaceMeRequest?: () => void;
  } = {},
): string[] {
  const permissionProbes: string[] = [];
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("ws_1");
  setAuthenticated(authMe());
  installFetch(({ url }) => {
    const parsed = new URL(url, "http://crewday.test");
    if (parsed.pathname === "/w/ws_1/api/v1/me") {
      options.onWorkspaceMeRequest?.();
      if (options.deferredWorkspaceMeResponse) return options.deferredWorkspaceMeResponse;
      if (options.deferWorkspaceMe) return new Promise<Response>(() => {});
      return jsonResponse(options.workspaceMeResponse ?? workspaceMe());
    }
    if (parsed.pathname === "/w/ws_1/api/v1/permissions/resolved/self") {
      const actionKey = parsed.searchParams.get("action_key") ?? "";
      permissionProbes.push(actionKey);
      return jsonResponse({
        effect: allowed.has(actionKey) ? "allow" : "deny",
        source_layer: "test",
        source_rule_id: null,
        matched_groups: allowed.has(actionKey) ? ["managers"] : [],
      });
    }
    throw new Error(`Unscripted fetch: ${url}`);
  });

  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  registerQueryKeyWorkspaceGetter(() => "ws_1");
  if (options.cachedAuthMe) queryClient.setQueryData(qk.authMe(), options.cachedAuthMe);
  if (options.cachedWorkspaceMe) queryClient.setQueryData(qk.me(), options.cachedWorkspaceMe);

  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialPath]}>
        <WorkspaceProvider>
          <Routes>
            <Route element={<ManagerLayout />}>
              <Route path="/w/ws_1/dashboard" element={<main><PageHeader title="Dashboard" /></main>} />
              <Route path="/w/ws_1/chat" element={<main data-testid="chat-view">Chat</main>} />
            </Route>
          </Routes>
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return permissionProbes;
}

function renderManagerLayoutWithDeferredPermissions(allowed: Set<string>): {
  permissionProbes: string[];
  resolvePermissions: () => void;
} {
  const permissionProbes: string[] = [];
  const pendingPermissions: Array<{
    actionKey: string;
    resolve: (response: Response) => void;
  }> = [];
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("ws_1");
  setAuthenticated(authMe());
  installFetch(({ url }) => {
    const parsed = new URL(url, "http://crewday.test");
    if (parsed.pathname === "/w/ws_1/api/v1/me") {
      return jsonResponse(workspaceMe());
    }
    if (parsed.pathname === "/w/ws_1/api/v1/permissions/resolved/self") {
      const actionKey = parsed.searchParams.get("action_key") ?? "";
      permissionProbes.push(actionKey);
      return new Promise<Response>((resolve) => {
        pendingPermissions.push({ actionKey, resolve });
      });
    }
    throw new Error(`Unscripted fetch: ${url}`);
  });

  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/w/ws_1/dashboard"]}>
        <WorkspaceProvider>
          <Routes>
            <Route element={<ManagerLayout />}>
              <Route path="/w/ws_1/dashboard" element={<main><PageHeader title="Dashboard" /></main>} />
            </Route>
          </Routes>
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );

  return {
    permissionProbes,
    resolvePermissions: () => {
      for (const pending of pendingPermissions) {
        pending.resolve(jsonResponse({
          effect: allowed.has(pending.actionKey) ? "allow" : "deny",
          source_layer: "test",
          source_rule_id: null,
          matched_groups: allowed.has(pending.actionKey) ? ["managers"] : [],
        }));
      }
    },
  };
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

describe("<ManagerLayout> permission-resolved navigation", () => {
  it("renders manager footer details from the workspace /me payload", async () => {
    const managerPayload = workspaceMe({
      manager_name: "Operations Lead",
      employee: {
        ...workspaceMe().employee,
        name: "Operations Lead",
        avatar_initials: "OL",
      },
    });

    renderManagerLayout(new Set(["employees.read"]), "/w/ws_1/dashboard", {
      workspaceMeResponse: managerPayload,
    });

    await waitFor(() => {
      expect(screen.getByText("Operations Lead")).toBeInTheDocument();
      expect(screen.getByText("OL")).toBeInTheDocument();
    });
  });

  it("falls back to auth identity while auth-shaped me data is cached, then renders workspace me", async () => {
    let resolveWorkspaceMe: (response: Response) => void = () => {};
    const deferredWorkspaceMeResponse = new Promise<Response>((resolve) => {
      resolveWorkspaceMe = resolve;
    });
    const workspaceMeRequests: string[] = [];
    const managerPayload = workspaceMe({
      manager_name: "Operations Lead",
      employee: {
        ...workspaceMe().employee,
        name: "Operations Lead",
        avatar_initials: "OL",
      },
    });

    renderManagerLayout(new Set(["employees.read"]), "/w/ws_1/dashboard", {
      cachedAuthMe: authMe(),
      cachedWorkspaceMe: authMe(),
      deferredWorkspaceMeResponse,
      onWorkspaceMeRequest: () => workspaceMeRequests.push("me"),
    });

    expect(screen.getByText("Manager User")).toBeInTheDocument();
    expect(screen.getByText("MU")).toBeInTheDocument();
    expect(screen.getByTestId("agent-sidebar")).toHaveTextContent("agent:manager");
    await waitFor(() => {
      expect(workspaceMeRequests).toEqual(["me"]);
    });

    resolveWorkspaceMe(jsonResponse(managerPayload));

    await waitFor(() => {
      expect(screen.getByText("Operations Lead")).toBeInTheDocument();
      expect(screen.getByText("OL")).toBeInTheDocument();
    });
  });

  it("keeps the fixed phone bottom row and puts extra allowed management destinations in nav", async () => {
    renderManagerLayout(new Set([
      "employees.read",
      "permissions.edit_rules",
      "payroll.view_other",
      "api_tokens.manage",
      "audit_log.view",
      "scope.edit_settings",
    ]));

    const bottomNav = screen.getByRole("navigation", { name: "Bottom navigation" });
    expect(within(bottomNav).getByRole("link", { name: /Today/i })).toBeInTheDocument();
    expect(within(bottomNav).getByRole("link", { name: /Schedule/i })).toBeInTheDocument();
    expect(within(bottomNav).getByRole("link", { name: /Chat/i })).toBeInTheDocument();
    expect(within(bottomNav).getByRole("link", { name: /Expenses/i })).toBeInTheDocument();
    expect(within(bottomNav).getByRole("link", { name: /Me/i })).toBeInTheDocument();

    await waitFor(
      () => {
        expect(screen.getByRole("link", { name: /Dashboard/i })).toBeInTheDocument();
        expect(screen.getByRole("link", { name: /Employees/i })).toBeInTheDocument();
        expect(screen.getByRole("link", { name: /Pay/i })).toBeInTheDocument();
        expect(screen.getByRole("link", { name: /Permissions/i })).toBeInTheDocument();
        expect(screen.getByRole("link", { name: /Audit log/i })).toBeInTheDocument();
        expect(screen.getByRole("link", { name: /API tokens/i })).toBeInTheDocument();
        expect(screen.getByRole("link", { name: "My profile" })).toBeInTheDocument();
        expect(screen.getByRole("link", { name: "Workspace settings" })).toBeInTheDocument();
      },
      { timeout: 3000 },
    );
    expect(screen.getByRole("link", { name: /Dashboard/i })).toHaveAttribute("href", "/w/ws_1/dashboard");
    expect(screen.getByRole("link", { name: /API tokens/i })).toHaveAttribute("href", "/w/ws_1/tokens");
    expect(screen.getByRole("link", { name: "My profile" })).toHaveAttribute("href", "/w/ws_1/me");
    expect(screen.getByRole("button", { name: "Open menu" })).toBeInTheDocument();
    expect(screen.getByTestId("agent-sidebar")).toHaveTextContent("agent:manager");
  });

  it("hides permission-gated management destinations when no actions are allowed", async () => {
    const probes = renderManagerLayout(new Set());

    await waitFor(
      () => {
        expect(probes).toContain("api_tokens.manage");
        expect(screen.queryByRole("link", { name: /Dashboard/i })).toBeNull();
        expect(screen.getByRole("button", { name: "Open menu" })).toBeInTheDocument();
      },
      { timeout: 3000 },
    );

    const bottomNav = screen.getByRole("navigation", { name: "Bottom navigation" });
    expect(within(bottomNav).getByRole("link", { name: /Today/i })).toBeInTheDocument();
    expect(within(bottomNav).getByRole("link", { name: /Schedule/i })).toBeInTheDocument();
    expect(within(bottomNav).getByRole("link", { name: /Chat/i })).toBeInTheDocument();
    expect(within(bottomNav).getByRole("link", { name: /Expenses/i })).toBeInTheDocument();
    expect(within(bottomNav).getByRole("link", { name: /Me/i })).toBeInTheDocument();
  });

  it("marks the manager shell as a full-screen chat surface on /chat", async () => {
    renderManagerLayout(new Set(), "/w/ws_1/chat");

    expect(await screen.findByTestId("chat-view")).toBeInTheDocument();
    expect(screen.getByTestId("chat-view").closest(".desk")).toHaveClass("desk--chat");
    expect(screen.getByTestId("agent-sidebar")).toHaveTextContent("agent:manager");
  });

  it("probes only unique action keys required by workspace nav links", async () => {
    const probes = renderManagerLayout(new Set(["employees.read"]));

    await waitFor(
      () => {
        expect(probes).toContain("api_tokens.manage");
      },
      { timeout: 3000 },
    );

    const unique = new Set(probes);
    expect(probes).toHaveLength(unique.size);
    expect(probes).toEqual(EXPECTED_NAV_ACTIONS);
  });

  it("starts manager nav permission probes together instead of waiting for each response", async () => {
    const { permissionProbes, resolvePermissions } = renderManagerLayoutWithDeferredPermissions(new Set([
      "employees.read",
      "permissions.edit_rules",
    ]));

    await waitFor(() => {
      expect(permissionProbes).toEqual(EXPECTED_NAV_ACTIONS);
    });

    resolvePermissions();

    await waitFor(() => {
      expect(screen.getByRole("link", { name: /Dashboard/i })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Permissions/i })).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: /API tokens/i })).toBeNull();
    });
  });
});
