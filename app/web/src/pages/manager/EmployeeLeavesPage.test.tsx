import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import EmployeeLeavesPage from "./EmployeeLeavesPage";
import { jsonResponse } from "@/test/helpers";

function installFetch({ failLeaves = false }: { failLeaves?: boolean } = {}) {
  const calls: { url: string; method: string }[] = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const method = init?.method ?? "GET";
    calls.push({ url: resolved, method });
    if (resolved === "/api/v1/auth/me") {
      return jsonResponse({
        user_id: "mgr_1",
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
    if (resolved === "/w/acme/api/v1/employees/emp_1/leaves") {
      if (failLeaves) {
        return jsonResponse({ type: "server_error", title: "Server error" }, 500);
      }
      return jsonResponse({
        subject: {
          id: "emp_1",
          name: "Maya Santos",
          roles: ["housekeeper"],
          properties: [],
          avatar_initials: "MS",
          avatar_file_id: null,
          avatar_url: null,
          phone: "",
          email: "maya@example.com",
          started_on: "2026-01-01",
          capabilities: {},
          workspaces: ["ws_owner"],
          villas: [],
          language: "en",
          weekly_availability: {},
          evidence_policy: "inherit",
          preferred_locale: null,
          settings_override: {},
        },
        leaves: [
          {
            id: "leave_pending",
            employee_id: "emp_1",
            starts_on: "2026-05-10",
            ends_on: "2026-05-12",
            category: "vacation",
            note: "Family trip",
            approved_at: null,
          },
          {
            id: "leave_approved",
            employee_id: "emp_1",
            starts_on: "2026-06-01",
            ends_on: "2026-06-01",
            category: "sick",
            note: "Checkup",
            approved_at: "2026-04-30T00:00:00Z",
          },
        ],
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

function Harness({ initialPath = "/employee/emp_1/leaves" }: { initialPath?: string }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialPath]}>
        <WorkspaceProvider>
          <Routes>
            <Route path="/employee/:eid/leaves" element={<EmployeeLeavesPage />} />
            <Route path="/w/:slug/user/:eid/leaves" element={<EmployeeLeavesPage />} />
          </Routes>
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

describe("<EmployeeLeavesPage>", () => {
  it("renders the promoted mock ledger from the production endpoint", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Maya Santos — leave ledger")).toBeInTheDocument();
      expect(screen.getByText("Family trip")).toBeInTheDocument();
      expect(screen.getByText("Checkup")).toBeInTheDocument();
      expect(screen.getByText("Pending")).toBeInTheDocument();
      expect(screen.getByText("Approved")).toBeInTheDocument();
      expect(fake.calls).toContainEqual({
        url: "/w/acme/api/v1/employees/emp_1/leaves",
        method: "GET",
      });
    } finally {
      fake.restore();
    }
  });

  it("renders the mock failure copy when the ledger query fails", async () => {
    const fake = installFetch({ failLeaves: true });
    try {
      render(<Harness />);

      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
      expect(screen.queryByText("Maya Santos — leave ledger")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("loads from the canonical workspace-prefixed user route", async () => {
    const fake = installFetch();
    try {
      render(<Harness initialPath="/w/acme/user/emp_1/leaves" />);

      expect(await screen.findByText("Maya Santos — leave ledger")).toBeInTheDocument();
      expect(fake.calls).toContainEqual({
        url: "/w/acme/api/v1/employees/emp_1/leaves",
        method: "GET",
      });
    } finally {
      fake.restore();
    }
  });
});
