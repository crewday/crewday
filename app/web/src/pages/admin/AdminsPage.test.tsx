import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import AdminAdminsPage from "./AdminsPage";

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
  const paths = Object.keys(queues).sort((a, b) => b.length - a.length);
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    const pathname = new URL(resolved, "http://crewday.test").pathname;
    const path = paths.find((candidate) => pathname === candidate);
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

function Harness(): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/admin/admins"]}>
        <AdminAdminsPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function me(): unknown {
  return {
    user_id: "user_1",
    display_name: "Ada",
    email: "ada@example.com",
    is_owner: true,
    capabilities: {},
  };
}

function admins(overrides: Record<string, unknown> = {}): unknown {
  return {
    admins: [
      {
        id: "grant_1",
        user_id: "user_1",
        display_name: "Ada Lovelace",
        email: "ada@example.com",
        is_owner: true,
        granted_at: "2026-04-25T12:00:00+00:00",
        granted_by: "system",
      },
      {
        id: "grant_2",
        user_id: "user_2",
        display_name: "Grace Hopper",
        email: "grace@example.com",
        is_owner: false,
        granted_at: "2026-04-26T12:00:00+00:00",
        granted_by: "user_1",
      },
    ],
    ...overrides,
  };
}

function ownersGroup(): unknown {
  return {
    members: [
      {
        user_id: "user_3",
        display_name: "Katherine Johnson",
        email: "katherine@example.com",
        added_at: "2026-04-27T12:00:00+00:00",
        added_by: "user_1",
      },
    ],
  };
}

function installPageFetch(extra: Record<string, FakeResponse[]> = {}) {
  return installFetch({
    "/admin/api/v1/me": [{ body: me() }],
    "/admin/api/v1/admins": [{ body: admins() }],
    ...extra,
  });
}

function rowFor(text: string): HTMLTableRowElement {
  const row = screen.getByText(text).closest("tr");
  if (!(row instanceof HTMLTableRowElement)) throw new Error(`No row for ${text}`);
  return row;
}

function jsonBody(call: FetchCall): unknown {
  return JSON.parse(String(call.init.body));
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
});

describe("Admin AdminsPage", () => {
  it("renders admin counts, table rows, and grant form from the admins envelope", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Deployment admin team (2)")).toBeInTheDocument();
      expect(screen.getByText("1 owner")).toBeInTheDocument();

      const ownerRow = rowFor("Ada Lovelace");
      expect(within(ownerRow).getByText("ada@example.com")).toBeInTheDocument();
      expect(within(ownerRow).getByText("owner")).toBeInTheDocument();
      expect(within(ownerRow).getByText("system")).toBeInTheDocument();

      const adminRow = rowFor("Grace Hopper");
      expect(within(adminRow).getByText("grace@example.com")).toBeInTheDocument();
      expect(within(adminRow).getByText("admin")).toBeInTheDocument();
      expect(within(adminRow).getByText("user_1")).toBeInTheDocument();

      expect(screen.getByRole("button", { name: "Grant admin" })).toBeInTheDocument();
      expect(screen.getByLabelText("Email")).toBeInTheDocument();
      expect(fetcher.calls.map((call) => call.url)).toContain("/admin/api/v1/admins");
    } finally {
      fetcher.restore();
    }
  });

  it("grants owners through the admin and owners-group envelopes", async () => {
    const fetcher = installPageFetch({
      "/admin/api/v1/admins": [
        { body: admins() },
        {
          body: {
            admin: {
              id: "grant_3",
              user_id: "user_3",
              display_name: "Katherine Johnson",
              email: "katherine@example.com",
              is_owner: false,
              granted_at: "2026-04-27T12:00:00+00:00",
              granted_by: "user_1",
            },
          },
        },
        { body: admins() },
      ],
      "/admin/api/v1/admins/groups/owners/members": [{ body: ownersGroup() }],
    });
    try {
      render(<Harness />);
      await screen.findByText("Deployment admin team (2)");

      fireEvent.change(screen.getByLabelText("Email"), {
        target: { value: "katherine@example.com" },
      });
      fireEvent.click(screen.getByLabelText("Owners can archive workspaces and edit root-protected settings."));
      fireEvent.click(screen.getByRole("button", { name: "Grant admin" }));

      await waitFor(() => {
        expect(
          fetcher.calls.some((call) =>
            call.url.endsWith("/admin/api/v1/admins/groups/owners/members"),
          ),
        ).toBe(true);
      });

      const adminGrant = fetcher.calls.find(
        (call) => call.url.endsWith("/admin/api/v1/admins") && call.init.method === "POST",
      );
      expect(adminGrant).toBeDefined();
      expect(jsonBody(adminGrant!)).toEqual({ email: "katherine@example.com" });

      const ownerGrant = fetcher.calls.find((call) =>
        call.url.endsWith("/admin/api/v1/admins/groups/owners/members"),
      );
      expect(ownerGrant?.init.method).toBe("POST");
      expect(jsonBody(ownerGrant!)).toEqual({ email: "katherine@example.com" });
    } finally {
      fetcher.restore();
    }
  });
});
