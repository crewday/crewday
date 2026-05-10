import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import DashboardPage from "./DashboardPage";

interface FakeResponse {
  body: unknown;
}

function dashboardPayloads(auditRows: unknown[] = []): Record<string, FakeResponse[]> {
  return {
    "/admin/api/v1/usage/summary": [
      {
        body: {
          window_label: "rolling 30 days",
          deployment_spend_cents_30d: 2500,
          deployment_calls_30d: 50,
          workspace_count: 1,
          paused_workspace_count: 0,
          per_capability: [],
        },
      },
    ],
    "/admin/api/v1/usage/workspaces": [
      {
        body: {
          workspaces: [],
        },
      },
    ],
    "/admin/api/v1/workspaces": [
      {
        body: {
          workspaces: [],
        },
      },
    ],
    "/admin/api/v1/audit": [
      { body: { data: auditRows, next_cursor: null, has_more: false } },
    ],
  };
}

function installFetch(scripted: Record<string, FakeResponse[]>): () => void {
  const original = globalThis.fetch;
  const queues: Record<string, FakeResponse[]> = {};
  for (const [path, responses] of Object.entries(scripted)) {
    queues[path] = [...responses];
  }
  const spy = vi.fn(async (url: string | URL | Request) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const pathname = new URL(resolved, "http://crewday.test").pathname;
    const next = queues[pathname]?.shift();
    if (!next) throw new Error(`Unscripted fetch: ${resolved}`);
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify(next.body),
    } as unknown as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return () => {
    (globalThis as { fetch: typeof fetch }).fetch = original;
  };
}

function Harness(): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/admin/dashboard"]}>
        <DashboardPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
});

describe("Admin DashboardPage", () => {
  it("renders recent audit actor and target cells through the readable admin audit row", async () => {
    const actorId = "01KR5S2VJ5TGPKGXWYW9C9951M";
    const entityId = "01KR8B7KBHXE03FQMXZ6XBVRQE";
    const restore = installFetch(
      dashboardPayloads([
        {
          id: "row-readable",
          actor_id: actorId,
          actor_kind: "user",
          actor_grant_role: "admin",
          actor_was_owner_member: true,
          entity_kind: "admin_agent_message",
          entity_id: entityId,
          action: "admin.agent.message.created.with.a.very.long.audit.action.value",
          diff: {},
          correlation_id: "mock",
          created_at: "2026-04-18T12:00:00+00:00",
        },
      ]),
    );
    try {
      render(<Harness />);

      expect(await screen.findByText("User")).toBeInTheDocument();
      expect(screen.getByText("Admin")).toBeInTheDocument();
      expect(screen.getByText("Owner")).toBeInTheDocument();
      expect(screen.getByText(actorId)).toBeInTheDocument();
      expect(screen.getByText("Admin agent message")).toBeInTheDocument();
      expect(screen.getByText(entityId)).toBeInTheDocument();
      expect(screen.queryByText(`user ${actorId} · owner`)).not.toBeInTheDocument();
      expect(screen.queryByText(`admin_agent_message:${entityId}`)).not.toBeInTheDocument();
    } finally {
      restore();
    }
  });

  it("excludes archived workspaces from pressure rows", async () => {
    // code-health: ignore[nloc] Integration fixture spells out all dashboard API payloads for this route case.
    const restore = installFetch({
      "/admin/api/v1/usage/summary": [
        {
          body: {
            window_label: "rolling 30 days",
            deployment_spend_cents_30d: 2500,
            deployment_calls_30d: 50,
            workspace_count: 2,
            paused_workspace_count: 1,
            per_capability: [],
          },
        },
      ],
      "/admin/api/v1/usage/workspaces": [
        {
          body: {
            workspaces: [
              {
                workspace_id: "ws_active",
                slug: "active",
                name: "Active House",
                cap_cents_30d: 1000,
                spent_cents_30d: 800,
                percent: 80,
                paused: false,
              },
              {
                workspace_id: "ws_archived",
                slug: "archived",
                name: "Archived House",
                cap_cents_30d: 1000,
                spent_cents_30d: 1000,
                percent: 100,
                paused: true,
              },
            ],
          },
        },
      ],
      "/admin/api/v1/workspaces": [
        {
          body: {
            workspaces: [
              {
                id: "ws_active",
                slug: "active",
                name: "Active House",
                plan: "free",
                verification_state: "trusted",
                members_count: 2,
                archived_at: null,
                created_at: "2026-04-01T00:00:00+00:00",
              },
              {
                id: "ws_archived",
                slug: "archived",
                name: "Archived House",
                plan: "free",
                verification_state: "trusted",
                members_count: 2,
                archived_at: "2026-04-02T00:00:00+00:00",
                created_at: "2026-04-01T00:00:00+00:00",
              },
            ],
          },
        },
      ],
      "/admin/api/v1/audit": [
        { body: { data: [], next_cursor: null, has_more: false } },
      ],
    });
    try {
      render(<Harness />);

      expect(await screen.findByText("Active House")).toBeInTheDocument();
      expect(screen.queryByText("Archived House")).not.toBeInTheDocument();
    } finally {
      restore();
    }
  });
});
