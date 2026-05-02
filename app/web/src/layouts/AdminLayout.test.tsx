// crewday — AdminLayout role guard.
//
// What this covers:
//   1. A signed-in non-admin (`is_deployment_admin: false`) hitting
//      /admin/dashboard directly is redirected to RoleHome (`/`) so
//      the root `<RoleHome>` can route them by grant role. Closes
//      cd-28s7's parallel acceptance criterion (LoginPage filters the
//      `?next=` phishing case; AdminLayout is the belt-and-braces
//      shell guard for direct navigation / stale bookmarks).
//   2. /admin/api/v1/me returning a non-2xx (mirrors the production
//      404 the spec describes) drives the same redirect — the probe
//      failure is the canonical "not an admin" signal even when /me
//      hasn't resolved yet.
//
// What this does NOT cover (and why):
//   - The full `.desk` chrome / nav rendering — those are visual
//     concerns owned by Playwright; this test is purely about the
//     access guard.
//   - The deployment-admin happy path. Mounting the outlet pulls in
//     `<AgentSidebar role="admin">` → `<WorkspaceSwitcher>` →
//     `<WorkspaceProvider>` plus a long tail of other context
//     providers. The "outlet renders" branch is exercised end-to-end
//     by Playwright; the unit-level value here is the guard, not
//     the chrome.

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactElement } from "react";
import AdminLayout from "./AdminLayout";
import { __resetApiProvidersForTests } from "@/lib/api";

// ── Test harness ──────────────────────────────────────────────────

interface FakeResponse {
  status?: number;
  body: unknown;
}

interface FetchCall {
  url: string;
  init: RequestInit;
}

function installFetch(scripted: Record<string, FakeResponse[]>): {
  calls: FetchCall[];
  restore: () => void;
} {
  const calls: FetchCall[] = [];
  const original = globalThis.fetch;
  const queues: Record<string, FakeResponse[]> = {};
  for (const [path, responses] of Object.entries(scripted)) {
    queues[path] = [...responses];
  }
  // Longer suffixes first so `/admin/api/v1/me` matches before
  // `/api/v1/me` — both end in `/me`.
  const paths = Object.keys(queues).sort((a, b) => b.length - a.length);
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    const path = paths.find((candidate) => resolved.endsWith(candidate));
    if (!path) throw new Error(`Unscripted fetch: ${resolved}`);
    const next = queues[path]!.shift();
    if (!next) throw new Error(`No more responses for: ${resolved}`);
    const status = next.status ?? 200;
    const ok = status >= 200 && status < 300;
    return {
      ok,
      status,
      statusText: ok ? "OK" : "Error",
      text: async () => JSON.stringify(next.body),
    } as unknown as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function Harness({ initial = "/admin/dashboard" }: { initial?: string }): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          {/* Stand-in for App.tsx's RoleHome at `/` — exact behaviour
              isn't this test's concern; we only need to observe that
              the redirect lands here. */}
          <Route path="/" element={<div data-testid="role-home">role home</div>} />
          <Route element={<AdminLayout />}>
            <Route
              path="/admin/dashboard"
              element={<div data-testid="admin-dashboard">admin dashboard</div>}
            />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  vi.unstubAllGlobals();
});

// ── Tests ─────────────────────────────────────────────────────────

describe("<AdminLayout> — role guard (closes cd-28s7)", () => {
  it("redirects a signed-in non-admin to RoleHome instead of mounting the admin outlet", async () => {
    const { restore } = installFetch({
      // /api/v1/me — caller is a worker, NOT a deployment admin.
      "/api/v1/me": [
        {
          status: 200,
          body: {
            role: "worker",
            theme: "light",
            agent_sidebar_collapsed: false,
            employee: { id: "emp_1", name: "Maria" },
            manager_name: "Élodie",
            today: "2026-05-02",
            now: "2026-05-02T10:00:00Z",
            user_id: "01HZ_USER",
            agent_approval_mode: "auto",
            current_workspace_id: "ws_1",
            available_workspaces: [],
            client_binding_org_ids: [],
            is_deployment_admin: false,
            is_deployment_owner: false,
          },
        },
      ],
      // /admin/api/v1/me — backend rejects (404 is the spec'd signal,
      // mirrored here as a non-2xx so `useQuery({ retry: false })`
      // flips to `isError`).
      "/admin/api/v1/me": [{ status: 404, body: { detail: "not an admin" } }],
    });

    try {
      render(<Harness />);
      await waitFor(() => {
        expect(screen.getByTestId("role-home")).toBeInTheDocument();
      });
      // Outlet must NOT have rendered — that would mean the worker saw
      // admin chrome before the redirect committed.
      expect(screen.queryByTestId("admin-dashboard")).toBeNull();
    } finally {
      restore();
    }
  });

  it("also redirects when /api/v1/me carries is_deployment_admin: false even if /admin/api/v1/me is still resolving", async () => {
    // The guard short-circuits on `is_deployment_admin === false` —
    // we don't wait for the admin probe to finish before sending the
    // worker home. The admin probe is scripted to 404 anyway because
    // jsdom + react-query will fire it; not scripting a response would
    // fail the test with "Unscripted fetch".
    const { restore } = installFetch({
      "/api/v1/me": [
        {
          status: 200,
          body: {
            role: "manager",
            theme: "light",
            agent_sidebar_collapsed: false,
            employee: { id: "emp_2", name: "Élodie" },
            manager_name: "Élodie",
            today: "2026-05-02",
            now: "2026-05-02T10:00:00Z",
            user_id: "01HZ_MGR",
            agent_approval_mode: "auto",
            current_workspace_id: "ws_1",
            available_workspaces: [],
            client_binding_org_ids: [],
            is_deployment_admin: false,
            is_deployment_owner: false,
          },
        },
      ],
      "/admin/api/v1/me": [{ status: 404, body: { detail: "not an admin" } }],
    });

    try {
      render(<Harness />);
      await waitFor(() => {
        expect(screen.getByTestId("role-home")).toBeInTheDocument();
      });
      expect(screen.queryByTestId("admin-dashboard")).toBeNull();
    } finally {
      restore();
    }
  });

});
