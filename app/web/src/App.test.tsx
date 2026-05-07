import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { type ReactElement } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { RoleProvider } from "@/context/RoleContext";
import { setAuthenticated, setUnauthenticated } from "@/auth/authStore";
import { __resetAuthStoreForTests } from "@/auth/useAuth";
import type { AuthMe } from "@/auth/types";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { installFetch, jsonResponse } from "@/test/helpers";
import App from "./App";

const mockRenders = vi.hoisted(() => ({
  clientLayout: vi.fn(),
  clientPortfolioPage: vi.fn(),
  employeeLayout: vi.fn(),
  agentFetches: false,
  agentSidebar: vi.fn(),
  managerLayout: vi.fn(),
  chatPage: vi.fn(),
  dashboardPage: vi.fn(),
  schedulerPage: vi.fn(),
  apiTokensPage: vi.fn(),
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
  const { useEffect } = await vi.importActual<typeof import("react")>("react");
  function MockAgentSidebar({ role }: { role: "employee" | "manager" }): ReactElement {
    mockRenders.agentSidebar(role);
    useEffect(() => {
      if (!mockRenders.agentFetches) return;
      void fetch(`/w/ws_1/api/v1/agent/${role}/log`);
    }, [role]);
    return <aside data-testid="agent-sidebar">agent:{role}</aside>;
  }
  return {
    default: function MockEmployeeLayout(): ReactElement {
      mockRenders.employeeLayout();
      return (
        <div data-testid="employee-layout">
          <MockAgentSidebar role="employee" />
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
  const { useEffect } = await vi.importActual<typeof import("react")>("react");
  function MockAgentSidebar({ role }: { role: "employee" | "manager" }): ReactElement {
    mockRenders.agentSidebar(role);
    useEffect(() => {
      if (!mockRenders.agentFetches) return;
      void fetch(`/w/ws_1/api/v1/agent/${role}/log`);
    }, [role]);
    return <aside data-testid="agent-sidebar">agent:{role}</aside>;
  }
  return {
    default: function MockManagerLayout(): ReactElement {
      mockRenders.managerLayout();
      return (
        <div data-testid="manager-layout">
          <MockAgentSidebar role="manager" />
          <RouterOutlet />
        </div>
      );
    },
  };
});

vi.mock("@/layouts/ClientLayout", async () => {
  const { Outlet: RouterOutlet } = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return {
    default: function MockClientLayout(): ReactElement {
      mockRenders.clientLayout();
      return (
        <div data-testid="client-layout">
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

vi.mock("@/pages/SchedulerPage", () => ({
  default: function MockSchedulerPage(): ReactElement {
    mockRenders.schedulerPage();
    return <main data-testid="scheduler-page">Scheduler</main>;
  },
}));

vi.mock("@/pages/manager/ApiTokensPage", () => ({
  default: function MockApiTokensPage(): ReactElement {
    mockRenders.apiTokensPage();
    return <main data-testid="api-tokens-page">API tokens</main>;
  },
}));

vi.mock("@/pages/client/PortfolioPage", () => ({
  default: function MockClientPortfolioPage(): ReactElement {
    mockRenders.clientPortfolioPage();
    return <main data-testid="client-portfolio">Client portfolio</main>;
  },
}));

vi.mock("@/pages/public/LoginPage", () => ({
  default: function MockLoginPage(): ReactElement {
    return <main data-testid="login-page">Login</main>;
  },
}));

vi.mock("@/pages/public/RecoverPage", () => ({
  default: function MockRecoverPage(): ReactElement {
    return <main data-testid="recover-page">Recover</main>;
  },
}));

vi.mock("@/pages/public/EnrollPage", () => ({
  default: function MockEnrollPage(): ReactElement {
    return <main data-testid="recover-enroll-page">Recover enroll</main>;
  },
}));

vi.mock("@/pages/public/AcceptPage", () => ({
  default: function MockAcceptPage(): ReactElement {
    return <main data-testid="accept-page">Accept</main>;
  },
}));

vi.mock("@/pages/public/GuestPage", () => ({
  default: function MockGuestPage(): ReactElement {
    return <main data-testid="guest-page">Guest</main>;
  },
}));

vi.mock("@/pages/public/SignupPage", () => ({
  default: function MockSignupPage(): ReactElement {
    return <main data-testid="signup-page">Signup</main>;
  },
}));

vi.mock("@/pages/public/SignupVerifyPage", () => ({
  default: function MockSignupVerifyPage(): ReactElement {
    return <main data-testid="signup-verify-page">Signup verify</main>;
  },
}));

vi.mock("@/pages/public/SignupEnrollPage", () => ({
  default: function MockSignupEnrollPage(): ReactElement {
    return <main data-testid="signup-enroll-page">Signup enroll</main>;
  },
}));

type AppRole = "employee" | "manager" | "client";
type WorkspaceGrantRole = "manager" | "worker" | "client";
const clientPortalRoutes = [
  "/portfolio",
  "/billable_hours",
  "/quotes",
  "/invoices",
] as const;

function authMeFor(
  role: AppRole,
  grantRole: WorkspaceGrantRole = role === "employee" ? "worker" : role,
): AuthMe {
  return {
    user_id:
      role === "manager" ? "usr_manager"
      : role === "client" ? "usr_client"
      : "usr_worker",
    display_name:
      role === "manager" ? "Manager User"
      : role === "client" ? "Client User"
      : "Worker User",
    email:
      role === "manager" ? "manager@example.com"
      : role === "client" ? "client@example.com"
      : "worker@example.com",
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
        grant_role: grantRole,
        binding_org_id: grantRole === "client" ? "org_client" : null,
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
  return <span data-testid="location">{location.pathname + location.search}</span>;
}

function renderAppAt(path: string, role: AppRole, grantRole?: WorkspaceGrantRole): void {
  vi.spyOn(preferences, "readRoleCookie").mockReturnValue(role);
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("ws_1");
  setAuthenticated(authMeFor(role, grantRole));

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

function renderUnauthenticatedAppAt(path: string): void {
  setUnauthenticated();

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
  mockRenders.agentFetches = false;
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("App public root and protected deep links", () => {
  it("keeps authenticated / on the role home", async () => {
    installPermissionAllowFetch();
    renderAppAt("/", "manager");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
    });
    expect(await screen.findByTestId("manager-dashboard")).toBeInTheDocument();
  });

  it("sends logged-out bare protected links to /login with next", async () => {
    renderUnauthenticatedAppAt("/dashboard");

    await waitFor(() => {
      expect(screen.getByTestId("location"))
        .toHaveTextContent("/login?next=%2Fdashboard");
    });
  });

  it("sends logged-out workspace-prefixed protected links to /login with next", async () => {
    renderUnauthenticatedAppAt("/w/dev/dashboard?tab=ops");

    await waitFor(() => {
      expect(screen.getByTestId("location"))
        .toHaveTextContent("/login?next=%2Fw%2Fdev%2Fdashboard%3Ftab%3Dops");
    });
  });

  it("normalises authenticated workspace-prefixed protected links after setting the workspace", async () => {
    installPermissionAllowFetch();
    renderAppAt("/w/ws_1/dashboard", "manager");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
    });
    expect(await screen.findByTestId("manager-dashboard")).toBeInTheDocument();
  });

  it("normalises authenticated workspace-prefixed aliases before specific protected routes render", async () => {
    installPermissionAllowFetch();
    renderAppAt("/w/ws_1/scheduler?view=day", "manager");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/scheduler?view=day");
    });
    expect(await screen.findByTestId("scheduler-page")).toBeInTheDocument();
  });

  it.each([
    ["/login", "login-page"],
    ["/signup", "signup-page"],
    ["/signup/verify", "signup-verify-page"],
    ["/signup/enroll", "signup-enroll-page"],
    ["/recover", "recover-page"],
    ["/recover/enroll", "recover-enroll-page"],
    ["/auth/magic/tok_1", "signup-verify-page"],
    ["/accept/tok_1", "accept-page"],
    ["/guest/tok_1", "guest-page"],
    ["/w/dev/guest/tok_1", "guest-page"],
  ])("keeps %s on the public route branch", async (path, testId) => {
    renderUnauthenticatedAppAt(path);

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent(path);
    });
    expect(await screen.findByTestId(testId)).toBeInTheDocument();
  });
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

  it("uses the active manager grant instead of a stale employee role cookie", async () => {
    installPermissionAllowFetch();
    renderAppAt("/", "employee", "manager");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
    });

    expect(await screen.findByTestId("manager-dashboard")).toBeInTheDocument();
    expect(mockRenders.managerLayout).toHaveBeenCalled();
    expect(mockRenders.employeeLayout).not.toHaveBeenCalled();
  });

  it("uses the manager sidebar log scope with a stale employee role cookie", async () => {
    const requestedPaths: string[] = [];
    mockRenders.agentFetches = true;
    installFetch(({ url }) => {
      const parsed = new URL(url, "http://crewday.test");
      requestedPaths.push(parsed.pathname);
      if (parsed.pathname === "/w/ws_1/api/v1/agent/manager/log") {
        return jsonResponse([]);
      }
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

    renderAppAt("/today", "employee", "manager");

    await waitFor(() => {
      expect(requestedPaths).toContain("/w/ws_1/api/v1/agent/manager/log");
    });

    expect(requestedPaths).not.toContain("/w/ws_1/api/v1/agent/employee/log");
    expect(mockRenders.agentSidebar).toHaveBeenCalledWith("manager");
    expect(mockRenders.employeeLayout).not.toHaveBeenCalled();
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

describe("App client portal role routing", () => {
  it.each(clientPortalRoutes)(
    "redirects manager direct navigation away from the client portal shell at %s",
    async (path) => {
      installPermissionAllowFetch();
      renderAppAt(path, "manager");

      await waitFor(() => {
        expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
      });

      expect(await screen.findByTestId("manager-dashboard")).toBeInTheDocument();
      expect(mockRenders.managerLayout).toHaveBeenCalled();
      expect(mockRenders.clientLayout).not.toHaveBeenCalled();
      expect(mockRenders.clientPortfolioPage).not.toHaveBeenCalled();
    },
  );

  it("redirects a stale client role cookie when the active workspace grant is manager", async () => {
    installPermissionAllowFetch();
    renderAppAt("/portfolio", "client", "manager");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
    });

    expect(await screen.findByTestId("manager-dashboard")).toBeInTheDocument();
    expect(mockRenders.managerLayout).toHaveBeenCalled();
    expect(mockRenders.clientLayout).not.toHaveBeenCalled();
    expect(mockRenders.clientPortfolioPage).not.toHaveBeenCalled();
  });

  it("renders the client portal shell for client sessions", async () => {
    renderAppAt("/portfolio", "client");

    expect(await screen.findByTestId("client-portfolio")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/portfolio");
    expect(mockRenders.clientLayout).toHaveBeenCalled();
    expect(mockRenders.clientPortfolioPage).toHaveBeenCalled();
    expect(mockRenders.managerLayout).not.toHaveBeenCalled();
  });

  it("uses the active client grant instead of a stale manager role cookie", async () => {
    renderAppAt("/portfolio", "manager", "client");

    expect(await screen.findByTestId("client-portfolio")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/portfolio");
    expect(mockRenders.clientLayout).toHaveBeenCalled();
    expect(mockRenders.clientPortfolioPage).toHaveBeenCalled();
    expect(mockRenders.managerLayout).not.toHaveBeenCalled();
  });
});

describe("App manager API token routes", () => {
  it("renders the API tokens manager page at the /api-tokens alias", async () => {
    installPermissionAllowFetch();
    renderAppAt("/api-tokens", "manager");

    expect(await screen.findByTestId("api-tokens-page")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/api-tokens");
    expect(mockRenders.managerLayout).toHaveBeenCalled();
    expect(mockRenders.apiTokensPage).toHaveBeenCalled();
  });
});
