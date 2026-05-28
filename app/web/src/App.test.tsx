import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactElement } from "react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
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
  adminDashboardPage: vi.fn(),
  adminLayout: vi.fn(),
  adminLlmPage: vi.fn(),
  adminLlmUsagePage: vi.fn(),
  clientBillableHoursPage: vi.fn(),
  clientInvoicesPage: vi.fn(),
  clientLayout: vi.fn(),
  clientPortfolioPage: vi.fn(),
  clientQuotesPage: vi.fn(),
  employeeLayout: vi.fn(),
  agentFetches: false,
  agentSidebar: vi.fn(),
  agentSidebarMount: vi.fn(),
  managerLayout: vi.fn(),
  managerLayoutMount: vi.fn(),
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
      mockRenders.agentSidebarMount(role);
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
      mockRenders.agentSidebarMount(role);
    }, [role]);
    useEffect(() => {
      if (!mockRenders.agentFetches) return;
      void fetch(`/w/ws_1/api/v1/agent/${role}/log`);
    }, [role]);
    return <aside data-testid="agent-sidebar">agent:{role}</aside>;
  }
  return {
    default: function MockManagerLayout(): ReactElement {
      mockRenders.managerLayout();
      useEffect(() => {
        mockRenders.managerLayoutMount();
      }, []);
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

vi.mock("@/layouts/AdminLayout", async () => {
  const { Outlet: RouterOutlet } = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return {
    default: function MockAdminLayout(): ReactElement {
      mockRenders.adminLayout();
      return (
        <div data-testid="admin-layout">
          <RouterOutlet />
        </div>
      );
    },
  };
});

vi.mock("@/pages/admin/DashboardPage", () => ({
  default: function MockAdminDashboardPage(): ReactElement {
    mockRenders.adminDashboardPage();
    return <main data-testid="admin-dashboard">Admin dashboard</main>;
  },
}));

vi.mock("@/pages/admin/LlmPage", () => ({
  default: function MockAdminLlmPage(): ReactElement {
    mockRenders.adminLlmPage();
    return <main data-testid="admin-llm-graph">Admin LLM graph</main>;
  },
}));

vi.mock("@/pages/admin/LlmUsagePage", () => ({
  default: function MockAdminLlmUsagePage(): ReactElement {
    mockRenders.adminLlmUsagePage();
    return <main data-testid="admin-llm-usage">Admin LLM usage</main>;
  },
}));

vi.mock("@/pages/employee/ChatPage", () => ({
  default: function MockChatPage(): ReactElement {
    mockRenders.chatPage();
    return <main data-testid="full-chat">Operations agent</main>;
  },
}));

vi.mock("@/pages/employee/TodayPage", () => ({
  default: function MockTodayPage(): ReactElement {
    return <main data-testid="today-page">Today</main>;
  },
}));

vi.mock("@/pages/employee/SchedulePage", () => ({
  default: function MockSchedulePage(): ReactElement {
    return <main data-testid="schedule-page">Schedule</main>;
  },
}));

vi.mock("@/pages/employee/TaskDetailPage", () => ({
  default: function MockTaskDetailPage(): ReactElement {
    return <main data-testid="task-detail-page">Task</main>;
  },
}));

vi.mock("@/pages/employee/MyExpensesPage", () => ({
  default: function MockMyExpensesPage(): ReactElement {
    return <main data-testid="my-expenses-page">My expenses</main>;
  },
}));

vi.mock("@/pages/employee/MePage", () => ({
  default: function MockMePage(): ReactElement {
    return <main data-testid="me-page">Me</main>;
  },
}));

vi.mock("@/pages/employee/HistoryPage", () => ({
  default: function MockHistoryPage(): ReactElement {
    return <main data-testid="history-page">History</main>;
  },
}));

vi.mock("@/pages/employee/IssueNewPage", () => ({
  default: function MockIssueNewPage(): ReactElement {
    return <main data-testid="issue-new-page">New issue</main>;
  },
}));

vi.mock("@/pages/employee/EmployeeAssetPage", () => ({
  default: function MockEmployeeAssetPage(): ReactElement {
    return <main data-testid="employee-asset-page">Asset</main>;
  },
}));

vi.mock("@/pages/employee/AssetScanPage", () => ({
  default: function MockAssetScanPage(): ReactElement {
    return <main data-testid="asset-scan-page">Scan asset</main>;
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

vi.mock("@/pages/client/BillableHoursPage", () => ({
  default: function MockClientBillableHoursPage(): ReactElement {
    mockRenders.clientBillableHoursPage();
    return <main data-testid="client-billable-hours">Client billable hours</main>;
  },
}));

vi.mock("@/pages/client/QuotesPage", () => ({
  default: function MockClientQuotesPage(): ReactElement {
    mockRenders.clientQuotesPage();
    return <main data-testid="client-quotes">Client quotes</main>;
  },
}));

vi.mock("@/pages/client/InvoicesPage", () => ({
  default: function MockClientInvoicesPage(): ReactElement {
    mockRenders.clientInvoicesPage();
    return <main data-testid="client-invoices">Client invoices</main>;
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
type WorkspaceGrantRole = "manager" | "worker" | "client" | "admin";
const clientPortalRoutes = [
  "/portfolio",
  "/billable-hours",
  "/quotes",
  "/invoices",
] as const;

function authMeFor(
  role: AppRole,
  grantRole: WorkspaceGrantRole = role === "employee" ? "worker" : role,
): AuthMe {
  // code-health: ignore[nloc params] App route tests keep the full auth payload local so role redirects remain explicit.
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

function multiWorkspaceAuthMe(): AuthMe {
  return {
    ...authMeFor("manager", "manager"),
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
      {
        workspace: {
          id: "ws_2",
          name: "Second workspace",
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

function installPermissionAllowFetch(): void {
  installFetch(({ url }) => {
    const parsed = new URL(url, "http://crewday.test");
    if (/^\/w\/[^/]+\/api\/v1\/permissions\/resolved\/self$/.test(parsed.pathname)) {
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
  return <span data-testid="location">{location.pathname + location.search + location.hash}</span>;
}

function NavigationProbe(): ReactElement {
  const navigate = useNavigate();
  return (
    <div>
      <button type="button" onClick={() => navigate("/w/ws_1/today")}>Go today</button>
      <button type="button" onClick={() => navigate("/w/ws_1/dashboard")}>Go dashboard</button>
    </div>
  );
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
            <NavigationProbe />
            <App />
          </WorkspaceProvider>
        </RoleProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderAppWithUser(
  path: string,
  role: AppRole,
  user: AuthMe,
  options: { workspaceCookie?: string | null } = {},
): void {
  vi.spyOn(preferences, "readRoleCookie").mockReturnValue(role);
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue(
    Object.hasOwn(options, "workspaceCookie") ? (options.workspaceCookie ?? null) : "ws_1",
  );
  setAuthenticated(user);

  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <RoleProvider>
          <WorkspaceProvider>
            <LocationProbe />
            <NavigationProbe />
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
  it.each([
    ["employee", "worker", "/w/ws_1/today", "employee-layout"],
    ["manager", "manager", "/w/ws_1/dashboard", "manager-dashboard"],
    ["manager", "admin", "/w/ws_1/dashboard", "manager-dashboard"],
    ["client", "client", "/w/ws_1/portfolio", "client-portfolio"],
  ] as const)("routes authenticated / for %s/%s to %s", async (role, grantRole, expectedPath, testId) => {
    installPermissionAllowFetch();
    renderAppAt("/", role, grantRole);

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent(expectedPath);
    });
    expect(await screen.findByTestId(testId)).toBeInTheDocument();
  });

  it.each([
    ["employee", "worker", "/w/ws_1/today", "employee-layout"],
    ["manager", "manager", "/w/ws_1/dashboard", "manager-dashboard"],
    ["client", "client", "/w/ws_1/portfolio", "client-portfolio"],
  ] as const)("keeps canonical workspace-prefixed %s role home at %s", async (role, grantRole, path, testId) => {
    installPermissionAllowFetch();
    renderAppAt(path, role, grantRole);

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent(path);
    });
    expect(await screen.findByTestId(testId)).toBeInTheDocument();
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

  it("keeps authenticated workspace-prefixed protected links after setting the workspace", async () => {
    installPermissionAllowFetch();
    renderAppAt("/w/ws_1/dashboard?tab=ops#x", "manager");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/ws_1/dashboard?tab=ops#x");
    });
    expect(await screen.findByTestId("manager-dashboard")).toBeInTheDocument();
  });

  it("keeps authenticated workspace-prefixed aliases before specific protected routes render", async () => {
    installPermissionAllowFetch();
    renderAppAt("/w/ws_1/scheduler?view=day", "manager");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/ws_1/scheduler?view=day");
    });
    expect(await screen.findByTestId("scheduler-page")).toBeInTheDocument();
  });

  it("redirects authenticated bare workspace paths to the active workspace prefix", async () => {
    installPermissionAllowFetch();
    renderAppAt("/dashboard?tab=ops#x", "manager");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/ws_1/dashboard?tab=ops#x");
    });
    expect(await screen.findByTestId("manager-dashboard")).toBeInTheDocument();
  });

  it("keeps the manager shell mounted across manager workspace navigation", async () => {
    installPermissionAllowFetch();
    renderAppAt("/w/ws_1/dashboard", "manager");

    expect(await screen.findByTestId("manager-dashboard")).toBeInTheDocument();
    expect(mockRenders.managerLayoutMount).toHaveBeenCalledTimes(1);
    expect(mockRenders.agentSidebarMount).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Go today" }));

    expect(await screen.findByTestId("today-page")).toBeInTheDocument();
    expect(mockRenders.managerLayoutMount).toHaveBeenCalledTimes(1);
    expect(mockRenders.agentSidebarMount).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Go dashboard" }));

    expect(await screen.findByTestId("manager-dashboard")).toBeInTheDocument();
    expect(mockRenders.managerLayoutMount).toHaveBeenCalledTimes(1);
    expect(mockRenders.agentSidebarMount).toHaveBeenCalledTimes(1);
  });

  it("redirects authenticated bare client role home to the active workspace prefix", async () => {
    renderAppAt("/portfolio?view=all#top", "client");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/ws_1/portfolio?view=all#top");
    });
    expect(await screen.findByTestId("client-portfolio")).toBeInTheDocument();
  });

  it.each([
    ["/today?view=now#top", "/w/ws_1/today?view=now#top", "today-page"],
    ["/schedule", "/w/ws_1/schedule", "schedule-page"],
    ["/task/t1", "/w/ws_1/task/t1", "task-detail-page"],
    ["/my/expenses", "/w/ws_1/my/expenses", "my-expenses-page"],
    ["/me", "/w/ws_1/me", "me-page"],
    ["/history#chats", "/w/ws_1/history#chats", "history-page"],
    ["/issues/new", "/w/ws_1/issues/new", "issue-new-page"],
    ["/asset/a1", "/w/ws_1/asset/a1", "employee-asset-page"],
    ["/asset/scan", "/w/ws_1/asset/scan", "asset-scan-page"],
  ])("redirects authenticated bare worker/shared %s to %s", async (path, expectedPath, testId) => {
    renderAppAt(path, "employee");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent(expectedPath);
    });
    expect(await screen.findByTestId(testId)).toBeInTheDocument();
  });

  it("keeps deployment admin routes on the bare host", async () => {
    renderAppAt("/admin/dashboard", "manager");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/admin/dashboard");
    });
    expect(await screen.findByTestId("admin-dashboard")).toBeInTheDocument();
    expect(mockRenders.adminLayout).toHaveBeenCalled();
  });

  it("redirects bare admin LLM to the dedicated graph route", async () => {
    renderAppAt("/admin/llm", "manager");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/admin/llm/graph");
    });
    expect(await screen.findByTestId("admin-llm-graph")).toBeInTheDocument();
    expect(mockRenders.adminLlmPage).toHaveBeenCalled();
  });

  it("renders the dedicated admin LLM graph route directly", async () => {
    renderAppAt("/admin/llm/graph", "manager");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/admin/llm/graph");
    });
    expect(await screen.findByTestId("admin-llm-graph")).toBeInTheDocument();
    expect(mockRenders.adminLlmPage).toHaveBeenCalled();
  });

  it("renders the dedicated admin LLM usage route directly", async () => {
    renderAppAt("/admin/llm/usage", "manager");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/admin/llm/usage");
    });
    expect(await screen.findByTestId("admin-llm-usage")).toBeInTheDocument();
    expect(mockRenders.adminLlmUsagePage).toHaveBeenCalled();
  });

  it("keeps the authenticated workspace picker on the bare host", async () => {
    renderAppWithUser("/select-workspace", "manager", multiWorkspaceAuthMe());

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/select-workspace");
    });
    expect(await screen.findByRole("heading", { name: "Pick a workspace" })).toBeInTheDocument();
  });

  it("silently routes bare / to the remembered workspace canonical landing", async () => {
    const user = {
      ...multiWorkspaceAuthMe(),
      current_workspace_id: "ws_2",
    };

    renderAppWithUser("/", "manager", user, { workspaceCookie: "stale-workspace" });

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/ws_2/today");
    });
    expect(await screen.findByTestId("today-page")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Pick a workspace" })).toBeNull();
  });

  it("shows the workspace picker on bare / when no remembered workspace is valid", async () => {
    const user = {
      ...multiWorkspaceAuthMe(),
      current_workspace_id: null,
    };

    renderAppWithUser("/", "manager", user, { workspaceCookie: "stale-workspace" });

    expect(await screen.findByRole("heading", { name: "Pick a workspace" })).toBeInTheDocument();
    expect(screen.getByText("Acme")).toBeInTheDocument();
    expect(screen.getByText("Second workspace")).toBeInTheDocument();
    expect(screen.getByText("ws_1")).toBeInTheDocument();
    expect(screen.getByText("ws_2")).toBeInTheDocument();
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
  it("keeps manager /chat on the full-screen agent route in the manager shell", async () => {
    installPermissionAllowFetch();
    renderAppAt("/chat", "manager");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/ws_1/chat");
    });

    expect(await screen.findByTestId("full-chat")).toBeInTheDocument();
    expect(mockRenders.managerLayout).toHaveBeenCalled();
    expect(mockRenders.employeeLayout).not.toHaveBeenCalled();
    expect(mockRenders.chatPage).toHaveBeenCalled();
    expect(mockRenders.agentSidebar).toHaveBeenCalledWith("manager");
  });

  it("uses the active manager grant instead of a stale employee role cookie", async () => {
    installPermissionAllowFetch();
    renderAppAt("/", "employee", "manager");

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/ws_1/dashboard");
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

    expect(await screen.findByTestId("full-chat")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/w/ws_1/chat");
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
        expect(screen.getByTestId("location")).toHaveTextContent("/w/ws_1/dashboard");
      });

      expect(await screen.findByTestId("manager-dashboard")).toBeInTheDocument();
      expect(mockRenders.managerLayout).toHaveBeenCalled();
      expect(mockRenders.clientLayout).not.toHaveBeenCalled();
      expect(mockRenders.clientPortfolioPage).not.toHaveBeenCalled();
    },
  );

  it.each(clientPortalRoutes)(
    "redirects manager prefixed navigation away from the client portal shell at %s",
    async (path) => {
      installPermissionAllowFetch();
      renderAppAt("/w/ws_1" + path, "manager");

      await waitFor(() => {
        expect(screen.getByTestId("location")).toHaveTextContent("/w/ws_1/dashboard");
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
      expect(screen.getByTestId("location")).toHaveTextContent("/w/ws_1/dashboard");
    });

    expect(await screen.findByTestId("manager-dashboard")).toBeInTheDocument();
    expect(mockRenders.managerLayout).toHaveBeenCalled();
    expect(mockRenders.clientLayout).not.toHaveBeenCalled();
    expect(mockRenders.clientPortfolioPage).not.toHaveBeenCalled();
  });

  it("renders the client portal shell for client sessions", async () => {
    renderAppAt("/portfolio", "client");

    expect(await screen.findByTestId("client-portfolio")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/w/ws_1/portfolio");
    expect(mockRenders.clientLayout).toHaveBeenCalled();
    expect(mockRenders.clientPortfolioPage).toHaveBeenCalled();
    expect(mockRenders.managerLayout).not.toHaveBeenCalled();
  });

  it.each([
    ["/w/ws_1/portfolio", "client-portfolio"],
    ["/w/ws_1/billable-hours", "client-billable-hours"],
    ["/w/ws_1/quotes", "client-quotes"],
    ["/w/ws_1/invoices", "client-invoices"],
  ])("renders client portal surface %s for client sessions", async (path, testId) => {
    renderAppAt(path, "client");

    expect(await screen.findByTestId(testId)).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(path);
    expect(mockRenders.clientLayout).toHaveBeenCalled();
    expect(mockRenders.managerLayout).not.toHaveBeenCalled();
  });

  it("redirects the legacy billable_hours client URL to billable-hours", async () => {
    renderAppAt("/w/ws_1/billable_hours", "client");

    expect(await screen.findByTestId("client-billable-hours")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/w/ws_1/billable-hours");
  });

  it("uses the active client grant instead of a stale manager role cookie", async () => {
    renderAppAt("/portfolio", "manager", "client");

    expect(await screen.findByTestId("client-portfolio")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/w/ws_1/portfolio");
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
    expect(screen.getByTestId("location")).toHaveTextContent("/w/ws_1/api-tokens");
    expect(mockRenders.managerLayout).toHaveBeenCalled();
    expect(mockRenders.apiTokensPage).toHaveBeenCalled();
  });
});
