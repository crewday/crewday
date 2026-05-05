import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { type ReactElement } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { RoleProvider } from "@/context/RoleContext";
import { setAuthenticated } from "@/auth/authStore";
import { __resetAuthStoreForTests } from "@/auth/useAuth";
import type { AuthMe } from "@/auth/types";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { installFetch, jsonResponse } from "@/test/helpers";
import App from "./App";

const mockRenders = vi.hoisted(() => ({
  employeeLayout: vi.fn(),
  managerLayout: vi.fn(),
  chatPage: vi.fn(),
  dashboardPage: vi.fn(),
}));

vi.mock("@/layouts/PreviewShell", async () => {
  const { Outlet: RouterOutlet } = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return {
    default: function MockPreviewShell(): ReactElement {
      return <RouterOutlet />;
    },
  };
});

vi.mock("@/layouts/EmployeeLayout", async () => {
  const { Outlet: RouterOutlet } = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return {
    default: function MockEmployeeLayout(): ReactElement {
      mockRenders.employeeLayout();
      return (
        <div data-testid="employee-layout">
          <RouterOutlet />
        </div>
      );
    },
  };
});

vi.mock("@/layouts/ManagerLayout", async () => {
  const { Outlet: RouterOutlet } = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return {
    default: function MockManagerLayout(): ReactElement {
      mockRenders.managerLayout();
      return (
        <div data-testid="manager-layout">
          <RouterOutlet />
        </div>
      );
    },
  };
});

vi.mock("@/pages/employee/ChatPage", () => ({
  default: function MockChatPage(): ReactElement {
    mockRenders.chatPage();
    return <main data-testid="worker-chat">Worker operations agent</main>;
  },
}));

vi.mock("@/pages/manager/DashboardPage", () => ({
  default: function MockDashboardPage(): ReactElement {
    mockRenders.dashboardPage();
    return <main data-testid="manager-dashboard">Manager dashboard</main>;
  },
}));

function authMeFor(role: "employee" | "manager"): AuthMe {
  return {
    user_id: role === "manager" ? "usr_manager" : "usr_worker",
    display_name: role === "manager" ? "Manager User" : "Worker User",
    email: role === "manager" ? "manager@example.com" : "worker@example.com",
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
        grant_role: role === "manager" ? "manager" : "worker",
        binding_org_id: null,
        source: "workspace_grant",
      },
    ],
    current_workspace_id: "ws_1",
    is_deployment_admin: false,
  };
}

function installPermissionAllowFetch(): void {
  installFetch(({ url }) => {
    const parsed = new URL(url, "http://crewday.test");
    if (parsed.pathname === "/w/ws_1/api/v1/permissions/resolved/self") {
      return jsonResponse({
        effect: "allow",
        source_layer: "default_allow",
        source_rule_id: null,
        matched_groups: ["managers"],
      });
    }
    throw new Error(`Unscripted fetch: ${url}`);
  });
}

function LocationProbe(): ReactElement {
  const location = useLocation();
  return <span data-testid="location">{location.pathname}</span>;
}

function renderAppAt(path: string, role: "employee" | "manager"): void {
  vi.spyOn(preferences, "readRoleCookie").mockReturnValue(role);
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("ws_1");
  setAuthenticated(authMeFor(role));

  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <RoleProvider>
          <WorkspaceProvider>
            <LocationProbe />
            <App />
          </WorkspaceProvider>
        </RoleProvider>
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

describe("App /chat role routing", () => {
  it("redirects manager direct navigation away from worker full-screen chat", async () => {
    installPermissionAllowFetch();
    renderAppAt("/chat", "manager");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
    });

    expect(await screen.findByTestId("manager-dashboard")).toBeInTheDocument();
    expect(mockRenders.managerLayout).toHaveBeenCalled();
    expect(mockRenders.employeeLayout).not.toHaveBeenCalled();
    expect(mockRenders.chatPage).not.toHaveBeenCalled();
    expect(screen.queryByTestId("worker-chat")).toBeNull();
  });

  it("keeps worker /chat on the full-screen operations agent route", async () => {
    renderAppAt("/chat", "employee");

    expect(await screen.findByTestId("worker-chat")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/chat");
    expect(mockRenders.employeeLayout).toHaveBeenCalled();
    expect(mockRenders.chatPage).toHaveBeenCalled();
    expect(mockRenders.managerLayout).not.toHaveBeenCalled();
  });
});
