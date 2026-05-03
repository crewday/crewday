import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import AuditPage from "./AuditPage";

interface AuditRow {
  id: string;
  actor_id: string;
  actor_kind: "user" | "agent" | "system";
  actor_grant_role: string;
  actor_was_owner_member: boolean;
  entity_kind: string;
  entity_id: string;
  action: string;
  diff: Record<string, unknown>;
  correlation_id: string;
  created_at: string;
}

function row(overrides: Partial<AuditRow>): AuditRow {
  return {
    id: "row-x",
    actor_id: "u-elodie",
    actor_kind: "user",
    actor_grant_role: "admin",
    actor_was_owner_member: true,
    entity_kind: "deployment",
    entity_id: "ws-04",
    action: "deployment.budget.updated",
    diff: {},
    correlation_id: "mock",
    created_at: "2026-04-18T12:00:00+00:00",
    ...overrides,
  };
}

function installFetch(rows: AuditRow[]): {
  calls: string[];
  restore: () => void;
} {
  const original = globalThis.fetch;
  const calls: string[] = [];
  const spy = vi.fn(async (url: string | URL | Request) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push(resolved);
    if (resolved.startsWith("/admin/api/v1/audit")) {
      return {
        ok: true,
        status: 200,
        statusText: "OK",
        text: async () =>
          JSON.stringify({ data: rows, next_cursor: null, has_more: false }),
      } as unknown as Response;
    }
    throw new Error("Unscripted fetch: " + resolved);
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function Harness({ initial = "/admin/audit" }: { initial?: string }): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <AuditPage />
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

describe("Admin AuditPage", () => {
  it("forwards actor_id / action filters to the wire query but keeps actor_kind client-side", async () => {
    const fake = installFetch([
      row({ id: "a", actor_kind: "user", actor_id: "u-elodie", action: "deployment.budget.updated" }),
      row({ id: "b", actor_kind: "agent", actor_id: "agent-admin", action: "deployment.budget.adjusted" }),
    ]);
    try {
      render(
        <Harness initial="/admin/audit?actor_kind=user&actor_id=u-elodie&action=deployment.budget.updated" />,
      );

      // The user row should render and the agent row filtered out by
      // the client-side actor_kind tab.
      expect(await screen.findByText("deployment.budget.updated")).toBeInTheDocument();
      expect(screen.queryByText("deployment.budget.adjusted")).toBeNull();

      const auditCall = fake.calls.find((c) => c.startsWith("/admin/api/v1/audit?"));
      expect(auditCall).toBeTruthy();
      // Wire-shaped slice — actor_id + action go through.
      expect(auditCall).toContain("actor_id=u-elodie");
      expect(auditCall).toContain("action=deployment.budget.updated");
      // actor_kind is client-side: must NOT appear on the wire.
      expect(auditCall ?? "").not.toContain("actor_kind=");
    } finally {
      fake.restore();
    }
  });

  it("emits since/until as UTC ISO so the picker's local day is preserved across timezones", async () => {
    const fake = installFetch([row({ id: "x", action: "deployment.budget.updated" })]);
    try {
      render(<Harness initial="/admin/audit?since=2026-05-03&until=2026-05-04" />);

      await waitFor(() =>
        expect(
          fake.calls.find((c) => c.startsWith("/admin/api/v1/audit?")),
        ).toBeTruthy(),
      );
      const auditCall = fake.calls.find((c) => c.startsWith("/admin/api/v1/audit?"))!;
      const url = new URL(auditCall, "http://crewday.test");
      const since = url.searchParams.get("since");
      const until = url.searchParams.get("until");
      // Both must be ISO-8601 with explicit timezone (UTC `Z` or
      // numeric offset) so the backend, which treats naive ISO as
      // UTC, does not silently shift the local-day boundary.
      expect(since).toMatch(/^[0-9T:.-]+(Z|[+-]\d{2}:\d{2})$/);
      expect(until).toMatch(/^[0-9T:.-]+(Z|[+-]\d{2}:\d{2})$/);
      // Round-trip through Date: the since boundary must equal the
      // start of 2026-05-03 in the *local* zone the picker used; the
      // until boundary must equal the end of 2026-05-04. We compare
      // against the locally-constructed timestamp the helper would
      // build — independent of the test runner's TZ.
      const expectedSince = new Date(2026, 4, 3, 0, 0, 0, 0).toISOString();
      const expectedUntil = new Date(2026, 4, 4, 23, 59, 59, 999).toISOString();
      expect(since).toBe(expectedSince);
      expect(until).toBe(expectedUntil);
    } finally {
      fake.restore();
    }
  });

  it("renders an empty-state when the filter excludes every row", async () => {
    const fake = installFetch([row({ actor_kind: "agent" })]);
    try {
      render(<Harness initial="/admin/audit?actor_kind=user" />);

      expect(
        await screen.findByText("No audit rows match this filter."),
      ).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });
});
