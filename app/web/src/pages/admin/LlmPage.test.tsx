import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import adminLlmCss from "@/styles/admin-llm.css?raw";
import LlmPage from "./LlmPage";
import LlmUsagePage from "./LlmUsagePage";
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

function Harness({ page = "graph" }: { page?: "graph" | "usage" }): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const element = page === "usage" ? <LlmUsagePage /> : <LlmPage />;
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/admin/llm/${page}`]}>
        {element}
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

function providerModelRow(dialog: HTMLElement, name: string): HTMLElement {
  const row = within(dialog).getByText(name).closest(".llm-assignment-provider-model");
  if (!(row instanceof HTMLElement)) throw new Error(`${name} row not found`);
  return row;
}

function chatManagerRung(): HTMLElement {
  const capability = screen
    .getAllByText("chat.manager")
    .find((el) => el.classList.contains("llm-graph-node__name"))
    ?.closest("article");
  if (!(capability instanceof HTMLElement)) {
    throw new Error("chat.manager capability not found");
  }
  const rung = within(capability)
    .getByRole("button", { name: /chat\.manager assignment rung 0/ })
    .closest(".llm-graph-chain__rung");
  if (!(rung instanceof HTMLElement)) throw new Error("assignment rung not found");
  return rung;
}

function chatManagerAssignmentButton(): HTMLElement {
  return within(chatManagerRung()).getByRole("button", {
    name: /chat\.manager assignment rung 0/,
  });
}

function defaultCapabilityCard(): HTMLElement {
  const capability = screen
    .getAllByText("default")
    .find((el) => el.classList.contains("llm-graph-node__name"))
    ?.closest("article");
  if (!(capability instanceof HTMLElement)) {
    throw new Error("default capability not found");
  }
  return capability;
}

function assignmentDialog(name: string): HTMLElement {
  return screen.getByRole("dialog", { name });
}

function graphBoard(): HTMLElement {
  const board = document.querySelector(".llm-graph");
  if (!(board instanceof HTMLElement)) throw new Error("LLM graph not found");
  return board;
}

function expectGraphUnmuted(): void {
  expect(graphBoard().querySelector(".is-dim")).not.toBeInTheDocument();
}

function rect(left: number, top: number, width: number, height: number): DOMRect {
  return {
    x: left,
    y: top,
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    toJSON: () => ({}),
  } as DOMRect;
}

function pathEndpoint(d: string): { x: number; y: number } {
  const values = Array.from(d.matchAll(/-?\d+(?:\.\d+)?/g), (match) =>
    Number(match[0]),
  );
  if (values.length < 2) throw new Error(`Path has no endpoint: ${d}`);
  return { x: values[values.length - 2]!, y: values[values.length - 1]! };
}

function pathStart(d: string): { x: number; y: number } {
  const values = Array.from(d.matchAll(/-?\d+(?:\.\d+)?/g), (match) =>
    Number(match[0]),
  );
  if (values.length < 2) throw new Error(`Path has no start: ${d}`);
  return { x: values[0]!, y: values[1]! };
}

function expectSharedFormModal(
  dialog: HTMLElement,
  options: { wide?: boolean; section?: boolean; footer?: boolean } = {},
): void {
  expect(dialog).toHaveClass("modal", "modal--sheet", "form-modal-dialog");
  if (options.wide) expect(dialog).toHaveClass("form-modal-dialog--wide");
  expect(dialog).not.toHaveClass("llm-registry-dialog", "llm-assignment-dialog");
  expect(dialog.querySelector(".llm-registry-form__close")).not.toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "Close" })).toHaveClass(
    "form-modal__close",
  );
  expect(
    dialog.querySelector(options.section ? "section.form-modal" : "form.form-modal"),
  ).toBeInTheDocument();
  if (options.footer === false) {
    expect(dialog.querySelector(".form-modal__footer")).not.toBeInTheDocument();
  } else {
    expect(dialog.querySelector(".form-modal__footer")).toBeInTheDocument();
  }
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
  it("renders graph columns without usage panels and keeps the prompt drawer reachable", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);

      expect(await findOpenRouterProvider()).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "LLM graph" })).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: "Graph" })).not.toBeInTheDocument();
      expect(screen.queryByRole("link", { name: "Usage" })).not.toBeInTheDocument();
      expect(screen.getAllByRole("button", { name: "+ New provider" })).toHaveLength(
        1,
      );
      expect(
        screen.getByRole("button", {
          name: "OpenRouter provider, 12 calls, $1.25 spend",
        }),
      ).toBeInTheDocument();
      expect(screen.getByText("Gemma 4 31B IT")).toBeInTheDocument();
      expect(screen.getAllByText("voice.transcribe").length).toBeGreaterThan(0);
      expect(screen.queryByText("Spend (30d)")).not.toBeInTheDocument();
      expect(screen.queryByText("Provider-model pricing")).not.toBeInTheDocument();
      expect(screen.queryByText("Recent calls")).not.toBeInTheDocument();
      expect(within(graphBoard()).queryByText(/^\d+ models?$/)).not.toBeInTheDocument();
      expect(
        within(graphBoard()).queryByText(/^\d+ providers?$/),
      ).not.toBeInTheDocument();
      expect(fetcher.calls.some((call) => call.url === "/admin/api/v1/llm/calls")).toBe(
        false,
      );

      openOverflowItem("Prompts");
      const drawer = await screen.findByText("Prompt library");
      expect(drawer).toBeInTheDocument();
      expect(screen.getByText("Manager chat")).toBeInTheDocument();
      expect(screen.getByText("You are the manager assistant.")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("renders the usage page support panels together", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness page="usage" />);

      expect(await screen.findByRole("heading", { name: "LLM usage" })).toBeInTheDocument();
      expect(await screen.findByText("Provider-model pricing")).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: "Graph" })).not.toBeInTheDocument();
      expect(screen.queryByRole("link", { name: "Usage" })).not.toBeInTheDocument();
      expect(screen.getByText("Spend (30d)")).toBeInTheDocument();
      expect(screen.getByText("Recent calls")).toBeInTheDocument();
      const recentCalls = screen.getByText("Recent calls").closest(".panel");
      if (!(recentCalls instanceof HTMLElement)) throw new Error("Recent calls panel not found");
      const subCentCost = within(recentCalls).getByText("$0.000400");
      expect(subCentCost).toHaveClass("mono");
      expect(within(recentCalls).getByText("$0.03")).toHaveClass("mono");
      expect(within(recentCalls).queryByText("$0.00")).not.toBeInTheDocument();
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
            deltas: [
              {
                provider_model_id: "pm_gemma",
                api_model_id: "google/gemma-4-31b-it",
                input_before: 0.15,
                input_after: 0.12,
                output_before: 0.2,
                output_after: 0.18,
                status: "updated",
              },
            ],
            updated: 1,
            skipped: 2,
            errors: 0,
          },
        },
      ],
    });
    try {
      render(<Harness page="usage" />);
      expect(await screen.findByText("Provider-model pricing")).toBeInTheDocument();

      const pricingPanel = screen.getByText("Provider-model pricing").closest(".panel");
      if (!(pricingPanel instanceof HTMLElement)) throw new Error("pricing panel not found");
      fireEvent.click(within(pricingPanel).getByRole("button", { name: "Sync pricing" }));

      expect(
        await within(pricingPanel).findByText(/Last result: 1 updated, 2 skipped, 0 errors/),
      ).toBeInTheDocument();
      expect(
        within(pricingPanel).getByLabelText("Pricing sync deltas"),
      ).toHaveTextContent("google/gemma-4-31b-it");
      expect(fetcher.calls.some((call) => call.url === "/admin/api/v1/llm/sync-pricing")).toBe(true);
      await waitFor(() => {
        expect(
          fetcher.calls.filter((call) => call.url === "/admin/api/v1/llm/graph"),
        ).toHaveLength(2);
        expect(
          fetcher.calls.filter((call) => call.url === "/admin/api/v1/llm/calls"),
        ).toHaveLength(2);
      });
    } finally {
      fetcher.restore();
    }
  });

  it("edits, reloads revisions, and resets prompt templates from the prompt dialog", async () => {
    const initialDetail = {
      ...prompts[0],
      template: "You are the manager assistant.",
      notes: "Current production prompt.",
    };
    const savedDetail = {
      ...initialDetail,
      version: 4,
      is_customised: true,
      template: "You are the manager assistant.\nKeep answers terse.",
      notes: "Terse mode.",
      revisions_count: 3,
    };
    const resetDetail = {
      ...initialDetail,
      version: 5,
      is_customised: false,
      revisions_count: 4,
      notes: null,
    };
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/prompts": [
        { body: prompts },
        { body: [{ ...prompts[0], version: 4, is_customised: true, revisions_count: 3 }] },
        { body: [{ ...prompts[0], version: 5, is_customised: false, revisions_count: 4 }] },
        { body: prompts },
      ],
      "/admin/api/v1/llm/prompts/prompt_chat_manager": [
        { body: initialDetail },
        { body: savedDetail },
        { body: savedDetail },
        { body: resetDetail },
      ],
      "/admin/api/v1/llm/prompts/prompt_chat_manager/reset-to-default": [
        { body: resetDetail },
      ],
      "/admin/api/v1/llm/prompts/prompt_chat_manager/revisions": [
        {
          body: [
            {
              id: "rev_2",
              template_id: "prompt_chat_manager",
              version: 2,
              body: "Previous manager assistant prompt.",
              notes: "Before edit.",
              created_at: "2026-04-29T12:00:00Z",
              created_by_user_id: "user_admin",
            },
          ],
        },
        {
          body: [
            {
              id: "rev_3",
              template_id: "prompt_chat_manager",
              version: 3,
              body: "You are the manager assistant.",
              notes: "Current production prompt.",
              created_at: "2026-04-30T12:00:00Z",
              created_by_user_id: "user_admin",
            },
          ],
        },
        {
          body: [
            {
              id: "rev_4",
              template_id: "prompt_chat_manager",
              version: 4,
              body: "You are the manager assistant.\nKeep answers terse.",
              notes: "Terse mode.",
              created_at: "2026-04-30T12:05:00Z",
              created_by_user_id: "user_admin",
            },
          ],
        },
      ],
    });
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      openOverflowItem("Prompts");
      const drawer = await screen.findByText("Prompt library");
      const promptDrawer = drawer.closest(".llm-prompt-drawer");
      if (!(promptDrawer instanceof HTMLElement)) throw new Error("prompt drawer not found");
      fireEvent.click(within(promptDrawer).getByRole("button", { name: /Manager chat/ }));

      const dialog = await screen.findByRole("dialog", { name: "Manager chat" });
      expectSharedFormModal(dialog, { wide: true });
      expect(await within(dialog).findByLabelText(/Active template body/)).toHaveValue(
        "You are the manager assistant.",
      );
      expect(within(dialog).getByText("Revision history")).toBeInTheDocument();
      expect(await within(dialog).findByText("Previous manager assistant prompt.")).toBeInTheDocument();

      fireEvent.change(within(dialog).getByLabelText(/Active template body/), {
        target: { value: "You are the manager assistant.\nKeep answers terse." },
      });
      fireEvent.change(within(dialog).getByLabelText(/Notes/), {
        target: { value: "Terse mode." },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Save prompt" }));

      await waitFor(() => {
        const saveCall = fetcher.calls.find(
          (call) =>
            call.url === "/admin/api/v1/llm/prompts/prompt_chat_manager" &&
            call.init.method === "PUT",
        );
        expect(saveCall).toBeDefined();
        expect(jsonBody(saveCall!)).toEqual({
          template: "You are the manager assistant.\nKeep answers terse.",
          notes: "Terse mode.",
        });
      });

      expect(await within(dialog).findByText("v4")).toBeInTheDocument();
      fireEvent.click(within(dialog).getByRole("button", { name: "Reset to default" }));

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url ===
                "/admin/api/v1/llm/prompts/prompt_chat_manager/reset-to-default" &&
              call.init.method === "POST",
          ),
        ).toBe(true);
      });
      expect(await within(dialog).findByText("v5")).toBeInTheDocument();
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

      fireEvent.click(screen.getByRole("button", { name: "+ New provider" }));
      const createDialog = screen.getByRole("dialog", { name: "Create provider" });
      expectSharedFormModal(createDialog);
      expect(within(createDialog).queryByLabelText(/Priority/)).not.toBeInTheDocument();
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
      expect(jsonBody(post!)).not.toHaveProperty("priority");

      fireEvent.click(await findOpenRouterProvider());
      expect(
        within(screen.getByRole("dialog", { name: "OpenRouter" })).queryByLabelText(
          /Priority/,
        ),
      ).not.toBeInTheDocument();
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
      expect(jsonBody(put!)).not.toHaveProperty("priority");

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
      expectSharedFormModal(screen.getByRole("dialog", { name: "Create model" }));
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
      expectSharedFormModal(screen.getByRole("dialog", { name: "Create provider-model" }));
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
      fireEvent.change(screen.getByLabelText(/^Thinking/), {
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
        thinking_level_override: "high",
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

  it("opens separate provider-model and assignment editors from assignment rungs", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      const rung = chatManagerRung();
      const providerModelButton = within(rung).getByRole("button", {
        name: /Open provider-model google\/gemma-4-31b-it for chat\.manager assignment/,
      });
      fireEvent.click(providerModelButton);

      const providerModelDialog = await screen.findByRole("dialog", {
        name: "google/gemma-4-31b-it",
      });
      expectSharedFormModal(providerModelDialog);
      expect(
        within(providerModelDialog).getByRole("button", { name: "Save provider-model" }),
      ).toBeInTheDocument();
      expect(providerModelButton).toHaveClass("is-active");
      expect(chatManagerRung()).toHaveClass("is-linked");
      expect(screen.queryByRole("dialog", { name: "chat.manager" })).toBeNull();

      fireEvent.click(within(providerModelDialog).getByRole("button", { name: "Close" }));
      await waitFor(() => {
        expect(screen.queryByRole("dialog", { name: "google/gemma-4-31b-it" })).toBeNull();
      });

      fireEvent.click(chatManagerAssignmentButton());
      const assignment = assignmentDialog("chat.manager");
      expectSharedFormModal(assignment, { wide: true, section: true, footer: false });
      expect(within(assignment).getByText("Available provider-models")).toBeInTheDocument();
      expect(within(assignment).getByText("Selected chain")).toBeInTheDocument();
      expect(chatManagerRung()).toHaveClass("is-active");
      expect(providerModelButton).not.toHaveClass("is-active");
      expect(screen.queryByRole("dialog", { name: "google/gemma-4-31b-it" })).toBeNull();
    } finally {
      fetcher.restore();
    }
  });

  it("clears assignment-side provider-model hover when the pointer leaves the rung", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      const providerModelButton = within(chatManagerRung()).getByRole("button", {
        name: /Open provider-model google\/gemma-4-31b-it for chat\.manager assignment/,
      });

      fireEvent.mouseOver(providerModelButton);
      expect(providerModelButton).toHaveClass("is-active");
      expect(chatManagerRung()).toHaveClass("is-linked");

      fireEvent.mouseLeave(providerModelButton, { relatedTarget: document.body });
      expect(providerModelButton).not.toHaveClass("is-active");
      expect(chatManagerRung()).not.toHaveClass("is-linked");

      fireEvent.mouseOver(providerModelButton);
      fireEvent.mouseLeave(providerModelButton, {
        relatedTarget: chatManagerAssignmentButton(),
      });
      expect(chatManagerRung()).toHaveClass("is-active");
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

      fireEvent.click(screen.getByRole("button", { name: "+ New provider" }));
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

  it("creates assignment rungs from the direct provider-model picker", async () => {
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/graph": [
        { body: graph },
        { body: graph },
        { body: graph },
        { body: graph },
      ],
      "/admin/api/v1/llm/assignments": [
        {
          body: {
            ...graph.assignments[1],
            id: "assign_chat_manager_fallback",
            provider_model_id: "pm_fast",
            priority: 1,
          },
        },
      ],
    });
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      fireEvent.click(chatManagerAssignmentButton());
      const dialog = assignmentDialog("chat.manager");
      expectSharedFormModal(dialog, { wide: true, section: true, footer: false });
      expect(within(dialog).queryByLabelText(/Max tokens/)).not.toBeInTheDocument();
      expect(within(dialog).queryByLabelText(/Temperature/)).not.toBeInTheDocument();
      expect(within(dialog).queryByLabelText(/Extra API params/)).not.toBeInTheDocument();
      expect(
        within(dialog).queryByRole("textbox", { name: /Required capabilities/ }),
      ).not.toBeInTheDocument();
      expect(
        within(providerModelRow(dialog, "Text Only")).getByRole("button", {
          name: /Add Text Only/,
        }),
      ).toBeDisabled();

      fireEvent.click(
        within(providerModelRow(dialog, "Fast Chat")).getByRole("button", {
          name: /Add Fast Chat/,
        }),
      );

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url === "/admin/api/v1/llm/assignments" &&
              call.init.method === "POST",
          ),
        ).toBe(true);
      });
      const post = fetcher.calls.find(
        (call) =>
          call.url === "/admin/api/v1/llm/assignments" &&
          call.init.method === "POST",
      );
      expect(jsonBody(post!)).toMatchObject({
        capability: "chat.manager",
        provider_model_id: "pm_fast",
        priority: 1,
      });
      expect(
        fetcher.calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/assignments/assign_chat_manager" &&
            call.init.method === "PUT",
        ),
      ).toBe(false);
    } finally {
      fetcher.restore();
    }
  });

  it("deletes and reorders assignment rungs from the assignment modal", async () => {
    const twoRungGraph = {
      ...graph,
      assignments: [
        graph.assignments[0],
        graph.assignments[1],
        {
          ...graph.assignments[1],
          id: "assign_chat_manager_fallback",
          provider_model_id: "pm_fast",
          priority: 1,
        },
      ],
    };
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/graph": [
        { body: twoRungGraph },
        { body: twoRungGraph },
        { body: twoRungGraph },
        { body: twoRungGraph },
      ],
      "/admin/api/v1/llm/assignments/reorder": [{ body: twoRungGraph.assignments }],
      "/admin/api/v1/llm/assignments/assign_chat_manager_fallback": [
        { status: 204, body: null },
      ],
    });
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      fireEvent.click(chatManagerAssignmentButton());
      const dialog = assignmentDialog("chat.manager");
      fireEvent.keyDown(providerModelRow(dialog, "Fast Chat"), {
        key: "ArrowUp",
        altKey: true,
      });

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url === "/admin/api/v1/llm/assignments/reorder" &&
              call.init.method === "PATCH",
          ),
        ).toBe(true);
      });
      const reorder = fetcher.calls.find(
        (call) =>
          call.url === "/admin/api/v1/llm/assignments/reorder" &&
          call.init.method === "PATCH",
      );
      expect(jsonBody(reorder!)).toEqual([
        {
          capability: "chat.manager",
          ids_in_priority_order: [
            "assign_chat_manager_fallback",
            "assign_chat_manager",
          ],
        },
      ]);

      fireEvent.click(
        within(providerModelRow(dialog, "Fast Chat")).getByRole("button", {
          name: /Remove Fast Chat/,
        }),
      );
      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url ===
                "/admin/api/v1/llm/assignments/assign_chat_manager_fallback" &&
              call.init.method === "DELETE",
          ),
        ).toBe(true);
      });
    } finally {
      fetcher.restore();
    }
  });

  it("does not stage unsaved local assignment rungs in the modal", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      fireEvent.click(chatManagerAssignmentButton());
      const dialog = assignmentDialog("chat.manager");
      expect(within(dialog).queryByText("New rung")).not.toBeInTheDocument();
      expect(within(dialog).queryByRole("button", { name: /Add rung/ })).toBeNull();
      expect(
        fetcher.calls.some(
          (call) =>
            call.url.startsWith("/admin/api/v1/llm/assignments") &&
            call.init.method !== "GET",
        ),
      ).toBe(false);
    } finally {
      fetcher.restore();
    }
  });

  it("adds compatible provider-models to default through the normal assignment API", async () => {
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/assignments": [
        {
          body: {
            ...graph.assignments[0],
            id: "assign_default_fast",
            provider_model_id: "pm_fast",
            priority: 1,
          },
        },
      ],
    });
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      fireEvent.click(
        within(defaultCapabilityCard()).getByRole("button", {
          name: /default capability/,
        }),
      );

      const dialog = assignmentDialog("default");
      expect(within(dialog).getByText("Selected chain")).toBeInTheDocument();
      expect(within(dialog).getByText("Gemma 4 31B IT")).toBeInTheDocument();
      expect(
        within(providerModelRow(dialog, "Text Only")).getByRole("button", {
          name: /Add Text Only/,
        }),
      ).toBeDisabled();

      fireEvent.click(
        within(providerModelRow(dialog, "Fast Chat")).getByRole("button", {
          name: /Add Fast Chat/,
        }),
      );

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url === "/admin/api/v1/llm/assignments" &&
              call.init.method === "POST",
          ),
        ).toBe(true);
      });
      const post = fetcher.calls.find(
        (call) =>
          call.url === "/admin/api/v1/llm/assignments" &&
          call.init.method === "POST",
      )!;
      expect(jsonBody(post)).toMatchObject({
        capability: "default",
        provider_model_id: "pm_fast",
        priority: 1,
        required_capabilities: ["chat", "function_calling"],
      });
    } finally {
      fetcher.restore();
    }
  });

  it("reorders default assignment rungs through the normal assignment API", async () => {
    const twoRungDefaultGraph = {
      ...graph,
      assignments: [
        graph.assignments[0],
        {
          ...graph.assignments[0],
          id: "assign_default_fast",
          provider_model_id: "pm_fast",
          priority: 1,
        },
        ...graph.assignments.slice(1),
      ],
    };
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/graph": [
        { body: twoRungDefaultGraph },
        { body: twoRungDefaultGraph },
        { body: twoRungDefaultGraph },
      ],
      "/admin/api/v1/llm/assignments/reorder": [
        { body: twoRungDefaultGraph.assignments },
      ],
    });
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      fireEvent.click(
        within(defaultCapabilityCard()).getByRole("button", {
          name: /default capability/,
        }),
      );

      const dialog = assignmentDialog("default");
      fireEvent.keyDown(providerModelRow(dialog, "Fast Chat"), {
        key: "ArrowUp",
        altKey: true,
      });

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url === "/admin/api/v1/llm/assignments/reorder" &&
              call.init.method === "PATCH",
          ),
        ).toBe(true);
      });
      const reorder = fetcher.calls.find(
        (call) =>
          call.url === "/admin/api/v1/llm/assignments/reorder" &&
          call.init.method === "PATCH",
      )!;
      expect(jsonBody(reorder)).toEqual([
        {
          capability: "default",
          ids_in_priority_order: ["assign_default_fast", "assign_default"],
        },
      ]);
    } finally {
      fetcher.restore();
    }
  });

  it("deletes default assignment rungs through the normal assignment API", async () => {
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/assignments/assign_default": [{ status: 204, body: null }],
    });
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      fireEvent.click(
        within(defaultCapabilityCard()).getByRole("button", {
          name: /default capability/,
        }),
      );

      const dialog = assignmentDialog("default");
      fireEvent.click(
        within(providerModelRow(dialog, "Gemma 4 31B IT")).getByRole("button", {
          name: /Remove Gemma 4 31B IT/,
        }),
      );

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url === "/admin/api/v1/llm/assignments/assign_default" &&
              call.init.method === "DELETE",
          ),
        ).toBe(true);
      });
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

      expect(
        await screen.findByLabelText("Recent usage: 31 calls, $2.50 spend"),
      ).toHaveTextContent("$2.50");
      expect(
        screen.getByLabelText("Recent usage: 21 calls, $1.75 spend"),
      ).toHaveTextContent("$1.75");
      expect(
        screen.getByLabelText("Recent usage: 19 calls, $1.50 spend"),
      ).toHaveTextContent("$1.50");
      expect(
        screen.getByLabelText("Recent usage: 41 calls, $3.25 spend"),
      ).toHaveTextContent("$3.25");
      expect(
        screen.getByLabelText("Recent usage: 52 calls, $4.50 spend"),
      ).toHaveTextContent("$4.50");
      expect(
        screen.getByLabelText("Recent usage: 5 calls, $0.75 spend"),
      ).toHaveTextContent("$0.75");
      const board = within(graphBoard());
      expect(board.queryByText(/^30d$/)).not.toBeInTheDocument();
      expect(board.queryByText("pm 30d")).not.toBeInTheDocument();
      expect(board.queryByText("child 30d")).not.toBeInTheDocument();
      expect(board.queryByText("direct 40 · inherited 12")).not.toBeInTheDocument();
      expect(board.queryByLabelText("pm 30d 19 calls")).not.toBeInTheDocument();
      expect(board.queryByLabelText("child 30d 5 calls")).not.toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("keeps graph badges usage-first while preserving disabled visual states", async () => {
    const disabledGraph = {
      ...graph,
      providers: [{ ...graph.providers[0], is_enabled: false }],
      models: [{ ...graph.models[0], is_active: false }, ...graph.models.slice(1)],
      provider_models: [
        { ...graph.provider_models[0], is_enabled: false },
        ...graph.provider_models.slice(1),
      ],
      capabilities: [
        ...graph.capabilities,
        {
          key: "documents.ocr",
          description: "Read receipt images",
          required_capabilities: ["vision"],
          spend_usd_30d: 0,
          calls_30d: 0,
          direct_spend_usd_30d: 0,
          direct_calls_30d: 0,
          inherited_spend_usd_30d: 0,
          inherited_calls_30d: 0,
        },
      ],
    };
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/graph": [
        { body: disabledGraph },
        { body: disabledGraph },
        { body: disabledGraph },
      ],
    });
    try {
      render(<Harness />);

      const provider = await findOpenRouterProvider();
      expect(provider).toHaveClass("is-disabled");
      expect(within(provider).queryByText(/^on$/)).not.toBeInTheDocument();
      expect(within(provider).queryByText(/^off$/)).not.toBeInTheDocument();
      expect(within(provider).getByLabelText("Recent usage: 12 calls, $1.25 spend")).toHaveClass(
        "llm-usage-total",
      );

      const model = screen.getByText("Gemma 4 31B IT").closest("article");
      if (!(model instanceof HTMLElement)) throw new Error("model card not found");
      expect(model).toHaveClass("is-disabled");
      expect(within(model).queryByText("Google")).not.toBeInTheDocument();

      const providerModel = screen.getByRole("button", {
        name: /OpenRouter provider model for Gemma 4 31B IT/,
      });
      expect(providerModel).toHaveClass("is-disabled");

      const board = within(graphBoard());
      expect(board.queryByText(/^1 rung$/)).not.toBeInTheDocument();
      expect(board.queryByText(/^\d+ rungs$/)).not.toBeInTheDocument();
      expect(board.getByText("unassigned")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("renders graph paths unmuted before a selection", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      expectGraphUnmuted();
    } finally {
      fetcher.restore();
    }
  });

  it("emphasizes hovered graph paths without muting unrelated cards", async () => {
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
      expect(textOnlyCard).not.toHaveClass("is-dim");
      expectGraphUnmuted();

      fireEvent.mouseLeave(gemmaProviderModel);
      expectGraphUnmuted();
    } finally {
      fetcher.restore();
    }
  });

  it("mutes only cards outside the clicked selection path", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      const gemmaCard = screen.getByText("Gemma 4 31B IT").closest("article");
      const textOnlyCard = screen.getByText("Text Only").closest("article");
      if (!(gemmaCard instanceof HTMLElement) || !(textOnlyCard instanceof HTMLElement)) {
        throw new Error("model cards not found");
      }

      fireEvent.click(chatManagerAssignmentButton());

      expect(chatManagerRung()).toHaveClass("is-active");
      expect(gemmaCard).toHaveClass("is-linked");
      expect(textOnlyCard).toHaveClass("is-dim");
      await waitFor(() => {
        expect(
          document.querySelector(".llm-graph__edge--assign.is-error"),
        ).toHaveClass("is-dim");
      });
    } finally {
      fetcher.restore();
    }
  });

  it("clears graph selection and muted opacity when empty graph space is clicked", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);

      await findOpenRouterProvider();
      const textOnlyCard = screen.getByText("Text Only").closest("article");
      if (!(textOnlyCard instanceof HTMLElement)) throw new Error("model card not found");

      fireEvent.click(chatManagerAssignmentButton());

      expect(chatManagerRung()).toHaveClass("is-active");
      expect(graphBoard().querySelector(".is-active")).toBeInTheDocument();
      expect(textOnlyCard).toHaveClass("is-dim");

      fireEvent.click(graphBoard());

      expect(chatManagerRung()).not.toHaveClass("is-active");
      expect(graphBoard().querySelector(".is-active")).not.toBeInTheDocument();
      expectGraphUnmuted();
    } finally {
      fetcher.restore();
    }
  });

  it("clears focus hover fallback when empty graph space is clicked", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);

      const provider = await findOpenRouterProvider();

      fireEvent.focus(provider);

      expect(provider).toHaveClass("is-active");
      expect(graphBoard().querySelector(".is-active")).toBeInTheDocument();

      fireEvent.click(graphBoard());

      expect(provider).not.toHaveClass("is-active");
      expect(graphBoard().querySelector(".is-active")).not.toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("does not clear graph selection from graph controls or headers", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);

      const provider = await findOpenRouterProvider();
      fireEvent.click(provider);
      expect(provider).toHaveClass("is-active");

      const providerDialog = screen.getByRole("dialog", { name: "OpenRouter" });
      fireEvent.click(within(providerDialog).getByRole("button", { name: "Close" }));
      await waitFor(() => {
        expect(screen.queryByRole("dialog", { name: "OpenRouter" })).toBeNull();
      });
      expect(provider).not.toHaveClass("is-active");

      fireEvent.click(provider);
      fireEvent.click(screen.getByText("Providers"));
      expect(provider).toHaveClass("is-active");

      fireEvent.click(screen.getByRole("button", { name: "+ New provider" }));
      expect(provider).toHaveClass("is-active");
      expect(screen.getByRole("dialog", { name: "Create provider" })).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("keeps nested graph buttons borderless across focus and active states", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      const gemmaButton = modelButton("Gemma 4 31B IT");
      const gemmaCard = gemmaButton.closest(".llm-graph-node");
      if (!(gemmaCard instanceof HTMLElement)) throw new Error("model card not found");

      fireEvent.focus(gemmaButton);
      expect(gemmaButton).toHaveClass("llm-graph-node__button");
      expect(gemmaCard).toHaveClass("is-active");

      fireEvent.mouseDown(gemmaButton);
      fireEvent.click(gemmaButton);
      expect(gemmaButton).toHaveClass("llm-graph-node__button");
      expect(gemmaButton).not.toHaveClass("is-active", "is-linked", "is-dim");
      expect(gemmaCard).toHaveClass("is-active");

      expect(adminLlmCss).toMatch(
        /\.llm-graph-node:hover,\s*\.llm-graph-node:focus-visible,\s*\.llm-graph-node:focus-within\s*{[\s\S]*border-color: var\(--moss\);/m,
      );
      expect(adminLlmCss).toMatch(
        /\.llm-graph-node__button:focus,\s*\.llm-graph-node__button:focus-visible,\s*\.llm-graph-node__button:active\s*{[\s\S]*border: 0;/m,
      );
      expect(adminLlmCss).not.toMatch(
        /\.llm-graph-node__button:focus-visible\s*{[\s\S]*border-color:/m,
      );
    } finally {
      fetcher.restore();
    }
  });

  it("anchors provider-model graph edges to provider-model subcards", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function getBoundingClientRectMock(this: HTMLElement) {
        const el = this;
        const label = el.getAttribute("aria-label") ?? "";
        if (el.classList.contains("llm-graph")) return rect(0, 0, 900, 600);
        if (label.startsWith("OpenRouter provider,")) return rect(10, 100, 180, 80);
        if (label.startsWith("Gemma 4 31B IT model,")) return rect(360, 60, 180, 100);
        if (label.startsWith("OpenRouter provider model for Gemma 4 31B IT,")) {
          return rect(380, 210, 160, 40);
        }
        if (label.startsWith("OpenRouter provider model for Text Only,")) {
          return rect(380, 320, 160, 40);
        }
        if (label.startsWith("OpenRouter provider model for Fast Chat,")) {
          return rect(380, 430, 160, 40);
        }
        if (
          el.classList.contains("llm-graph-chain__rung") &&
          el.textContent?.includes("google/gemma-4-31b-it")
        ) {
          return rect(710, 220, 160, 44);
        }
        return rect(0, 0, 0, 0);
      },
    );
    const fetcher = installPageFetch();
    try {
      render(<Harness />);
      await findOpenRouterProvider();

      await waitFor(() => {
        const endpoints = Array.from(
          document.querySelectorAll<SVGPathElement>(".llm-graph__edge--pm"),
          (path) => pathEndpoint(path.getAttribute("d") ?? ""),
        );
        expect(endpoints).toContainEqual({ x: 380, y: 230 });
      });
      const pmEndpoints = Array.from(
        document.querySelectorAll<SVGPathElement>(".llm-graph__edge--pm"),
        (path) => pathEndpoint(path.getAttribute("d") ?? ""),
      );
      expect(pmEndpoints).not.toContainEqual({ x: 360, y: 110 });

      const assignStarts = Array.from(
        document.querySelectorAll<SVGPathElement>(".llm-graph__edge--assign"),
        (path) => pathStart(path.getAttribute("d") ?? ""),
      );
      expect(assignStarts).toContainEqual({ x: 540, y: 230 });
      expect(assignStarts).not.toContainEqual({ x: 540, y: 110 });
    } finally {
      fetcher.restore();
    }
  });

  it("creates, updates, and removes capability inheritance from the modal", async () => {
    // code-health: ignore[nloc] Inheritance modal regression keeps create, update, delete, and graph refresh assertions in one flow.
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
    const updatedGraph = {
      ...graph,
      inheritance: [
        {
          capability: "voice.transcribe",
          inherits_from: "default",
          source: "explicit",
        },
      ],
    };
    const fetcher = installPageFetch({
      "/admin/api/v1/llm/graph": [
        { body: graph },
        { body: explicitGraph },
        { body: updatedGraph },
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
      "/admin/api/v1/llm/inheritance/voice.transcribe": [
        {
          body: {
            capability: "voice.transcribe",
            inherits_from: "default",
            source: "explicit",
          },
        },
        { status: 204, body: null },
      ],
    });
    try {
      render(<Harness />);
      await findOpenRouterProvider();
      const board = within(graphBoard());
      expect(board.queryByText("implicit default")).not.toBeInTheDocument();
      expect(board.queryByText("invalid implicit")).not.toBeInTheDocument();
      expect(board.queryByText("explicit")).not.toBeInTheDocument();
      expect(board.queryByText("invalid explicit")).not.toBeInTheDocument();
      expect(board.queryByText("Set explicit parent")).not.toBeInTheDocument();
      expect(board.queryByText("Edit in modal")).not.toBeInTheDocument();

      const inheritedChild = screen.getByRole("button", {
        name: /Open assignment and inheritance settings for voice.transcribe/,
      });
      expect(inheritedChild).toHaveClass("llm-graph-node__child", "is-error");
      fireEvent.click(inheritedChild);
      const dialog = assignmentDialog("voice.transcribe");
      fireEvent.change(within(dialog).getByLabelText(/Parent capability/), {
        target: { value: "chat.manager" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Create inheritance" }));

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

      fireEvent.change(within(dialog).getByLabelText(/Change inheritance/), {
        target: { value: "default" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Change inheritance" }));

      await waitFor(() => {
        expect(
          fetcher.calls.some(
            (call) =>
              call.url === "/admin/api/v1/llm/inheritance/voice.transcribe" &&
              call.init.method === "PUT",
          ),
        ).toBe(true);
      });
      const put = fetcher.calls.find(
        (call) =>
          call.url === "/admin/api/v1/llm/inheritance/voice.transcribe" &&
          call.init.method === "PUT",
      );
      expect(jsonBody(put!)).toEqual({ inherits_from: "default" });

      fireEvent.click(within(dialog).getByRole("button", { name: "Remove inheritance" }));

      await waitFor(() => {
        expect(
          fetcher.calls.filter(
            (call) =>
              call.url === "/admin/api/v1/llm/inheritance/voice.transcribe" &&
              call.init.method === "DELETE",
          ),
        ).toHaveLength(1);
      });
    } finally {
      fetcher.restore();
    }
  });
});
