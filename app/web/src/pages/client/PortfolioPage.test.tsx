import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import type { Me } from "@/types/api";
import PortfolioPage from "./PortfolioPage";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? "OK" : "Error",
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function mePayload(overrides: Partial<Me> = {}): Me {
  return {
    role: "client",
    theme: "system",
    agent_sidebar_collapsed: false,
    employee: {} as Me["employee"],
    manager_name: "Clara Client",
    today: "2026-04-30",
    now: "2026-04-30T12:00:00Z",
    user_id: "usr_client",
    agent_approval_mode: "strict",
    current_workspace_id: "ws_agency",
    available_workspaces: [],
    client_binding_org_ids: ["org_client"],
    is_deployment_admin: false,
    is_deployment_owner: false,
    ...overrides,
  };
}

function installFetch({ role = "client" }: { role?: Me["role"] } = {}) {
  const calls: string[] = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push(resolved);
    if (resolved === "/api/v1/auth/me") {
      return jsonResponse({
        user_id: "usr_client",
        display_name: "Clara Client",
        email: "clara@example.com",
        available_workspaces: [],
        current_workspace_id: "ws_agency",
      });
    }
    if (resolved === "/api/v1/me/workspaces") {
      return jsonResponse([
        {
          workspace_id: "ws_agency",
          slug: "acme",
          name: "Acme Agency",
          current_role: role,
          last_seen_at: null,
          settings_override: {},
        },
        {
          workspace_id: "ws_partner",
          slug: "partner",
          name: "Partner Ops",
          current_role: "manager",
          last_seen_at: null,
          settings_override: {},
        },
      ]);
    }
    if (resolved === "/w/acme/api/v1/me") {
      return jsonResponse(mePayload({ role, client_binding_org_ids: role === "client" ? ["org_client"] : [] }));
    }
    if (resolved === "/w/acme/api/v1/client/portfolio?limit=500") {
      return jsonResponse({
        data: [
          {
            id: "prop_1",
            organization_id: "org_client",
            organization_name: "Luxe Guests",
            name: "Villa Cliente",
            kind: "str",
            address: "Porto",
            country: "PT",
            timezone: "Europe/Lisbon",
            default_currency: "EUR",
          },
        ],
        next_cursor: null,
        has_more: false,
      });
    }
    if (resolved === "/w/acme/api/v1/properties") {
      return jsonResponse([
        {
          id: "prop_1",
          name: "Villa Cliente",
          city: "Porto",
          timezone: "Europe/Lisbon",
          color: "moss",
          kind: "str",
          areas: ["Kitchen", "Terrace"],
          evidence_policy: "inherit",
          country: "PT",
          locale: "pt-PT",
          settings_override: {},
          client_org_id: null,
          owner_user_id: null,
        },
        {
          id: "prop_other",
          name: "Leaked Villa",
          city: "Lisbon",
          timezone: "Europe/Lisbon",
          color: "sky",
          kind: "vacation",
          areas: ["Suite"],
          evidence_policy: "inherit",
          country: "PT",
          locale: "pt-PT",
          settings_override: {},
          client_org_id: null,
          owner_user_id: null,
        },
      ]);
    }
    if (resolved === "/w/acme/api/v1/stays/reservations?limit=500") {
      return jsonResponse({
        data: [
          {
            id: "res_1",
            property_id: "prop_1",
            check_in: "2026-04-29T15:00:00Z",
            check_out: "2026-05-02T10:00:00Z",
            guest_name: "Dana Guest",
            guest_count: 2,
            status: "checked_in",
            source: "api",
          },
          {
            id: "res_other",
            property_id: "prop_other",
            check_in: "2026-04-29T15:00:00Z",
            check_out: "2026-05-02T10:00:00Z",
            guest_name: "Hidden Guest",
            guest_count: 2,
            status: "checked_in",
            source: "api",
          },
        ],
        next_cursor: null,
        has_more: false,
      });
    }
    if (resolved === "/w/acme/api/v1/properties/prop_1/share") {
      return jsonResponse({
        data: [
          {
            property_id: "prop_1",
            workspace_id: "ws_agency",
            label: "Acme Agency",
            membership_role: "owner_workspace",
            status: "active",
            share_guest_identity: true,
            created_at: "2026-04-29T00:00:00Z",
          },
          {
            property_id: "prop_1",
            workspace_id: "ws_partner",
            label: "Partner Ops",
            membership_role: "managed_workspace",
            status: "active",
            share_guest_identity: false,
            created_at: "2026-04-29T00:00:00Z",
          },
        ],
        next_cursor: null,
        has_more: false,
      });
    }
    if (resolved === "/w/acme/api/v1/property_closures?property_id=prop_1&limit=100") {
      return jsonResponse({
        data: [
          {
            id: "closure_1",
            property_id: "prop_1",
            starts_at: "2026-05-10T00:00:00Z",
            ends_at: "2026-05-11T00:00:00Z",
            reason: "renovation",
          },
        ],
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

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WorkspaceProvider>
          <PortfolioPage />
        </WorkspaceProvider>
      </MemoryRouter>
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

describe("<PortfolioPage>", () => {
  it("renders only properties bound to the client's organizations", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByRole("heading", { name: "Villa Cliente" })).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "Leaked Villa" })).toBeNull();
      expect(screen.queryByText("Hidden Guest")).toBeNull();
      expect(screen.getByText("Porto · Europe/Lisbon")).toBeInTheDocument();
      expect(screen.getByText("1 stays")).toBeInTheDocument();
      expect(screen.getByText("2 areas")).toBeInTheDocument();
      expect(screen.getByText("1 closure")).toBeInTheDocument();
      expect(screen.getByText("Current stay: Dana Guest")).toBeInTheDocument();
      expect(screen.getByText("Billed to Luxe Guests")).toBeInTheDocument();
      expect(screen.getByText("Owner: Acme Agency")).toBeInTheDocument();
      expect(screen.getByText("Managed by Partner Ops")).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Villa Cliente/ })).toHaveAttribute("href", "/property/prop_1");
      expect(fake.calls).toContain("/w/acme/api/v1/properties/prop_1/share");
      expect(fake.calls).not.toContain("/w/acme/api/v1/properties/prop_other/share");
    } finally {
      fake.restore();
    }
  });

  it("shows a not-authorized card before loading client-only data for non-client roles", async () => {
    const fake = installFetch({ role: "manager" });
    try {
      render(<Harness />);

      expect(await screen.findByText("This page is only available to client portal users.")).toBeInTheDocument();
      expect(fake.calls).not.toContain("/w/acme/api/v1/client/portfolio?limit=500");
      expect(fake.calls).not.toContain("/w/acme/api/v1/properties");
      expect(fake.calls).not.toContain("/w/acme/api/v1/stays/reservations?limit=500");
    } finally {
      fake.restore();
    }
  });
});
