import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import PropertiesPage from "./PropertiesPage";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? "OK" : "Error",
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function installFetch({ failProperties = false }: { failProperties?: boolean } = {}) {
  const calls: string[] = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push(resolved);
    if (resolved === "/api/v1/auth/me") {
      return jsonResponse({
        user_id: "usr_1",
        display_name: "Mina",
        email: "mina@example.com",
        available_workspaces: [],
        current_workspace_id: "ws_owner",
      });
    }
    if (resolved === "/api/v1/me/workspaces") {
      return jsonResponse([
        {
          workspace_id: "ws_owner",
          slug: "acme",
          name: "Acme",
          current_role: "manager",
          last_seen_at: null,
          settings_override: {},
        },
      ]);
    }
    if (resolved === "/w/acme/api/v1/properties") {
      if (failProperties) {
        return jsonResponse({ type: "server_error", title: "Server error" }, 500);
      }
      return jsonResponse([
        {
          id: "prop_1",
          name: "Villa Rosa",
          city: "Porto",
          timezone: "Europe/Lisbon",
          color: "moss",
          kind: "str",
          areas: ["Kitchen", "Terrace"],
          evidence_policy: "inherit",
          country: "PT",
          locale: "pt-PT",
          settings_override: {},
          client_org_id: "org_1",
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
            check_in: "2026-04-29T12:00:00Z",
            check_out: "2026-04-30T10:00:00Z",
            guest_name: "Guest",
            guest_count: 2,
            status: "scheduled",
            source: "api",
          },
        ],
        next_cursor: null,
        has_more: false,
      });
    }
    if (resolved === "/w/acme/api/v1/billing/organizations") {
      return jsonResponse({
        data: [
          {
            id: "org_1",
            workspace_id: "ws_owner",
            kind: "client",
            display_name: "Luxe Guests",
            tax_id: null,
            default_currency: "EUR",
            notes_md: null,
          },
        ],
      });
    }
    if (resolved === "/w/acme/api/v1/properties/prop_1/share") {
      return jsonResponse({
        data: [
          {
            property_id: "prop_1",
            workspace_id: "ws_owner",
            label: "Acme",
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
            starts_at: "2026-05-01T00:00:00Z",
            ends_at: "2026-05-02T00:00:00Z",
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
          <PropertiesPage />
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

describe("<PropertiesPage>", () => {
  it("renders the mock cards from production property endpoints", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByRole("heading", { name: "Villa Rosa" })).toBeInTheDocument();
      expect(screen.getByText("Porto · Europe/Lisbon")).toBeInTheDocument();
      expect(screen.getByText("1 stays")).toBeInTheDocument();
      expect(screen.getByText("2 areas")).toBeInTheDocument();
      expect(screen.getByText("1 closure")).toBeInTheDocument();
      expect(screen.getByText("Owner")).toBeInTheDocument();
      expect(screen.getByText("Managed: Partner Ops")).toBeInTheDocument();
      expect(screen.getByText("Client: Luxe Guests")).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Overview/ })).toHaveAttribute("href", "/property/prop_1");
      expect(fake.calls).toContain("/api/v1/auth/me");
      expect(fake.calls).toContain("/api/v1/me/workspaces");
      expect(fake.calls).toContain("/w/acme/api/v1/properties/prop_1/share");
    } finally {
      fake.restore();
    }
  });

  it("renders the mock failure copy when the properties query fails", async () => {
    const fake = installFetch({ failProperties: true });
    try {
      render(<Harness />);

      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "Villa Rosa" })).toBeNull();
    } finally {
      fake.restore();
    }
  });
});
