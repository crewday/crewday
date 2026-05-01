import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import LlmPage from "./LlmPage";
import { calls, graph, prompts } from "./LlmPage.testData";

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
      <MemoryRouter initialEntries={["/admin/llm"]}>
        <LlmPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function installPageFetch(extra: Record<string, FakeResponse[]> = {}) {
  return installFetch({
    "/admin/api/v1/llm/graph": [{ body: graph }, { body: graph }, { body: graph }],
    "/admin/api/v1/llm/calls": [{ body: calls }, { body: calls }, { body: calls }],
    "/admin/api/v1/llm/prompts": [{ body: prompts }, { body: prompts }, { body: prompts }],
    ...extra,
  });
}

function openOverflowItem(label: string): void {
  fireEvent.click(screen.getByRole("button", { name: "More actions" }));
  fireEvent.click(screen.getByRole("menuitem", { name: label }));
}

function jsonBody(call: FetchCall): unknown {
  return JSON.parse(String(call.init.body));
}

beforeEach(() => {
  class TestResizeObserver {
    observe(): void {}
    disconnect(): void {}
  }
  (globalThis as { ResizeObserver: unknown }).ResizeObserver = TestResizeObserver;
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function close() {
    this.removeAttribute("open");
  };
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
});

describe("Admin LlmPage", () => {
  it("renders graph columns, pricing, recent calls, and the prompt drawer", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("OpenRouter")).toBeInTheDocument();
      expect(screen.getByText("LLM & agents")).toBeInTheDocument();
      expect(screen.getByText("Gemma 4 31B IT")).toBeInTheDocument();
      expect(screen.getAllByText("voice.transcribe").length).toBeGreaterThan(0);
      expect(screen.getByText("Provider-model pricing")).toBeInTheDocument();
      expect(screen.getByText("Recent calls")).toBeInTheDocument();

      openOverflowItem("Prompts");
      const drawer = await screen.findByText("Prompt library");
      expect(drawer).toBeInTheDocument();
      expect(screen.getByText("Manager chat")).toBeInTheDocument();
      expect(screen.getByText("You are the manager assistant.")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("syncs pricing and surfaces the server result", async () => {
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/sync-pricing": [
        {
          body: {
            started_at: "2026-04-30T12:01:00Z",
            deltas: [],
            updated: 1,
            skipped: 2,
            errors: 0,
          },
        },
      ],
    });
    try {
      render(<Harness />);
      await screen.findByText("OpenRouter");

      openOverflowItem("Sync pricing");

      expect(await screen.findByText("Pricing sync:")).toBeInTheDocument();
      expect(screen.getByText(/1 updated/)).toBeInTheDocument();
      expect(fetcher.calls.some((call) => call.url === "/admin/api/v1/llm/sync-pricing")).toBe(true);
    } finally {
      fetcher.restore();
    }
  });

  it("writes selected assignment to a clicked model through the assignment API", async () => {
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/assignments/assign_chat_manager": [
        {
          body: {
            ...graph.assignments[0],
            provider_model_id: "pm_fast",
          },
        },
      ],
    });
    try {
      render(<Harness />);
      await screen.findByText("OpenRouter");

      const assignmentText = screen.getAllByText("google/gemma-4-31b-it").find((el) =>
        el.closest(".llm-graph-chain__rung"),
      );
      if (!assignmentText) throw new Error("assignment rung not found");
      fireEvent.click(assignmentText.closest(".llm-graph-chain__rung")!);

      const modelCard = screen.getByText("Fast Chat").closest("article");
      if (!(modelCard instanceof HTMLElement)) throw new Error("model card not found");
      fireEvent.click(modelCard);

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url === "/admin/api/v1/llm/assignments/assign_chat_manager" &&
              call.init.method === "PUT",
          ),
        ).toBe(true);
      });
      const put = fetcher.calls.find(
        (call) =>
          call.url === "/admin/api/v1/llm/assignments/assign_chat_manager" &&
          call.init.method === "PUT",
      );
      expect(put).toBeDefined();
      expect(jsonBody(put!)).toEqual({ provider_model_id: "pm_fast" });
      expect(within(modelCard).getByText("test/fast-chat")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("selects an incompatible model normally instead of sending a rejected assignment update", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);
      await screen.findByText("OpenRouter");

      const assignmentText = screen.getAllByText("google/gemma-4-31b-it").find((el) =>
        el.closest(".llm-graph-chain__rung"),
      );
      if (!assignmentText) throw new Error("assignment rung not found");
      fireEvent.click(assignmentText.closest(".llm-graph-chain__rung")!);

      const modelCard = screen.getByText("Text Only").closest("article");
      if (!(modelCard instanceof HTMLElement)) throw new Error("model card not found");
      fireEvent.click(modelCard);

      expect(
        fetcher.calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/assignments/assign_chat_manager" &&
            call.init.method === "PUT",
        ),
      ).toBe(false);
      expect(modelCard).toHaveClass("is-active");
    } finally {
      fetcher.restore();
    }
  });
});
