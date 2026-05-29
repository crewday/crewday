import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { type ReactElement } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RoleProvider } from "@/context/RoleContext";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { setAuthenticated } from "@/auth/authStore";
import { __resetAuthStoreForTests } from "@/auth/useAuth";
import type { AuthMe } from "@/auth/types";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { installFakeIndexedDb } from "@/test/fakeIndexedDb";
import { installFetch, installFetchRouteHandlers } from "@/test/helpers";
import App from "./App";

let restoreIndexedDb: (() => void) | null = null;

vi.mock("@/components/AgentSidebar", () => ({
  default: function MockAgentSidebar(): ReactElement {
    return <aside data-testid="agent-sidebar">Agent</aside>;
  },
}));

vi.mock("@/layouts/PreviewShell", async () => {
  const { Outlet } = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return {
    default: function MockPreviewShell(): ReactElement {
      return <Outlet />;
    },
  };
});

function authMe(grantRole: "worker" | "manager" = "worker"): AuthMe {
  return {
    user_id: "usr_worker",
    display_name: "Worker User",
    email: "worker@example.test",
    current_workspace_id: "acme",
    is_deployment_admin: false,
    available_workspaces: [
      {
        workspace: {
          id: "acme",
          name: "Acme",
          timezone: "UTC",
          default_currency: "USD",
          default_country: "US",
          default_locale: "en",
        },
        grant_role: grantRole,
        binding_org_id: null,
        source: "workspace_grant",
      },
    ],
  };
}

function workspaceMe(role: "worker" | "manager" = "worker"): unknown {
  return {
    role,
    theme: "system",
    agent_sidebar_collapsed: false,
    employee: {
      id: "emp_1",
      user_id: "usr_worker",
      name: "Worker User",
      first_name: "Worker",
      last_name: "User",
      email: "worker@example.test",
      phone: null,
      avatar_url: null,
      avatar_initials: "WU",
      roles: ["housekeeper"],
    },
    manager_name: "Manager User",
    today: "2026-05-29",
    now: "2026-05-29T10:00:00Z",
    user_id: "usr_worker",
    agent_approval_mode: "confirm",
    current_workspace_id: "acme",
    available_workspaces: [],
    client_binding_org_ids: [],
    is_deployment_admin: false,
    is_deployment_owner: false,
  };
}

function LocationProbe(): ReactElement {
  const location = useLocation();
  return <span data-testid="location">{location.pathname}</span>;
}

function Harness(): ReactElement {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/w/acme/notifications"]}>
        <RoleProvider>
          <WorkspaceProvider>
            <LocationProbe />
            <App />
          </WorkspaceProvider>
        </RoleProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  restoreIndexedDb = installFakeIndexedDb();
  vi.spyOn(preferences, "readRoleCookie").mockReturnValue("employee");
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
  setAuthenticated(authMe());
});

afterEach(() => {
  cleanup();
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  restoreIndexedDb?.();
  restoreIndexedDb = null;
  vi.restoreAllMocks();
});

describe("Notifications app route", () => {
  it("keeps an authenticated worker broadcast deep link on the notifications page", async () => {
    installFetchRouteHandlers([
      {
        path: "/w/acme/api/v1/me",
        respond: { body: workspaceMe() },
      },
      {
        path: "/w/acme/api/v1/bookings",
        respond: { body: [] },
      },
      {
        path: "/w/acme/api/v1/messaging/notifications?limit=100",
        respond: {
          body: {
            data: [],
            next_cursor: null,
            has_more: false,
            total_estimate: 0,
          },
        },
      },
    ]);

    render(<Harness />);

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/acme/notifications");
    });
    expect(await screen.findByRole("heading", { name: "Notifications" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Notifications" })).not.toHaveClass(
      "nav-link--phone-hidden",
    );
    expect(screen.queryByTestId("today-page")).not.toBeInTheDocument();
  });

  it("keeps an authenticated manager broadcast deep link on the notifications page", async () => {
    vi.mocked(preferences.readRoleCookie).mockReturnValue("manager");
    setAuthenticated(authMe("manager"));
    installFetch(({ url }) => {
      const parsed = new URL(url, "http://crewday.test");
      const path = parsed.pathname + parsed.search;
      if (path === "/w/acme/api/v1/me") {
        return Response.json(workspaceMe("manager"));
      }
      if (path === "/w/acme/api/v1/messaging/notifications?limit=100") {
        return Response.json({
          data: [],
          next_cursor: null,
          has_more: false,
          total_estimate: 0,
        });
      }
      if (path.startsWith("/w/acme/api/v1/permissions/resolved/self?")) {
        return Response.json({ effect: "allow" });
      }
      throw new Error(`Unscripted fetch: ${path}`);
    });

    render(<Harness />);

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/acme/notifications");
    });
    expect(await screen.findByRole("heading", { name: "Notifications" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Notifications" })).not.toHaveClass(
      "nav-link--phone-hidden",
    );
  });
});
