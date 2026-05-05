import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import {
  installFetch as installFetchSpy,
  jsonResponse,
  type FetchCall,
} from "@/test/helpers";
import LeavesInboxPage from "./LeavesInboxPage";

/**
 * Closure-style stub: every URL is hand-routed because two of the
 * endpoints share a prefix and one needs a `failLeaves` toggle.
 * Hoists the global-fetch swap and `Response` builder into the shared
 * helper module; the routing logic stays local because it's specific
 * to the page's contract surface.
 */
function installFetch({ failLeaves = false }: { failLeaves?: boolean } = {}): {
  calls: FetchCall[];
  callsBy(): { url: string; method: string }[];
  restore: () => void;
} {
  const env = installFetchSpy(({ url: resolved }) => {
    // code-health: ignore[nloc] Leaves inbox fixture keeps auth, list, approve, and reject responses together.
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
    if (resolved === "/w/acme/api/v1/leaves") {
      if (failLeaves) {
        return jsonResponse({ type: "server_error", title: "Server error" }, 500);
      }
      return jsonResponse({
        pending: [
          {
            id: "leave_pending",
            employee_id: "emp_1",
            starts_on: "2026-05-10",
            ends_on: "2026-05-12",
            category: "vacation",
            note: "Family trip",
            approved_at: null,
          },
        ],
        approved: [
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
    if (resolved === "/w/acme/api/v1/employees") {
      return jsonResponse([
        {
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
      ]);
    }
    if (resolved === "/w/acme/api/v1/leaves/leave_pending/approve") {
      return jsonResponse({
        id: "leave_pending",
        employee_id: "emp_1",
        starts_on: "2026-05-10",
        ends_on: "2026-05-12",
        category: "vacation",
        note: "Family trip",
        approved_at: "2026-04-30T00:00:00Z",
      });
    }
    if (resolved === "/w/acme/api/v1/leaves/leave_pending/reject") {
      return jsonResponse({
        id: "leave_pending",
        employee_id: "emp_1",
        starts_on: "2026-05-10",
        ends_on: "2026-05-12",
        category: "vacation",
        note: "Family trip",
        approved_at: null,
      });
    }
    throw new Error(`Unexpected fetch call: ${resolved}`);
  });
  return {
    calls: env.calls,
    callsBy: () =>
      env.calls.map((call) => ({
        url: call.url,
        method: call.init.method ?? "GET",
      })),
    restore: env.restore,
  };
}

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WorkspaceProvider>
          <LeavesInboxPage />
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

describe("<LeavesInboxPage>", () => {
  it("renders pending and approved leave rows from production endpoints", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Pending · 1")).toBeInTheDocument();
      expect(screen.getAllByText("Maya Santos")).toHaveLength(2);
      expect(screen.getByText("Family trip")).toBeInTheDocument();
      expect(screen.getByText("Checkup")).toBeInTheDocument();
      expect(screen.getByText("Approved (upcoming)")).toBeInTheDocument();
      expect(fake.callsBy()).toEqual(
        expect.arrayContaining([
          { url: "/w/acme/api/v1/leaves", method: "GET" },
          { url: "/w/acme/api/v1/employees", method: "GET" },
        ]),
      );
    } finally {
      fake.restore();
    }
  });

  it("posts approve decisions through the shared leaves alias", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "Approve" }));

      await waitFor(() => {
        expect(fake.callsBy()).toContainEqual({
          url: "/w/acme/api/v1/leaves/leave_pending/approve",
          method: "POST",
        });
      });
    } finally {
      fake.restore();
    }
  });

  it("posts reject decisions through the shared leaves alias", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "Reject" }));

      await waitFor(() => {
        expect(fake.callsBy()).toContainEqual({
          url: "/w/acme/api/v1/leaves/leave_pending/reject",
          method: "POST",
        });
      });
    } finally {
      fake.restore();
    }
  });

  it("renders the mock failure copy when the leaves query fails", async () => {
    const fake = installFetch({ failLeaves: true });
    try {
      render(<Harness />);

      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
      expect(screen.queryByText("Pending · 1")).toBeNull();
    } finally {
      fake.restore();
    }
  });
});
