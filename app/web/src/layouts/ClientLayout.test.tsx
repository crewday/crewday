import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { jsonResponse } from "@/test/helpers";
import ClientLayout from "./ClientLayout";

function installFetch() {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  const fetchSpy = vi.fn(async (url: string | URL | Request) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push(resolved);
    if (resolved === "/w/acme/api/v1/me") {
      return jsonResponse({
        role: "client",
        theme: "system",
        agent_sidebar_collapsed: false,
        employee: {},
        manager_name: "Clara Client",
        today: "2026-04-30",
        now: "2026-04-30T12:00:00Z",
        user_id: "usr_client",
        agent_approval_mode: "strict",
        current_workspace_id: "ws_acme",
        available_workspaces: [],
        client_binding_org_ids: ["org_client"],
        is_deployment_admin: false,
        is_deployment_owner: false,
      });
    }
    if (resolved === "/w/acme/api/v1/users/usr_client") {
      return jsonResponse({ user: { id: "usr_client", display_name: "Clara Client" } });
    }
    if (resolved === "/api/v1/me/workspaces") {
      return jsonResponse([
        {
          workspace_id: "ws_acme",
          slug: "acme",
          name: "Acme",
          current_role: "client",
          last_seen_at: null,
          settings_override: {},
        },
      ]);
    }
    throw new Error("Unexpected fetch " + resolved);
  });
  (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = originalFetch;
    },
  };
}

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/w/acme/portfolio"]}>
        <WorkspaceProvider>
          <ClientLayout />
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

describe("<ClientLayout>", () => {
  it("emits workspace-prefixed client navigation links", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(screen.getByRole("link", { name: "Properties" })).toHaveAttribute("href", "/w/acme/portfolio");
      expect(screen.getByRole("link", { name: "Scheduler" })).toHaveAttribute("href", "/w/acme/scheduler");
      expect(screen.getByRole("link", { name: "Billable hours" })).toHaveAttribute("href", "/w/acme/billable-hours");
      expect(screen.getByRole("link", { name: "Quotes" })).toHaveAttribute("href", "/w/acme/quotes");
      expect(screen.getByRole("link", { name: "Invoices" })).toHaveAttribute("href", "/w/acme/invoices");
      expect(screen.getByRole("link", { name: "Me" })).toHaveAttribute("href", "/w/acme/me");
      expect(await screen.findByText("Clara Client")).toBeInTheDocument();
      expect(fake.calls).toContain("/w/acme/api/v1/me");
      expect(fake.calls).toContain("/w/acme/api/v1/users/usr_client");
    } finally {
      fake.restore();
    }
  });
});
