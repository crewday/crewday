import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import type { Me } from "@/types/api";
import BillableHoursPage from "./BillableHoursPage";

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
    manager_name: "Manager",
    today: "2026-04-30",
    now: "2026-04-30T12:00:00Z",
    user_id: "user_client",
    agent_approval_mode: "strict",
    current_workspace_id: "ws_1",
    available_workspaces: [],
    client_binding_org_ids: ["org_1"],
    is_deployment_admin: false,
    is_deployment_owner: false,
    ...overrides,
  };
}

function installFetch({
  role = "client",
  billableStatus = 200,
}: {
  role?: Me["role"];
  billableStatus?: number;
} = {}) {
  const originalFetch = globalThis.fetch;
  const requests: string[] = [];
  const fetchSpy = vi.fn(async (url: string | URL | Request) => {
    const resolved = typeof url === "string" ? url : url.toString();
    requests.push(resolved);
    if (resolved === "/w/acme/api/v1/me") {
      return jsonResponse(mePayload({
        role,
        client_binding_org_ids: role === "client" ? ["org_1"] : [],
      }));
    }
    if (resolved === "/w/acme/api/v1/client/billable-hours?limit=500") {
      return jsonResponse({
        data: [
          {
            work_order_id: "wo_1",
            property_id: "prop_1",
            property_name: "Villa Rosa",
            week_start: "2026-04-27",
            hours_decimal: "3.50",
            total_cents: 35000,
            currency: "EUR",
            shift_id: "shift_should_not_render",
            pay_rule: { hourly_rate_cents: 4200 },
            accrued_cents: 35000,
          },
          {
            work_order_id: "wo_2",
            property_id: "prop_2",
            property_name: "Casa Azul",
            week_start: "2026-04-27",
            hours_decimal: "0.33",
            total_cents: 3333,
            currency: "EUR",
          },
        ],
        next_cursor: null,
        has_more: false,
      }, billableStatus);
    }
    throw new Error("Unexpected fetch " + resolved);
  });
  (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;
  return {
    requests,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = originalFetch;
    },
  };
}

function Harness() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WorkspaceProvider>
          <BillableHoursPage />
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

describe("<BillableHoursPage>", () => {
  it("renders client billable hours without worker pay-rate fields", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Villa Rosa")).toBeInTheDocument();
      expect(screen.getByText("Casa Azul")).toBeInTheDocument();
      expect(screen.getByText("210")).toBeInTheDocument();
      expect(screen.getByText("20")).toBeInTheDocument();
      expect(screen.getByText("€100.00")).toBeInTheDocument();
      expect(screen.getByText("€99.99")).toBeInTheDocument();
      expect(screen.getByText("230 min")).toBeInTheDocument();
      expect(screen.getByText("€383.33")).toBeInTheDocument();
      expect(screen.getByText("€350.00")).toBeInTheDocument();
      expect(screen.queryByText(/pay_rule|hourly_rate_cents|shift_id|accrued_cents/i)).toBeNull();
      expect(fake.requests).toEqual([
        "/w/acme/api/v1/me",
        "/w/acme/api/v1/client/billable-hours?limit=500",
      ]);
    } finally {
      fake.restore();
    }
  });

  it("shows a client-only gate without loading billable data for non-client roles", async () => {
    const fake = installFetch({ role: "manager" });
    try {
      render(<Harness />);

      expect(await screen.findByText("This page is only available to client portal users.")).toBeInTheDocument();
      expect(fake.requests).toEqual(["/w/acme/api/v1/me"]);
    } finally {
      fake.restore();
    }
  });

  it("shows a load failure when the client billable-hours endpoint fails", async () => {
    const fake = installFetch({ billableStatus: 500 });
    try {
      render(<Harness />);

      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
      expect(fake.requests).toEqual([
        "/w/acme/api/v1/me",
        "/w/acme/api/v1/client/billable-hours?limit=500",
      ]);
    } finally {
      fake.restore();
    }
  });
});
