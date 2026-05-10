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
  delayMs?: number;
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
    if (next.delayMs) {
      await new Promise((resolve) => setTimeout(resolve, next.delayMs));
    }
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
      <MemoryRouter initialEntries={["/admin/llm/graph"]}>
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

function chatManagerRung(): HTMLElement {
  const capability = screen
    .getAllByText("chat.manager")
    .find((el) => el.classList.contains("llm-graph-node__name"))
    ?.closest("article");
  if (!(capability instanceof HTMLElement)) {
    throw new Error("chat.manager capability not found");
  }
  const rung = within(capability).getByText("google/gemma-4-31b-it").closest(".llm-graph-chain__rung");
  if (!(rung instanceof HTMLElement)) throw new Error("assignment rung not found");
  return rung;
}

function modelButton(name: string): HTMLElement {
  return screen.getByRole("button", { name: new RegExp(`${name} model`) });
}

async function findOpenRouterProvider(): Promise<HTMLElement> {
  return screen.findByRole("button", { name: /^OpenRouter provider,/ });
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

      expect(await findOpenRouterProvider()).toBeInTheDocument();
      expect(screen.getByText("LLM graph")).toBeInTheDocument();
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
      await findOpenRouterProvider();

      openOverflowItem("Sync pricing");

      expect(await screen.findByText("Pricing sync:")).toBeInTheDocument();
      expect(screen.getByText(/1 updated/)).toBeInTheDocument();
      expect(fetcher.calls.some((call) => call.url === "/admin/api/v1/llm/sync-pricing")).toBe(true);
    } finally {
      fetcher.restore();
    }
  });

  it("creates, edits, and handles delete conflicts for providers in the sheet dialog", async () => {
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/graph": [
        { body: graph },
        { body: graph },
        { body: graph },
        { body: graph },
        { body: graph },
      ],
      "/admin/api/v1/llm/providers": [
        {
          body: {
            ...graph.providers[0],
            id: "prov_fake",
            name: "Fake provider",
          },
        },
      ],
      "/admin/api/v1/llm/providers/prov_openrouter": [
        { body: { ...graph.providers[0], name: "OpenRouter EU" } },
        { status: 409, body: { error: "provider_in_use" } },
        { status: 204, body: null },
      ],
    });
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      fireEvent.click(screen.getAllByRole("button", { name: "+ New provider" })[0]!);
      fireEvent.change(screen.getByLabelText(/Name/), {
        target: { value: "Fake provider" },
      });
      fireEvent.change(screen.getByLabelText(/Type/), { target: { value: "fake" } });
      fireEvent.click(screen.getByRole("button", { name: "Create provider" }));

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url === "/admin/api/v1/llm/providers" &&
              call.init.method === "POST",
          ),
        ).toBe(true);
      });
      const post = fetcher.calls.find(
        (call) =>
          call.url === "/admin/api/v1/llm/providers" &&
          call.init.method === "POST",
      );
      expect(jsonBody(post!)).toMatchObject({
        name: "Fake provider",
        provider_type: "fake",
      });

      fireEvent.click(await findOpenRouterProvider());
      fireEvent.change(screen.getByLabelText(/Name/), {
        target: { value: "OpenRouter EU" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save provider" }));

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url === "/admin/api/v1/llm/providers/prov_openrouter" &&
              call.init.method === "PUT",
          ),
        ).toBe(true);
      });
      const put = fetcher.calls.find(
        (call) =>
          call.url === "/admin/api/v1/llm/providers/prov_openrouter" &&
          call.init.method === "PUT",
      );
      expect(jsonBody(put!)).toMatchObject({ name: "OpenRouter EU" });

      fireEvent.click(await findOpenRouterProvider());
      fireEvent.click(screen.getByRole("button", { name: "Delete provider" }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "attached to provider-model rows",
      );
      fireEvent.click(screen.getByRole("button", { name: "Delete provider" }));

      await waitFor(() => {
        expect(
          fetcher.calls.filter(
            (call) =>
              call.url === "/admin/api/v1/llm/providers/prov_openrouter" &&
              call.init.method === "DELETE",
          ),
        ).toHaveLength(2);
      });
    } finally {
      fetcher.restore();
    }
  });

  it("creates, edits, and handles delete conflicts for canonical models in the sheet dialog", async () => {
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/graph": [
        { body: graph },
        { body: graph },
        { body: graph },
        { body: graph },
        { body: graph },
      ],
      "/admin/api/v1/llm/models": [
        {
          body: {
            ...graph.models[0],
            id: "model_new",
            canonical_name: "test/new",
            display_name: "New model",
          },
        },
      ],
      "/admin/api/v1/llm/models/model_gemma": [
        { body: { ...graph.models[0], display_name: "Gemma admin" } },
        { status: 409, body: { error: "model_in_use" } },
        { status: 204, body: null },
      ],
    });
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      fireEvent.click(screen.getByRole("button", { name: "+ New model" }));
      fireEvent.change(screen.getByLabelText(/Canonical name/), {
        target: { value: "test/new" },
      });
      fireEvent.change(screen.getByLabelText(/Display name/), {
        target: { value: "New model" },
      });
      fireEvent.change(screen.getByLabelText(/Vendor/), {
        target: { value: "test" },
      });
      fireEvent.click(screen.getByLabelText("chat"));
      fireEvent.click(screen.getByRole("button", { name: "Create model" }));

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url === "/admin/api/v1/llm/models" &&
              call.init.method === "POST",
          ),
        ).toBe(true);
      });
      const post = fetcher.calls.find(
        (call) =>
          call.url === "/admin/api/v1/llm/models" && call.init.method === "POST",
      );
      expect(jsonBody(post!)).toMatchObject({
        canonical_name: "test/new",
        display_name: "New model",
        capabilities: ["chat"],
      });

      fireEvent.click(modelButton("Gemma 4 31B IT"));
      fireEvent.change(screen.getByLabelText(/Display name/), {
        target: { value: "Gemma admin" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save model" }));

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url === "/admin/api/v1/llm/models/model_gemma" &&
              call.init.method === "PUT",
          ),
        ).toBe(true);
      });
      const put = fetcher.calls.find(
        (call) =>
          call.url === "/admin/api/v1/llm/models/model_gemma" &&
          call.init.method === "PUT",
      );
      expect(jsonBody(put!)).toMatchObject({ display_name: "Gemma admin" });

      fireEvent.click(modelButton("Gemma 4 31B IT"));
      fireEvent.click(screen.getByRole("button", { name: "Delete model" }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "attached to provider-model rows",
      );
      fireEvent.click(screen.getByRole("button", { name: "Delete model" }));

      await waitFor(() => {
        expect(
          fetcher.calls.filter(
            (call) =>
              call.url === "/admin/api/v1/llm/models/model_gemma" &&
              call.init.method === "DELETE",
          ),
        ).toHaveLength(2);
      });
    } finally {
      fetcher.restore();
    }
  });

  it("creates, edits, and handles delete conflicts for provider-model joins in the sheet dialog", async () => {
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/graph": [
        { body: graph },
        { body: graph },
        { body: graph },
        { body: graph },
        { body: graph },
      ],
      "/admin/api/v1/llm/provider-models": [
        {
          body: {
            ...graph.provider_models[0],
            id: "pm_new",
            api_model_id: "test/new-wire",
          },
        },
      ],
      "/admin/api/v1/llm/provider-models/pm_gemma": [
        { body: { ...graph.provider_models[0], api_model_id: "google/gemma-admin" } },
        { status: 409, body: { error: "provider_model_in_use" } },
        { status: 204, delayMs: 200, body: null },
      ],
    });
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      fireEvent.click(
        screen.getByRole("button", { name: "+ New provider-model" }),
      );
      fireEvent.change(screen.getByLabelText(/API model id/), {
        target: { value: "test/new-wire" },
      });
      fireEvent.change(screen.getByLabelText(/Input cost per 1M/), {
        target: { value: "0.25" },
      });
      fireEvent.change(screen.getByLabelText(/Output cost per 1M/), {
        target: { value: "0.75" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Create provider-model" }));

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url === "/admin/api/v1/llm/provider-models" &&
              call.init.method === "POST",
          ),
        ).toBe(true);
      });
      const post = fetcher.calls.find(
        (call) =>
          call.url === "/admin/api/v1/llm/provider-models" &&
          call.init.method === "POST",
      );
      expect(jsonBody(post!)).toMatchObject({
        provider_id: "prov_openrouter",
        model_id: "model_gemma",
        api_model_id: "test/new-wire",
        input_cost_per_million: 0.25,
        output_cost_per_million: 0.75,
      });

      fireEvent.click(
        screen.getByRole("button", {
          name: /OpenRouter provider model for Gemma 4 31B IT/,
        }),
      );
      fireEvent.change(screen.getByLabelText(/API model id/), {
        target: { value: "google/gemma-admin" },
      });
      fireEvent.change(screen.getByLabelText(/Fixed cost per call/), {
        target: { value: "0.05" },
      });
      fireEvent.change(screen.getByLabelText(/Max tokens override/), {
        target: { value: "1024" },
      });
      fireEvent.change(screen.getByLabelText(/Temperature override/), {
        target: { value: "0.7" },
      });
      fireEvent.change(screen.getByLabelText(/Reasoning effort/), {
        target: { value: "high" },
      });
      fireEvent.change(screen.getByLabelText(/Price source override/), {
        target: { value: "openrouter" },
      });
      fireEvent.change(screen.getByLabelText(/Price source model override/), {
        target: { value: "openrouter/google-gemma" },
      });
      fireEvent.change(screen.getByLabelText(/Extra API params/), {
        target: { value: '{"top_p":0.9}' },
      });
      fireEvent.click(screen.getByLabelText("System prompt"));
      fireEvent.click(screen.getByLabelText("Temperature"));
      fireEvent.click(screen.getByLabelText("Enabled"));
      fireEvent.click(screen.getByRole("button", { name: "Save provider-model" }));

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
              call.init.method === "PUT",
          ),
        ).toBe(true);
      });
      const put = fetcher.calls.find(
        (call) =>
          call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
          call.init.method === "PUT",
      );
      expect(jsonBody(put!)).toMatchObject({
        api_model_id: "google/gemma-admin",
        fixed_cost_per_call_usd: 0.05,
        max_tokens_override: 1024,
        temperature_override: 0.7,
        supports_system_prompt: false,
        supports_temperature: false,
        reasoning_effort: "high",
        extra_api_params: { top_p: 0.9 },
        price_source_override: "openrouter",
        price_source_model_id_override: "openrouter/google-gemma",
        is_enabled: false,
      });

      fireEvent.click(
        screen.getByRole("button", {
          name: /OpenRouter provider model for Gemma 4 31B IT/,
        }),
      );
      fireEvent.click(screen.getByRole("button", { name: "Delete provider-model" }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "assigned to one or more capabilities",
      );
      fireEvent.click(screen.getByRole("button", { name: "Delete provider-model" }));
      expect(await screen.findByRole("button", { name: "Deleting…" })).toBeDisabled();

      await waitFor(() => {
        expect(
          fetcher.calls.filter(
            (call) =>
              call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
              call.init.method === "DELETE",
          ),
        ).toHaveLength(2);
      });
    } finally {
      fetcher.restore();
    }
  });

  it("blocks invalid provider-model numeric fields before sending mutations", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      fireEvent.click(
        screen.getByRole("button", {
          name: /OpenRouter provider model for Gemma 4 31B IT/,
        }),
      );
      fireEvent.change(screen.getByLabelText(/Fixed cost per call/), {
        target: { value: "-1" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save provider-model" }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Fixed cost must be zero or more.",
      );

      fireEvent.change(screen.getByLabelText(/Fixed cost per call/), {
        target: { value: "" },
      });
      fireEvent.change(screen.getByLabelText(/Temperature override/), {
        target: { value: "3" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save provider-model" }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Temperature override must be between 0 and 2.",
      );

      expect(
        fetcher.calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
            call.init.method === "PUT",
        ),
      ).toBe(false);
    } finally {
      fetcher.restore();
    }
  });

  it("shows provider save pending state while the create mutation is in flight", async () => {
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/graph": [{ body: graph }, { body: graph }],
      "/admin/api/v1/llm/providers": [
        {
          delayMs: 200,
          body: {
            ...graph.providers[0],
            id: "prov_slow",
            name: "Slow provider",
          },
        },
      ],
    });
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      fireEvent.click(screen.getAllByRole("button", { name: "+ New provider" })[0]!);
      fireEvent.change(screen.getByLabelText(/Name/), {
        target: { value: "Slow provider" },
      });
      fireEvent.change(screen.getByLabelText(/Type/), { target: { value: "fake" } });
      fireEvent.click(screen.getByRole("button", { name: "Create provider" }));

      expect(await screen.findByRole("button", { name: "Saving…" })).toBeDisabled();
      await waitFor(() => {
        expect(screen.queryByRole("dialog", { name: "Create provider" })).toBeNull();
      });
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
      await findOpenRouterProvider();

      fireEvent.click(chatManagerRung());

      const modelCard = screen.getByText("Fast Chat").closest("article");
      if (!(modelCard instanceof HTMLElement)) throw new Error("model card not found");
      fireEvent.click(modelButton("Fast Chat"));

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
      await findOpenRouterProvider();

      fireEvent.click(chatManagerRung());

      const modelCard = screen.getByText("Text Only").closest("article");
      if (!(modelCard instanceof HTMLElement)) throw new Error("model card not found");
      fireEvent.click(modelButton("Text Only"));

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

  it("renders graph rollup cost totals on cards and subcards", async () => {
    const costGraph = {
      ...graph,
      providers: [{ ...graph.providers[0], spend_usd_30d: 2.5, calls_30d: 31 }],
      models: [
        { ...graph.models[0], spend_usd_30d: 1.75, calls_30d: 21 },
        ...graph.models.slice(1),
      ],
      provider_models: [
        { ...graph.provider_models[0], spend_usd_30d: 1.5, calls_30d: 19 },
        ...graph.provider_models.slice(1),
      ],
      capabilities: [
        graph.capabilities[0],
        {
          ...graph.capabilities[1],
          spend_usd_30d: 3.25,
          calls_30d: 41,
          direct_calls_30d: 30,
          inherited_calls_30d: 11,
        },
        {
          ...graph.capabilities[2],
          spend_usd_30d: 0.75,
          calls_30d: 5,
        },
      ],
      assignments: [
        graph.assignments[0],
        {
          ...graph.assignments[1],
          spend_usd_30d: 4.5,
          calls_30d: 52,
          direct_calls_30d: 40,
          inherited_calls_30d: 12,
        },
      ],
    };
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/graph": [
        { body: costGraph },
        { body: costGraph },
        { body: costGraph },
      ],
    });
    try {
      render(<Harness />);

      expect(await screen.findByLabelText("30d 31 calls")).toHaveTextContent("$2.50");
      expect(screen.getByLabelText("30d 21 calls")).toHaveTextContent("$1.75");
      expect(screen.getByLabelText("pm 30d 19 calls")).toHaveTextContent("$1.50");
      expect(screen.getByLabelText("30d 41 calls")).toHaveTextContent("$3.25");
      expect(screen.getByLabelText("30d 52 calls")).toHaveTextContent("$4.50");
      expect(screen.getByText("direct 40 · inherited 12")).toBeInTheDocument();
      expect(screen.getByLabelText("child 30d 5 calls")).toHaveTextContent("$0.75");
    } finally {
      fetcher.restore();
    }
  });

  it("emphasizes hovered graph paths and preserves clicked selection", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      const gemmaProviderModel = screen.getByRole("button", {
        name: /OpenRouter provider model for Gemma 4 31B IT/,
      });
      const gemmaCard = screen.getByText("Gemma 4 31B IT").closest("article");
      const textOnlyCard = screen.getByText("Text Only").closest("article");
      if (!(gemmaCard instanceof HTMLElement) || !(textOnlyCard instanceof HTMLElement)) {
        throw new Error("model cards not found");
      }

      fireEvent.mouseEnter(gemmaProviderModel);

      expect(gemmaProviderModel).toHaveClass("is-active");
      expect(gemmaCard).toHaveClass("is-linked");
      expect(chatManagerRung()).toHaveClass("is-linked");
      expect(textOnlyCard).toHaveClass("is-dim");

      fireEvent.mouseLeave(gemmaProviderModel);
      fireEvent.click(chatManagerRung());

      expect(chatManagerRung()).toHaveClass("is-active");
      expect(gemmaCard).toHaveClass("is-linked");
      expect(textOnlyCard).toHaveClass("is-dim");
    } finally {
      fetcher.restore();
    }
  });

  it("changes and removes explicit capability inheritance from the assignment card", async () => {
    const explicitGraph = {
      ...graph,
      inheritance: [
        {
          capability: "voice.transcribe",
          inherits_from: "chat.manager",
          source: "explicit",
        },
      ],
    };
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/graph": [
        { body: graph },
        { body: explicitGraph },
        { body: graph },
      ],
      "/admin/api/v1/llm/inheritance": [
        {
          body: {
            capability: "voice.transcribe",
            inherits_from: "chat.manager",
            source: "explicit",
          },
        },
      ],
      "/admin/api/v1/llm/inheritance/voice.transcribe": [{ status: 204, body: null }],
    });
    try {
      render(<Harness />);
      await findOpenRouterProvider();
      expect(screen.getByText("invalid implicit")).toBeInTheDocument();

      fireEvent.change(
        screen.getByLabelText("Change voice.transcribe inheritance parent"),
        { target: { value: "chat.manager" } },
      );

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url === "/admin/api/v1/llm/inheritance" &&
              call.init.method === "POST",
          ),
        ).toBe(true);
      });
      const post = fetcher.calls.find(
        (call) =>
          call.url === "/admin/api/v1/llm/inheritance" &&
          call.init.method === "POST",
      );
      expect(jsonBody(post!)).toEqual({
        capability: "voice.transcribe",
        inherits_from: "chat.manager",
      });
      expect(await screen.findByText("invalid explicit")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Remove" }));

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url === "/admin/api/v1/llm/inheritance/voice.transcribe" &&
              call.init.method === "DELETE",
          ),
        ).toBe(true);
      });
      expect(await screen.findByText("invalid implicit")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });
});
