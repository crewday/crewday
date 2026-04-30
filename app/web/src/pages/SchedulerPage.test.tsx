import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import type { Role, SchedulerCalendarPayload } from "@/types/api";
import SchedulerPage from "./SchedulerPage";
import appSource from "../App.tsx?raw";

const roleState = vi.hoisted(() => ({ role: "manager" as Role }));

vi.mock("@/context/RoleContext", () => ({
  useRole: () => ({ role: roleState.role, setRole: vi.fn() }),
}));

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? "OK" : "Error",
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const CALENDAR: SchedulerCalendarPayload = {
  window: { from: "2026-05-04", to: "2026-05-10" },
  rulesets: [{ id: "ruleset_housekeeping", workspace_id: "ws_1", name: "Housekeeping" }],
  slots: [
    {
      id: "slot_monday",
      schedule_ruleset_id: "ruleset_housekeeping",
      weekday: 0,
      starts_local: "08:00",
      ends_local: "12:00",
    },
  ],
  assignments: [
    {
      id: "assignment_alex",
      user_id: "user_alex",
      work_role_id: "role_cleaner",
      property_id: "prop_villa",
      schedule_ruleset_id: "ruleset_housekeeping",
    },
  ],
  tasks: [
    {
      id: "task_turnover",
      title: "Turnover clean",
      property_id: "prop_villa",
      user_id: "user_alex",
      scheduled_start: "2026-05-04T09:30:00Z",
      estimated_minutes: 90,
      priority: "normal",
      status: "pending",
    },
  ],
  users: [{ id: "user_alex", first_name: "Alex", display_name: "Alex Rivera" }],
  properties: [{ id: "prop_villa", name: "Villa Rosa", timezone: "Europe/Lisbon" }],
};

function installFetch(payload: SchedulerCalendarPayload = CALENDAR) {
  const calls: string[] = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push(resolved);
    return jsonResponse(payload);
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
      <WorkspaceProvider>
        <MemoryRouter initialEntries={["/scheduler"]}>
          <SchedulerPage />
        </MemoryRouter>
      </WorkspaceProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  roleState.role = "manager";
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

describe("<SchedulerPage>", () => {
  it("wires the shared scheduler route and its workspace-scoped alias", () => {
    expect(appSource).toContain('<Route path="/scheduler" element={<SchedulerPage />} />');
    expect(appSource).toContain('<Route path="/w/:slug/scheduler" element={<SchedulerPage />} />');
  });

  it("loads the production calendar feed and renders the promoted grid", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Alex Rivera")).toBeInTheDocument();
      expect(screen.getByText("08:00–12:00")).toBeInTheDocument();
      expect(screen.getByText("Turnover clean")).toBeInTheDocument();
      expect(screen.getByText("Villa Rosa")).toBeInTheDocument();
      expect(fake.calls).toHaveLength(1);
      expect(fake.calls[0]).toMatch(
        /^\/w\/acme\/api\/v1\/scheduler\/calendar\?from=\d{4}-\d{2}-\d{2}&to=\d{4}-\d{2}-\d{2}$/,
      );
      expect(fake.calls[0]).not.toContain("from_=");
    } finally {
      fake.restore();
    }
  });

  it("keeps client views to first names while still rendering scoped rota data", async () => {
    roleState.role = "client";
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Alex")).toBeInTheDocument();
      expect(screen.queryByText("Alex Rivera")).toBeNull();
      expect(screen.getByText("08:00–12:00")).toBeInTheDocument();
      expect(screen.queryByText("gap")).toBeNull();
    } finally {
      fake.restore();
    }
  });
});
