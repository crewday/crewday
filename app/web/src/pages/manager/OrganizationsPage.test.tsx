import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Outlet } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import OrganizationsPage from "./OrganizationsPage";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? "OK" : "Error",
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const ORGS = [
  {
    id: "org_client",
    workspace_id: "ws_owner",
    kind: "client",
    display_name: "Luxury Villas",
    billing_address: {},
    tax_id: "PT-123",
    default_currency: "EUR",
    contact_email: "billing@luxury.example",
    contact_phone: "+3515550101",
    notes_md: "Preferred monthly rollup.",
    created_at: "2026-04-29T00:00:00Z",
    archived_at: null,
  },
  {
    id: "org_vendor",
    workspace_id: "ws_owner",
    kind: "vendor",
    display_name: "CleanCo",
    billing_address: {},
    tax_id: null,
    default_currency: "USD",
    contact_email: null,
    contact_phone: null,
    notes_md: null,
    created_at: "2026-04-29T00:00:00Z",
    archived_at: null,
  },
];

function installFetch({
  organizationsStatus = 200,
  detailStatus = 200,
}: {
  organizationsStatus?: number;
  detailStatus?: number;
} = {}) {
  const calls: string[] = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push(resolved);
    if (resolved === "/w/acme/api/v1/me") {
      return jsonResponse({
        role: "manager",
        theme: "light",
        agent_sidebar_collapsed: false,
        employee: null,
        manager_name: "Mina",
        today: "2026-04-30",
        now: "2026-04-30T00:00:00Z",
        user_id: "usr_1",
        agent_approval_mode: "manual",
        current_workspace_id: "ws_owner",
        available_workspaces: [],
        client_binding_org_ids: [],
        is_deployment_admin: false,
        is_deployment_owner: false,
      });
    }
    if (resolved === "/w/acme/api/v1/billing/organizations") {
      return jsonResponse({ data: ORGS }, organizationsStatus);
    }
    if (resolved === "/w/acme/api/v1/billing/organizations/org_client") {
      return jsonResponse(ORGS[0], detailStatus);
    }
    if (resolved === "/w/acme/api/v1/billing/organizations/org_vendor") {
      return jsonResponse(ORGS[1], detailStatus);
    }
    if (resolved === "/w/acme/api/v1/work_roles") {
      return jsonResponse({
        data: [{ id: "role_cleaner", name: "Cleaner" }],
        next_cursor: null,
        has_more: false,
      });
    }
    throw new Error(`Unexpected fetch call: ${resolved}`);
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function Harness({ initial = "/organizations" }: { initial?: string }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <WorkspaceProvider>
        <MemoryRouter initialEntries={[initial]}>
          <OrganizationsPage />
        </MemoryRouter>
      </WorkspaceProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<OrganizationsPage>", () => {
  it("gates the real organizations route with scope.view at runtime", async () => {
    const originalFetch = globalThis.fetch;
    vi.resetModules();
    vi.doMock("@/lib/preferences", async (importOriginal) => {
      const actual = await importOriginal<typeof import("@/lib/preferences")>();
      return {
        ...actual,
        readWorkspaceCookie: () => "acme",
      };
    });
    vi.doMock("@/context/RoleContext", () => ({
      useRole: () => ({ role: "manager", setRole: vi.fn() }),
    }));
    vi.doMock("@/layouts/PreviewShell", () => ({
      default: () => <Outlet />,
    }));
    vi.doMock("@/layouts/ManagerLayout", () => ({
      default: () => (
        <div>
          <div>manager shell</div>
          <Outlet />
        </div>
      ),
    }));
    vi.doMock("@/pages/manager/OrganizationsPage", () => ({
      default: () => <div>organizations route body</div>,
    }));

    const calls: string[] = [];
    (globalThis as { fetch: typeof fetch }).fetch = vi.fn(async (url: string | URL | Request) => {
      const resolved = typeof url === "string" ? url : url.toString();
      calls.push(resolved);
      if (resolved.startsWith("/w/acme/api/v1/permissions/resolved/self?")) {
        return jsonResponse({
          effect: "deny",
          source_layer: "no_match",
          source_rule_id: null,
          matched_groups: [],
        });
      }
      throw new Error(`Unexpected fetch call: ${resolved}`);
    }) as unknown as typeof fetch;

    try {
      const { default: App } = await import("@/App");
      const { WorkspaceProvider: RuntimeWorkspaceProvider } = await import("@/context/WorkspaceContext");
      const { __resetAuthStoreForTests } = await import("@/auth/useAuth");
      const { setAuthenticated } = await import("@/auth/authStore");
      const { __resetApiProvidersForTests: resetApi } = await import("@/lib/api");
      const { __resetQueryKeyGetterForTests: resetQueryKeys } = await import("@/lib/queryKeys");

      resetApi();
      resetQueryKeys();
      __resetAuthStoreForTests();
      setAuthenticated({
        user_id: "usr_1",
        display_name: "Mina",
        email: "mina@example.com",
        current_workspace_id: "ws_owner",
        available_workspaces: [
          {
            workspace: {
              id: "ws_owner",
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
        is_deployment_admin: false,
      });

      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={["/organizations"]}>
            <RuntimeWorkspaceProvider>
              <App />
            </RuntimeWorkspaceProvider>
          </MemoryRouter>
        </QueryClientProvider>,
      );

      expect(await screen.findByRole("alert")).toHaveTextContent("Access denied");
      expect(screen.queryByText("manager shell")).toBeNull();
      expect(screen.queryByText("organizations route body")).toBeNull();
      expect(calls[0]).toContain("action_key=scope.view");
      expect(calls[0]).not.toContain("organizations.edit");
    } finally {
      (globalThis as { fetch: typeof fetch }).fetch = originalFetch;
      vi.doUnmock("@/lib/preferences");
      vi.doUnmock("@/context/RoleContext");
      vi.doUnmock("@/layouts/PreviewShell");
      vi.doUnmock("@/layouts/ManagerLayout");
      vi.doUnmock("@/pages/manager/OrganizationsPage");
      vi.resetModules();
    }
  });

  it("renders the mock organizations structure from billing endpoints", async () => {
    const fake = installFetch();
    try {
      const { container } = render(<Harness />);

      expect(await screen.findByRole("heading", { name: "Counterparties" })).toBeInTheDocument();
      expect(container.querySelector(".org-list")).not.toBeNull();
      expect(container.querySelector(".org-list__row--active")).not.toBeNull();
      expect(screen.getAllByText("Luxury Villas").length).toBeGreaterThan(0);
      expect(screen.getByText("CleanCo")).toBeInTheDocument();
      expect(screen.getAllByText("Client").length).toBeGreaterThan(0);
      expect(screen.getByText("Supplier")).toBeInTheDocument();
      expect(await screen.findByText("PT-123")).toBeInTheDocument();
      expect(screen.getByText("Preferred monthly rollup.")).toBeInTheDocument();
      expect(screen.getByText("No rates on file. Shifts will surface in the \"unpriced\" CSV bucket.")).toBeInTheDocument();
      expect(fake.calls).toContain("/w/acme/api/v1/billing/organizations");
      expect(fake.calls).toContain("/w/acme/api/v1/billing/organizations/org_client");
      expect(fake.calls).not.toContain("/w/acme/api/v1/users");
    } finally {
      fake.restore();
    }
  });

  it("fetches the selected organization detail when a counterparty is clicked", async () => {
    const fake = installFetch();
    try {
      render(<Harness initial="/w/acme/organizations" />);

      await screen.findByRole("heading", { name: "Counterparties" });
      fireEvent.click(screen.getByText("CleanCo"));

      expect(await screen.findByRole("heading", { name: "CleanCo" })).toBeInTheDocument();
      expect(fake.calls).toContain("/w/acme/api/v1/billing/organizations/org_vendor");
    } finally {
      fake.restore();
    }
  });

  it("renders the failure state when the organization list fails", async () => {
    const fake = installFetch({ organizationsStatus: 500 });
    try {
      render(<Harness />);
      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
      expect(screen.queryByText("No organizations in this workspace.")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("renders the detail failure state instead of spinning forever", async () => {
    const fake = installFetch({ detailStatus: 500 });
    try {
      render(<Harness />);
      expect(await screen.findByRole("heading", { name: "Counterparties" })).toBeInTheDocument();
      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });
});
