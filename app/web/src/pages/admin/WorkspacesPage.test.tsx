import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import WorkspacesPage from "./WorkspacesPage";

interface FakeResponse {
  status?: number;
  body: unknown;
}

interface FetchCall {
  url: string;
  init: RequestInit;
}

interface DeferredResponse {
  promise: Promise<FakeResponse>;
  resolve: (response: FakeResponse) => void;
}

function deferredResponse(): DeferredResponse {
  let resolve!: (response: FakeResponse) => void;
  const promise = new Promise<FakeResponse>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function installFetch(
  scripted: Record<string, Array<FakeResponse | Promise<FakeResponse>>>,
): {
  calls: FetchCall[];
  restore: () => void;
} {
  const calls: FetchCall[] = [];
  const original = globalThis.fetch;
  const queues: Record<string, Array<FakeResponse | Promise<FakeResponse>>> = {};
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
    const response = await next;
    const status = response.status ?? 200;
    const ok = status >= 200 && status < 300;
    return {
      ok,
      status,
      statusText: ok ? "OK" : "Error",
      text: async () => JSON.stringify(response.body),
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
  // code-health: ignore[nloc] Route harness keeps workspace admin fixtures local to the integration test.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/admin/workspaces"]}>
        <WorkspacesPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function workspaces(overrides: Record<string, unknown> = {}): unknown {
  return {
    workspaces: [
      {
        id: "ws_1",
        slug: "smoke",
        name: "Smoke House",
        plan: "free",
        verification_state: "unverified",
        properties_count: 3,
        members_count: 4,
        spent_cents_30d: 600,
        cap_cents_30d: 1000,
        archived_at: null,
        created_at: "2026-05-06T12:00:00.000Z",
      },
      {
        id: "ws_2",
        slug: "archive",
        name: "Archive House",
        plan: "pro",
        verification_state: "human_verified",
        properties_count: 1,
        members_count: 2,
        spent_cents_30d: 75,
        cap_cents_30d: 500,
        archived_at: "2026-04-20T12:00:00.000Z",
        created_at: "2026-03-01T00:00:00+00:00",
      },
    ],
    ...overrides,
  };
}

function workspacePage(
  rows: Array<Record<string, unknown>>,
  overrides: Record<string, unknown> = {},
): unknown {
  return {
    workspaces: rows,
    data: rows,
    next_cursor: null,
    has_more: false,
    ...overrides,
  };
}

function activeWorkspace(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "ws_1",
    slug: "smoke",
    name: "Smoke House",
    plan: "free",
    verification_state: "unverified",
    properties_count: 3,
    members_count: 4,
    spent_cents_30d: 600,
    cap_cents_30d: 1000,
    archived_at: null,
    created_at: "2026-05-06T12:00:00.000Z",
    ...overrides,
  };
}

function smokeArchived(): unknown {
  return workspaces({
    workspaces: [
      {
        id: "ws_1",
        slug: "smoke",
        name: "Smoke House",
        plan: "free",
        verification_state: "unverified",
        properties_count: 3,
        members_count: 4,
        spent_cents_30d: 600,
        cap_cents_30d: 1000,
        archived_at: "2026-04-29T12:00:00.000Z",
        created_at: "2026-04-01T00:00:00+00:00",
      },
    ],
  });
}

function installPageFetch(extra: Record<string, Array<FakeResponse | Promise<FakeResponse>>> = {}) {
  return installFetch({
    "/admin/api/v1/workspaces": [{ body: workspaces() }, { body: workspaces() }],
    ...extra,
  });
}

function rowFor(text: string): HTMLElement {
  const row = screen.getByText(text).closest("[data-inline-table-row-group], tr");
  if (!(row instanceof HTMLElement)) throw new Error(`No row for ${text}`);
  return row;
}

function jsonBody(call: FetchCall): unknown {
  return JSON.parse(String(call.init.body));
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
});

describe("Admin WorkspacesPage", () => {
  it("renders active and archived workspace rows from the API envelope", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-05-10T12:00:00.000Z"));
    const fetcher = installPageFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Smoke House")).toBeInTheDocument();
      expect(screen.getByRole("table", { name: "Active workspaces" })).toBeInTheDocument();
      expect(screen.getByText("Active (1)")).toBeInTheDocument();
      expect(screen.getByText("Archived (1)")).toBeInTheDocument();

      const row = rowFor("Smoke House");
      expect(within(row).getByText("/w/smoke")).toBeInTheDocument();
      expect(within(row).getByText("free")).toBeInTheDocument();
      expect(within(row).getByText("unverified")).toBeInTheDocument();
      expect(within(row).getByText("3")).toBeInTheDocument();
      expect(within(row).getByText("4")).toBeInTheDocument();
      expect(within(row).getByText("$6.00")).toBeInTheDocument();
      expect(within(row).getByText("$10.00")).toBeInTheDocument();
      const createdAt = within(row).getByText("4 days ago");
      expect(createdAt.tagName).toBe("TIME");
      expect(createdAt).toHaveAttribute("dateTime", "2026-05-06T12:00:00.000Z");
      expect(createdAt).toHaveAttribute("title", expect.stringContaining("May 6, 2026"));
      expect(within(row).queryByText("2026-05-06T12:00:00.000Z")).not.toBeInTheDocument();

      const archivedRow = rowFor("Archive House");
      expect(within(archivedRow).getByText("/w/archive")).toBeInTheDocument();
      expect(within(archivedRow).getByText("pro")).toBeInTheDocument();
      const archivedAt = within(archivedRow).getByText("April 20th, 2026");
      expect(archivedAt.tagName).toBe("TIME");
      expect(archivedAt).toHaveAttribute("dateTime", "2026-04-20T12:00:00.000Z");
      expect(archivedAt).toHaveAttribute("title", expect.stringContaining("April 20, 2026"));
      expect(within(archivedRow).queryByText("2026-04-20T12:00:00.000Z")).not.toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("searches active workspaces with server-backed name and slug query", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetcher = installFetch({
      "/admin/api/v1/workspaces": [
        { body: workspaces() },
        { body: workspaces() },
        {
          body: workspacePage([
            activeWorkspace({
              id: "ws_3",
              slug: "north-annex",
              name: "North Annex",
              plan: "pro",
              verification_state: "human_verified",
            }),
          ]),
        },
      ],
    });
    try {
      render(<Harness />);
      await screen.findByText("Smoke House");

      fireEvent.change(screen.getByLabelText("Search workspaces"), {
        target: { value: "north" },
      });
      expect(
        fetcher.calls.some((call) => call.url.includes("q=north")),
      ).toBe(false);
      await vi.advanceTimersByTimeAsync(250);

      expect(await screen.findByText("North Annex")).toBeInTheDocument();
      expect(screen.queryByText("Smoke House")).not.toBeInTheDocument();
      const searchCall = fetcher.calls.find((call) => call.url.includes("q=north"));
      expect(searchCall?.url).toContain("limit=25");
    } finally {
      fetcher.restore();
    }
  });

  it("carries the debounced search query into cursor loading", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetcher = installFetch({
      "/admin/api/v1/workspaces": [
        { body: workspaces() },
        { body: workspaces() },
        {
          body: workspacePage([
            activeWorkspace({
              id: "ws_3",
              slug: "north-annex",
              name: "North Annex",
              plan: "pro",
              verification_state: "human_verified",
            }),
          ], {
            next_cursor: "north-cursor-2",
            has_more: true,
          }),
        },
        {
          body: workspacePage([
            activeWorkspace({
              id: "ws_4",
              slug: "north-boathouse",
              name: "North Boathouse",
              plan: "pro",
              created_at: "2026-05-08T12:00:00.000Z",
            }),
          ]),
        },
      ],
    });
    try {
      render(<Harness />);
      await screen.findByText("Smoke House");

      fireEvent.change(screen.getByLabelText("Search workspaces"), {
        target: { value: "north" },
      });
      await vi.advanceTimersByTimeAsync(250);
      expect(await screen.findByText("North Annex")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Load more rows" }));

      expect(await screen.findByText("North Boathouse")).toBeInTheDocument();
      const nextPageCall = fetcher.calls.find((call) =>
        call.url.includes("cursor=north-cursor-2"),
      );
      expect(nextPageCall?.url).toContain("q=north");
      expect(nextPageCall?.url).toContain("limit=25");
    } finally {
      fetcher.restore();
    }
  });

  it("loads the next active workspace page through the cursor controls", async () => {
    const fetcher = installFetch({
      "/admin/api/v1/workspaces": [
        {
          body: workspacePage([activeWorkspace()], {
            next_cursor: "cursor-2",
            has_more: true,
          }),
        },
        { body: workspaces() },
        {
          body: workspacePage([
            activeWorkspace({
              id: "ws_3",
              slug: "lake",
              name: "Lake House",
              created_at: "2026-05-07T12:00:00.000Z",
            }),
          ]),
        },
      ],
    });
    try {
      render(<Harness />);
      await screen.findByText("Smoke House");

      fireEvent.click(screen.getByRole("button", { name: "Load more rows" }));

      expect(await screen.findByText("Lake House")).toBeInTheDocument();
      const nextPageCall = fetcher.calls.find((call) =>
        call.url.includes("cursor=cursor-2"),
      );
      expect(nextPageCall?.url).toContain("limit=25");
    } finally {
      fetcher.restore();
    }
  });

  it("keeps an edited cap draft while cursor loading appends rows", async () => {
    const fetcher = installFetch({
      "/admin/api/v1/workspaces": [
        {
          body: workspacePage([activeWorkspace()], {
            next_cursor: "cursor-2",
            has_more: true,
          }),
        },
        { body: workspaces() },
        {
          body: workspacePage([
            activeWorkspace({
              id: "ws_3",
              slug: "lake",
              name: "Lake House",
              created_at: "2026-05-07T12:00:00.000Z",
            }),
          ]),
        },
      ],
    });
    try {
      render(<Harness />);
      await screen.findByText("Smoke House");

      const row = rowFor("Smoke House");
      fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
      fireEvent.change(within(row).getByRole("textbox", { name: "30 day cap dollars" }), {
        target: { value: "12.50" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Load more rows" }));

      expect(await screen.findByText("Lake House")).toBeInTheDocument();
      expect(within(rowFor("Smoke House")).getByRole("textbox", {
        name: "30 day cap dollars",
      })).toHaveValue("12.50");
    } finally {
      fetcher.restore();
    }
  });

  it("trusts a workspace optimistically and rolls back when the request fails", async () => {
    const trustResponse = deferredResponse();
    const fetcher = installPageFetch({
      "/admin/api/v1/workspaces": [
        { body: workspaces() },
        { body: workspaces() },
        { body: workspaces() },
        { body: workspaces() },
      ],
      "/admin/api/v1/workspaces/ws_1/trust": [trustResponse.promise],
    });
    try {
      render(<Harness />);
      await screen.findByText("Smoke House");

      const row = rowFor("Smoke House");
      fireEvent.click(within(row).getByRole("button", { name: "Trust" }));

      expect(await within(row).findByText("trusted")).toBeInTheDocument();
      trustResponse.resolve({ status: 500, body: { detail: "boom" } });

      await waitFor(() => {
        expect(within(rowFor("Smoke House")).getByText("unverified")).toBeInTheDocument();
      });
      const post = fetcher.calls.find((call) =>
        call.url.endsWith("/admin/api/v1/workspaces/ws_1/trust"),
      );
      expect(post?.init.method).toBe("POST");
    } finally {
      fetcher.restore();
    }
  });

  it("archives a workspace after confirmation with the mock confirmation text", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-05-10T12:00:00.000Z"));
    const archiveResponse = deferredResponse();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetcher = installFetch({
      "/admin/api/v1/workspaces": [
        {
          body: workspaces({
            workspaces: [
              {
                id: "ws_1",
                slug: "smoke",
                name: "Smoke House",
                plan: "free",
                verification_state: "unverified",
                properties_count: 3,
                members_count: 4,
                spent_cents_30d: 600,
                cap_cents_30d: 1000,
                archived_at: null,
                created_at: "2026-04-01T00:00:00+00:00",
              },
            ],
          }),
        },
        {
          body: workspaces({
            workspaces: [
              {
                id: "ws_1",
                slug: "smoke",
                name: "Smoke House",
                plan: "free",
                verification_state: "unverified",
                properties_count: 3,
                members_count: 4,
                spent_cents_30d: 600,
                cap_cents_30d: 1000,
                archived_at: null,
                created_at: "2026-04-01T00:00:00+00:00",
              },
            ],
          }),
        },
        { body: smokeArchived() },
        { body: smokeArchived() },
      ],
      "/admin/api/v1/workspaces/ws_1/archive": [archiveResponse.promise],
    });
    try {
      render(<Harness />);
      await screen.findByText("Smoke House");

      fireEvent.click(screen.getByRole("button", { name: "Archive" }));

      expect(confirm).toHaveBeenCalledWith(
        "Archive Smoke House? Owner can restore from backup.",
      );
      expect(await screen.findByText("Active (0)")).toBeInTheDocument();
      expect(screen.getByText("Archived (1)")).toBeInTheDocument();
      const archivedRow = rowFor("Smoke House");
      const archivedAt = within(archivedRow).getByText("just now");
      expect(archivedAt.tagName).toBe("TIME");
      expect(archivedAt.getAttribute("dateTime")).toMatch(
        /^2026-05-10T12:00:00\.\d{3}Z$/,
      );
      expect(archivedAt).toHaveAttribute("title", expect.stringContaining("May 10, 2026"));
      archiveResponse.resolve({
        body: { id: "ws_1", archived_at: "2026-04-29T12:00:00.000Z" },
      });

      await waitFor(() => {
        const post = fetcher.calls.find((call) =>
          call.url.endsWith("/admin/api/v1/workspaces/ws_1/archive"),
        );
        expect(post?.init.method).toBe("POST");
      });
    } finally {
      fetcher.restore();
    }
  });

  it("rolls back the archive optimism when the owners-only endpoint returns 404", async () => {
    const archiveResponse = deferredResponse();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetcher = installPageFetch({
      "/admin/api/v1/workspaces": [
        { body: workspaces() },
        { body: workspaces() },
        { body: workspaces() },
        { body: workspaces() },
      ],
      "/admin/api/v1/workspaces/ws_1/archive": [archiveResponse.promise],
    });
    try {
      render(<Harness />);
      await screen.findByText("Smoke House");

      const row = rowFor("Smoke House");
      fireEvent.click(within(row).getByRole("button", { name: "Archive" }));

      expect(confirm).toHaveBeenCalledWith(
        "Archive Smoke House? Owner can restore from backup.",
      );
      expect(await screen.findByText("Active (0)")).toBeInTheDocument();
      archiveResponse.resolve({ status: 404, body: { error: "not_found" } });

      expect(await screen.findByText("Active (1)")).toBeInTheDocument();
      expect(screen.getByText("Archived (1)")).toBeInTheDocument();
      expect(rowFor("Smoke House")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("saves cap edits through the usage cap endpoint and updates the row", async () => {
    const fetcher = installPageFetch({
      "/admin/api/v1/workspaces": [
        { body: workspaces() },
        { body: workspaces() },
        {
          body: workspaces({
            workspaces: [
              {
                id: "ws_1",
                slug: "smoke",
                name: "Smoke House",
                plan: "free",
                verification_state: "unverified",
                properties_count: 3,
                members_count: 4,
                spent_cents_30d: 600,
                cap_cents_30d: 1250,
                archived_at: null,
                created_at: "2026-04-01T00:00:00+00:00",
              },
            ],
          }),
        },
        {
          body: workspaces({
            workspaces: [
              {
                id: "ws_1",
                slug: "smoke",
                name: "Smoke House",
                plan: "free",
                verification_state: "unverified",
                properties_count: 3,
                members_count: 4,
                spent_cents_30d: 600,
                cap_cents_30d: 1250,
                archived_at: null,
                created_at: "2026-04-01T00:00:00+00:00",
              },
            ],
          }),
        },
      ],
      "/admin/api/v1/usage/workspaces/ws_1/cap": [
        { body: { workspace_id: "ws_1", cap_cents_30d: 1250 } },
      ],
      "/admin/api/v1/usage/workspaces": [{ body: { workspaces: [] } }],
      "/admin/api/v1/usage/summary": [
        {
          body: {
            window_label: "rolling 30 days",
            deployment_spend_cents_30d: 0,
            deployment_calls_30d: 0,
            workspace_count: 1,
            paused_workspace_count: 0,
            per_capability: [],
          },
        },
      ],
    });
    try {
      render(<Harness />);
      await screen.findByText("Smoke House");

      const row = rowFor("Smoke House");
      fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
      const input = within(row).getByRole("textbox", { name: "30 day cap dollars" });
      const save = within(row).getByRole("button", { name: "Save" });
      fireEvent.change(input, { target: { value: "" } });
      fireEvent.click(save);
      expect(
        await within(row).findByText("Enter a dollar amount from 0.00 to 10000.00."),
      ).toBeInTheDocument();
      fireEvent.change(input, { target: { value: "10000.01" } });
      fireEvent.click(save);
      expect(
        await within(row).findByText("Enter a dollar amount from 0.00 to 10000.00."),
      ).toBeInTheDocument();
      fireEvent.change(input, { target: { value: "12.345" } });
      fireEvent.click(save);
      expect(
        await within(row).findByText("Enter a dollar amount from 0.00 to 10000.00."),
      ).toBeInTheDocument();
      fireEvent.change(input, { target: { value: "12.50" } });
      fireEvent.click(save);

      await waitFor(() => {
        const put = fetcher.calls.find((call) =>
          call.url.endsWith("/admin/api/v1/usage/workspaces/ws_1/cap"),
        );
        expect(put).toBeDefined();
        expect(put?.init.method).toBe("PUT");
        expect(jsonBody(put!)).toEqual({ cap_cents_30d: 1250 });
      });
      expect(await within(row).findByText("$12.50")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("cancels cap edits without calling the usage cap endpoint", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);
      await screen.findByText("Smoke House");

      const row = rowFor("Smoke House");
      fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
      fireEvent.change(within(row).getByRole("textbox", { name: "30 day cap dollars" }), {
        target: { value: "12.50" },
      });
      fireEvent.click(within(row).getByRole("button", { name: "Cancel" }));

      expect(within(row).getByText("$10.00")).toBeInTheDocument();
      expect(
        fetcher.calls.some((call) =>
          call.url.endsWith("/admin/api/v1/usage/workspaces/ws_1/cap"),
        ),
      ).toBe(false);
    } finally {
      fetcher.restore();
    }
  });

  it("rolls back cap mutation optimism after a failed save", async () => {
    const capResponse = deferredResponse();
    const fetcher = installPageFetch({
      "/admin/api/v1/workspaces": [
        { body: workspaces() },
        { body: workspaces() },
        { body: workspaces() },
        { body: workspaces() },
      ],
      "/admin/api/v1/usage/workspaces/ws_1/cap": [capResponse.promise],
    });
    try {
      render(<Harness />);
      await screen.findByText("Smoke House");

      const row = rowFor("Smoke House");
      fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
      const input = within(row).getByRole("textbox", { name: "30 day cap dollars" });
      fireEvent.change(input, {
        target: { value: "12.50" },
      });
      fireEvent.click(within(row).getByRole("button", { name: "Save" }));

      expect(input).toHaveValue("12.50");
      capResponse.resolve({ status: 500, body: { detail: "boom" } });

      expect(await within(row).findByText("Could not save cap. Try again.")).toBeInTheDocument();
      fireEvent.click(within(row).getByRole("button", { name: "Cancel" }));
      expect(within(rowFor("Smoke House")).getByText("$10.00")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });
});
