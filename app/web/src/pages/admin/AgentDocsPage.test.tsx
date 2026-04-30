import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import type { AgentDoc, AgentDocSummary } from "@/types/api";
import AgentDocsPage from "./AgentDocsPage";

interface FakeResponse {
  status?: number;
  body: unknown;
}

interface FetchCall {
  url: string;
  init: RequestInit;
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? "OK" : "Error",
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function installFetch(scripted: Record<string, FakeResponse[]>): {
  calls: FetchCall[];
  restore: () => void;
} {
  const calls: FetchCall[] = [];
  const queues = new Map<string, FakeResponse[]>();
  for (const [path, responses] of Object.entries(scripted)) {
    queues.set(path, [...responses]);
  }
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const pathname = new URL(resolved, "http://crewday.test").pathname;
    calls.push({ url: resolved, init: init ?? {} });
    const queue = queues.get(pathname);
    if (!queue) throw new Error(`Unexpected fetch call: ${resolved}`);
    const next = queue.shift();
    if (!next) throw new Error(`No more responses for: ${resolved}`);
    return jsonResponse(next.body, next.status);
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
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/admin/agent-docs"]}>
        <AgentDocsPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const openApiSpec = {
  paths: {
    "/admin/api/v1/agent_docs": { get: {} },
    "/admin/api/v1/agent_docs/{slug}": { get: {} },
    "/w/{slug}/api/v1/tasks": { get: {}, post: {} },
  },
};

const summaries: AgentDocSummary[] = [
  {
    slug: "manager-playbook",
    title: "Manager playbook",
    summary: "Default guidance for manager-facing turns.",
    roles: ["manager", "admin"],
    updated_at: "2026-04-01T12:00:00Z",
  },
];

const detail: AgentDoc = {
  ...summaries[0]!,
  body_md: "# Manager playbook\n\nUse workspace context.",
  capabilities: ["kb.search", "tasks.read"],
  version: 3,
  is_customised: false,
  default_hash: "abc123",
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
});

describe("<AgentDocsPage>", () => {
  it("fetches the doc list first and fetches detail only after row selection", async () => {
    const fetcher = installFetch({
      "/admin/api/v1/agent_docs": [{ body: summaries }],
      "/admin/api/v1/agent_docs/manager-playbook": [{ body: detail }],
      "/api/openapi.json": [{ body: openApiSpec }],
    });
    try {
      render(<Harness />);

      expect(await screen.findByText("Manager playbook")).toBeInTheDocument();
      await screen.findByText("/admin/api/v1/agent_docs/{slug}");
      expect(fetcher.calls.map((call) => new URL(call.url, "http://crewday.test").pathname))
        .toEqual(["/admin/api/v1/agent_docs", "/api/openapi.json"]);
      expect(screen.queryByText("# Manager playbook")).not.toBeInTheDocument();

      fireEvent.change(screen.getByRole("searchbox", { name: "Search OpenAPI endpoints" }), {
        target: { value: "tasks" },
      });
      const openApiPanel = screen.getByRole("heading", { name: "OpenAPI" }).closest("section");
      if (!(openApiPanel instanceof HTMLElement)) throw new Error("OpenAPI panel missing");
      expect(within(openApiPanel).getByText("/w/{slug}/api/v1/tasks")).toBeInTheDocument();
      expect(
        within(openApiPanel).queryByText("/admin/api/v1/agent_docs/{slug}"),
      ).not.toBeInTheDocument();

      fireEvent.click(screen.getByText("manager-playbook"));

      await waitFor(() => {
        expect(fetcher.calls.map((call) => new URL(call.url, "http://crewday.test").pathname))
          .toEqual([
            "/admin/api/v1/agent_docs",
            "/api/openapi.json",
            "/admin/api/v1/agent_docs/manager-playbook",
          ]);
      });
      expect(await screen.findByText("# Manager playbook", { exact: false })).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });
});
