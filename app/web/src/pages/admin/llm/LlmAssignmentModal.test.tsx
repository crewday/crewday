import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { LlmGraphPayload } from "@/types";
import { graph } from "@/pages/admin/LlmPage.testData";
import LlmAssignmentModal from "./LlmAssignmentModal";
import { buildLlmIndexes } from "./lib/llmIndexes";

interface FetchCall {
  url: string;
  init: RequestInit;
}

function installFetch(
  responseFor?: (url: string, init: RequestInit) => { status?: number; body: unknown },
): FetchCall[] {
  const calls: FetchCall[] = [];
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = String(url);
    const requestInit = init ?? {};
    calls.push({ url: resolved, init: requestInit });
    const response = responseFor?.(resolved, requestInit) ?? { body: {} };
    const status = response.status ?? 200;
    const ok = status >= 200 && status < 300;
    return {
      ok,
      status,
      statusText: ok ? "OK" : "Error",
      text: async () => JSON.stringify(response.body),
    } as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return calls;
}

function installPendingFetch(): { calls: FetchCall[]; resolve: () => void } {
  const calls: FetchCall[] = [];
  let resolve: () => void = () => undefined;
  const pending = new Promise<Response>((done) => {
    resolve = () => {
      done({
        ok: true,
        status: 200,
        statusText: "OK",
        text: async () => JSON.stringify({}),
      } as Response);
    };
  });
  const spy = vi.fn((url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    return pending;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return { calls, resolve };
}

function renderAssignment(capabilityKey: string, testGraph: LlmGraphPayload = baseGraph) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <LlmAssignmentModal
        capabilityKey={capabilityKey}
        graph={testGraph}
        indexes={buildLlmIndexes(testGraph)}
        onClose={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

function bodyOf(call: FetchCall): unknown {
  return JSON.parse(String(call.init.body));
}

function providerModelRow(dialog: HTMLElement, name: string): HTMLElement {
  const row = within(dialog).getByText(name).closest(".llm-assignment-provider-model");
  if (!(row instanceof HTMLElement)) throw new Error(`${name} row not found`);
  return row;
}

function visibleOptionLabels(dialog: HTMLElement, label: RegExp): string[] {
  const picker = within(dialog).getByRole("combobox", { name: label });
  fireEvent.focus(picker);
  const listbox = within(dialog).getByRole("listbox", { name: label });
  return within(listbox)
    .getAllByRole("option")
    .map((option) => option.textContent ?? "");
}

function pickerColumn(dialog: HTMLElement, heading: string): HTMLElement {
  const column = within(dialog).getByText(heading).closest("section");
  if (!(column instanceof HTMLElement)) throw new Error(`${heading} column not found`);
  return column;
}

function dataTransfer(): DataTransfer {
  return {
    effectAllowed: "all",
    dropEffect: "none",
    files: [] as unknown as FileList,
    items: [] as unknown as DataTransferItemList,
    types: [],
    clearData: vi.fn(),
    getData: vi.fn(),
    setData: vi.fn(),
    setDragImage: vi.fn(),
  };
}

const baseGraph = graph as LlmGraphPayload;

const inheritedGraph: LlmGraphPayload = {
  ...baseGraph,
  capabilities: [
    ...baseGraph.capabilities,
    {
      key: "chat.admin",
      description: "Deployment-admin embedded chat agent",
      required_capabilities: ["chat", "function_calling"],
      spend_usd_30d: 0,
      calls_30d: 0,
      direct_spend_usd_30d: 0,
      direct_calls_30d: 0,
      inherited_spend_usd_30d: 0,
      inherited_calls_30d: 0,
    },
  ],
  inheritance: [
    ...baseGraph.inheritance,
    {
      capability: "chat.admin",
      inherits_from: "chat.manager",
      source: "explicit",
    },
  ],
};

const sortedParentsGraph: LlmGraphPayload = {
  ...baseGraph,
  capabilities: [
    ...baseGraph.capabilities,
    {
      key: "alpha.parent",
      description: "One inherited child",
      required_capabilities: ["chat"],
      spend_usd_30d: 0,
      calls_30d: 0,
      direct_spend_usd_30d: 0,
      direct_calls_30d: 0,
      inherited_spend_usd_30d: 0,
      inherited_calls_30d: 0,
    },
    {
      key: "beta.parent",
      description: "Two inherited children",
      required_capabilities: ["chat"],
      spend_usd_30d: 0,
      calls_30d: 0,
      direct_spend_usd_30d: 0,
      direct_calls_30d: 0,
      inherited_spend_usd_30d: 0,
      inherited_calls_30d: 0,
    },
    {
      key: "child.one",
      description: "First child",
      required_capabilities: ["chat"],
      spend_usd_30d: 0,
      calls_30d: 0,
      direct_spend_usd_30d: 0,
      direct_calls_30d: 0,
      inherited_spend_usd_30d: 0,
      inherited_calls_30d: 0,
    },
    {
      key: "child.two",
      description: "Second child",
      required_capabilities: ["chat"],
      spend_usd_30d: 0,
      calls_30d: 0,
      direct_spend_usd_30d: 0,
      direct_calls_30d: 0,
      inherited_spend_usd_30d: 0,
      inherited_calls_30d: 0,
    },
    {
      key: "child.three",
      description: "Third child",
      required_capabilities: ["chat"],
      spend_usd_30d: 0,
      calls_30d: 0,
      direct_spend_usd_30d: 0,
      direct_calls_30d: 0,
      inherited_spend_usd_30d: 0,
      inherited_calls_30d: 0,
    },
  ],
  inheritance: [
    ...baseGraph.inheritance,
    {
      capability: "child.one",
      inherits_from: "beta.parent",
      source: "explicit",
    },
    {
      capability: "child.two",
      inherits_from: "beta.parent",
      source: "explicit",
    },
    {
      capability: "child.three",
      inherits_from: "alpha.parent",
      source: "explicit",
    },
  ],
};

const sortedInheritedParentsGraph: LlmGraphPayload = {
  ...sortedParentsGraph,
  capabilities: [
    ...sortedParentsGraph.capabilities,
    {
      key: "chat.admin",
      description: "Deployment-admin embedded chat agent",
      required_capabilities: ["chat", "function_calling"],
      spend_usd_30d: 0,
      calls_30d: 0,
      direct_spend_usd_30d: 0,
      direct_calls_30d: 0,
      inherited_spend_usd_30d: 0,
      inherited_calls_30d: 0,
    },
  ],
  inheritance: [
    ...sortedParentsGraph.inheritance,
    {
      capability: "chat.admin",
      inherits_from: "chat.manager",
      source: "explicit",
    },
  ],
};

describe("LlmAssignmentModal", () => {
  it("shows explicit inheritance as inheritance-only controls with an inherited summary", () => {
    installFetch();
    renderAssignment("chat.admin", inheritedGraph);
    const dialog = screen.getByRole("dialog", { name: "chat.admin" });

    expect(within(dialog).getByText(/inherits the chain owned by/)).toBeInTheDocument();
    expect(within(dialog).getByText("Gemma 4 31B IT")).toBeInTheDocument();
    const parentPicker = within(dialog).getByRole("combobox", {
      name: /Change inheritance/,
    });
    expect(parentPicker).toHaveValue("chat.manager");
    expect(dialog.querySelector("select")).not.toBeInTheDocument();
    expect(
      within(dialog).queryByLabelText("Direct provider-model chain"),
    ).not.toBeInTheDocument();
    expect(within(dialog).queryByText("Available provider-models")).not.toBeInTheDocument();
    fireEvent.focus(parentPicker);
    expect(
      within(dialog).queryByRole("option", { name: /voice\.transcribe/ }),
    ).not.toBeInTheDocument();
    expect(
      within(dialog).queryByRole("option", { name: /chat\.admin/ }),
    ).not.toBeInTheDocument();
    fireEvent.change(parentPicker, { target: { value: "default" } });
    expect(within(dialog).getByRole("option", { name: /default/ })).toBeInTheDocument();
    expect(
      within(dialog).queryByRole("option", { name: /chat\.manager/ }),
    ).not.toBeInTheDocument();
  });

  it("changes and removes explicit inheritance through the inheritance endpoints", async () => {
    const calls = installFetch();
    renderAssignment("chat.admin", inheritedGraph);
    const dialog = screen.getByRole("dialog", { name: "chat.admin" });

    const parentPicker = within(dialog).getByRole("combobox", {
      name: /Change inheritance/,
    });
    fireEvent.focus(parentPicker);
    fireEvent.change(parentPicker, { target: { value: "default" } });
    fireEvent.mouseDown(within(dialog).getByRole("option", { name: /default/ }));
    expect(parentPicker).toHaveValue("default");

    const changeButton = within(dialog).getByRole("button", {
      name: "Change inheritance",
    });
    const footer = changeButton.closest(".form-modal__footer");
    expect(footer).toHaveClass("llm-assignment-dialog__inheritance-footer");
    const removeButton = within(dialog).getByRole("button", {
      name: "Remove inheritance",
    });
    expect(
      removeButton.compareDocumentPosition(changeButton) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    fireEvent.click(changeButton);

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/inheritance/chat.admin" &&
            call.init.method === "PUT",
        ),
      ).toBe(true);
    });
    const put = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/inheritance/chat.admin" &&
        call.init.method === "PUT",
    )!;
    expect(bodyOf(put)).toEqual({ inherits_from: "default" });

    expect(removeButton.closest(".form-modal__footer")).toBe(footer);
    fireEvent.click(removeButton);

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/inheritance/chat.admin" &&
            call.init.method === "DELETE",
        ),
      ).toBe(true);
    });
    expect(
      calls.some(
        (call) =>
          call.url === "/admin/api/v1/llm/assignments" &&
          call.init.method === "POST",
      ),
    ).toBe(false);
  });

  it("disables inherited assignment controls while an inheritance update is pending", async () => {
    const fetcher = installPendingFetch();
    renderAssignment("chat.admin", inheritedGraph);
    const dialog = screen.getByRole("dialog", { name: "chat.admin" });

    const parentPicker = within(dialog).getByRole("combobox", {
      name: /Change inheritance/,
    });
    fireEvent.focus(parentPicker);
    fireEvent.change(parentPicker, { target: { value: "default" } });
    fireEvent.mouseDown(within(dialog).getByRole("option", { name: /default/ }));

    fireEvent.click(within(dialog).getByRole("button", { name: "Change inheritance" }));

    await waitFor(() => {
      expect(fetcher.calls).toHaveLength(1);
    });
    expect(parentPicker).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "Change inheritance" }),
    ).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "Remove inheritance" }),
    ).toBeDisabled();

    fetcher.resolve();
    await waitFor(() => {
      expect(
        within(dialog).getByRole("button", { name: "Change inheritance" }),
      ).toBeEnabled();
    });
  });

  it("sorts searchable parent capabilities by existing inheritance usage", () => {
    installFetch();
    renderAssignment("chat.manager", sortedParentsGraph);
    const dialog = screen.getByRole("dialog", { name: "chat.manager" });

    const createOptions = visibleOptionLabels(dialog, /Parent capability/);
    expect(createOptions[0]).toContain("No explicit parent");
    expect(createOptions[1]).toContain("beta.parent");
    expect(createOptions[2]).toContain("alpha.parent");
    expect(createOptions[3]).toContain("default");
    expect(
      within(dialog).getByRole("combobox", { name: /Parent capability/ }),
    ).toHaveAttribute("aria-autocomplete", "list");
  });

  it("uses the same sorted parent order when changing inheritance", () => {
    installFetch();
    renderAssignment("chat.admin", sortedInheritedParentsGraph);
    const dialog = screen.getByRole("dialog", { name: "chat.admin" });

    const changeOptions = visibleOptionLabels(dialog, /Change inheritance/);
    expect(changeOptions[0]).toContain("Choose a parent capability");
    expect(changeOptions[1]).toContain("beta.parent");
    expect(changeOptions[2]).toContain("alpha.parent");
    expect(changeOptions[3]).toContain("chat.manager");
    expect(changeOptions[4]).toContain("default");
  });

  it("keeps inheritance above the playground and confirms replacing direct assignments", async () => {
    const calls = installFetch();
    renderAssignment("chat.manager");
    const dialog = screen.getByRole("dialog", { name: "chat.manager" });

    const inheritancePane = within(dialog)
      .getByText(/Create an explicit parent/)
      .closest(".llm-assignment-dialog__inheritance");
    const playground = within(dialog).getByRole("region", { name: "Playground" });
    expect(inheritancePane).toBeInstanceOf(HTMLElement);
    expect(
      inheritancePane!.compareDocumentPosition(playground) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    const parentPicker = within(dialog).getByRole("combobox", {
      name: /Parent capability/,
    });
    fireEvent.focus(parentPicker);
    fireEvent.change(parentPicker, { target: { value: "default" } });
    fireEvent.mouseDown(within(dialog).getByRole("option", { name: /default/ }));

    const createButton = within(dialog).getByRole("button", {
      name: "Create inheritance",
    });
    expect(createButton).toBeEnabled();
    fireEvent.click(createButton);

    const confirmation = screen.getByRole("alertdialog", {
      name: "Replace direct assignment chain?",
    });
    expect(confirmation).toHaveTextContent("chat.manager");
    expect(confirmation).toHaveTextContent("Gemma 4 31B IT via OpenRouter");
    fireEvent.click(
      within(confirmation).getByRole("button", { name: "Create inheritance" }),
    );

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/inheritance" &&
            call.init.method === "POST",
        ),
      ).toBe(true);
    });
    const post = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/inheritance" &&
        call.init.method === "POST",
    )!;
    expect(bodyOf(post)).toEqual({
      capability: "chat.manager",
      inherits_from: "default",
      clear_direct_assignments: true,
    });
  });

  it("adds compatible direct provider-models and prevents incompatible additions", async () => {
    const calls = installFetch();
    renderAssignment("chat.manager");
    const dialog = screen.getByRole("dialog", { name: "chat.manager" });

    expect(within(dialog).getByText("Available provider-models")).toBeInTheDocument();
    expect(within(dialog).queryByLabelText(/Max tokens/)).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText(/Temperature/)).not.toBeInTheDocument();
    expect(within(dialog).getByLabelText("Required capabilities")).toBeInTheDocument();
    expect(
      within(dialog).queryByRole("textbox", { name: /Required capabilities/ }),
    ).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText(/Extra API params/)).not.toBeInTheDocument();
    expect(within(dialog).queryByText("compatible")).not.toBeInTheDocument();
    expect(
      within(pickerColumn(dialog, "Available provider-models")).queryByText(
        "google/gemma-4-31b-it",
      ),
    ).not.toBeInTheDocument();
    expect(
      within(pickerColumn(dialog, "Available provider-models")).queryByText(
        "test/fast-chat",
      ),
    ).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/11 calls/)).not.toBeInTheDocument();

    const textOnly = providerModelRow(dialog, "Text Only");
    expect(within(textOnly).getByText("missing function_calling")).toBeInTheDocument();
    expect(within(textOnly).getByRole("button", { name: /Add Text Only/ })).toBeDisabled();

    const fastChat = providerModelRow(dialog, "Fast Chat");
    expect(fastChat).not.toHaveTextContent("test/fast-chat");
    expect(
      within(fastChat).getByRole("button", { name: /Add Fast Chat via OpenRouter/ }),
    ).toBeEnabled();

    fireEvent.click(
      within(fastChat).getByRole("button", {
        name: /Add Fast Chat/,
      }),
    );

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/assignments" &&
            call.init.method === "POST",
        ),
      ).toBe(true);
    });
    const post = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/assignments" &&
        call.init.method === "POST",
    )!;
    expect(bodyOf(post)).toMatchObject({
      capability: "chat.manager",
      provider_model_id: "pm_fast",
      priority: 1,
      max_tokens: null,
      temperature: null,
      thinking_level_override: null,
      extra_api_params: {},
      required_capabilities: ["chat", "function_calling"],
      is_enabled: true,
    });
  });

  it("shows local BGE as feedback.embed compatible and uses embedding smoke", async () => {
    const feedbackGraph: LlmGraphPayload = {
      ...baseGraph,
      providers: [
        ...baseGraph.providers,
        {
          id: "prov_local",
          name: "Local FastEmbed",
          provider_type: "local_embedding",
          endpoint: "",
          api_key_ref: null,
          api_key_status: "missing",
          default_model: null,
          requests_per_minute: 60,
          timeout_s: 60,
          is_enabled: true,
          provider_model_count: 1,
          spend_usd_30d: 0,
          calls_30d: 0,
        },
      ],
      models: [
        ...baseGraph.models,
        {
          id: "model_bge",
          canonical_name: "BAAI/bge-small-en-v1.5",
          display_name: "BGE Small EN v1.5",
          capabilities: ["embeddings"],
          context_window: null,
          max_output_tokens: null,
          embedding_dimensions: 384,
          thinking_level: "disabled",
          thinking_strategy: "none",
          price_source: "manual",
          price_source_model_id: null,
          is_active: true,
          notes: null,
          provider_model_count: 1,
          spend_usd_30d: 0,
          calls_30d: 0,
        },
      ],
      provider_models: [
        ...baseGraph.provider_models,
        {
          id: "pm_bge",
          provider_id: "prov_local",
          model_id: "model_bge",
          api_model_id: "BAAI/bge-small-en-v1.5",
          input_cost_per_million: 0,
          output_cost_per_million: 0,
          fixed_cost_per_call_usd: 0,
          max_tokens_override: null,
          temperature_override: null,
          supports_system_prompt: false,
          supports_temperature: false,
          thinking_strategy_override: null,
          effective_thinking_strategy: "none",
          extra_api_params: {},
          price_source_override: "none",
          price_source_model_id_override: null,
          price_last_synced_at: null,
          is_enabled: true,
          spend_usd_30d: 0,
          calls_30d: 0,
        },
      ],
      capabilities: [
        ...baseGraph.capabilities,
        {
          key: "feedback.embed",
          description: "Compute dense embeddings for texts",
          required_capabilities: ["embeddings"],
          spend_usd_30d: 0,
          calls_30d: 0,
          direct_spend_usd_30d: 0,
          direct_calls_30d: 0,
          inherited_spend_usd_30d: 0,
          inherited_calls_30d: 0,
        },
      ],
      assignments: [
        ...baseGraph.assignments,
        {
          id: "assign_feedback_embed",
          capability: "feedback.embed",
          description: "Compute dense embeddings for texts",
          priority: 0,
          provider_model_id: "pm_bge",
          max_tokens: null,
          temperature: null,
          thinking_level_override: null,
          effective_thinking_level: "disabled",
          effective_thinking_strategy: "none",
          extra_api_params: {},
          required_capabilities: ["embeddings"],
          is_enabled: true,
          last_used_at: null,
          spend_usd_30d: 0,
          calls_30d: 0,
          direct_spend_usd_30d: 0,
          direct_calls_30d: 0,
          inherited_spend_usd_30d: 0,
          inherited_calls_30d: 0,
        },
      ],
    };
    const calls = installFetch((url) =>
      url === "/admin/api/v1/llm/provider-models/pm_bge/embedding-smoke"
        ? {
            body: {
              status: "ok",
              model_used: "BAAI/bge-small-en-v1.5",
              provider_used: "Local FastEmbed",
              provider_model_id: "pm_bge",
              latency_ms: 12,
              embedding_dimensions: 384,
              vector_norm: 1,
            },
          }
        : { body: {} },
    );

    renderAssignment("feedback.embed", feedbackGraph);
    const dialog = screen.getByRole("dialog", { name: "feedback.embed" });

    expect(within(dialog).queryByRole("region", { name: "Playground" })).not.toBeInTheDocument();
    const smoke = within(dialog).getByRole("region", { name: "Embedding smoke" });
    expect(within(smoke).getByText("BAAI/bge-small-en-v1.5")).toBeInTheDocument();
    const textOnly = providerModelRow(dialog, "Text Only");
    expect(within(textOnly).getByText("missing embeddings")).toBeInTheDocument();
    expect(within(textOnly).getByRole("button", { name: /Add Text Only/ })).toBeDisabled();

    fireEvent.change(within(smoke).getByLabelText("Text Required"), {
      target: { value: "feedback text" },
    });
    fireEvent.click(within(smoke).getByRole("button", { name: "Run embedding smoke" }));

    expect(await within(smoke).findByText("Embedding ready")).toBeInTheDocument();
    const post = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/provider-models/pm_bge/embedding-smoke",
    );
    expect(post?.init.method).toBe("POST");
    expect(bodyOf(post!)).toEqual({ text: "feedback text" });
  });

  it("runs playground prompts through the edited assignment defaults", async () => {
    const assignmentGraph: LlmGraphPayload = {
      ...baseGraph,
      assignments: baseGraph.assignments.map((assignment) =>
        assignment.id === "assign_chat_manager"
          ? { ...assignment, max_tokens: 96, temperature: 0.3 }
          : assignment,
      ),
    };
    const calls = installFetch((url) =>
      url === "/admin/api/v1/llm/provider-models/pm_gemma/playground"
        ? {
            body: {
              status: "ok",
              assistant_text: "assignment pong",
              reasoning_text: null,
              model_used: "google/gemma-4-31b-it",
              provider_used: "OpenRouter",
              provider_model_id: "pm_gemma",
              assignment_id: "assign_chat_manager",
              latency_ms: 42,
              input_tokens: 6,
              output_tokens: 2,
              reasoning_tokens: null,
              finish_reason: "stop",
              stop_reason: "stop",
              cost_usd: "0.000001",
              cost_cents: 0,
              error_message: null,
            },
          }
        : { body: {} },
    );
    renderAssignment("chat.manager", assignmentGraph);
    const dialog = screen.getByRole("dialog", { name: "chat.manager" });
    const playground = within(dialog).getByRole("region", { name: "Playground" });

    expect(within(playground).getByLabelText("Assignment tuning defaults")).toHaveTextContent(
      "Max tokens 96",
    );
    expect(within(playground).getByLabelText("Assignment tuning defaults")).toHaveTextContent(
      "Temperature 0.3",
    );
    expect(within(playground).queryByLabelText("Max tokens Optional")).not.toBeInTheDocument();
    expect(within(playground).queryByLabelText("Temperature Optional")).not.toBeInTheDocument();

    fireEvent.change(within(playground).getByLabelText("Prompt Required"), {
      target: { value: "Say pong through the assignment." },
    });
    fireEvent.click(within(playground).getByRole("button", { name: "Run playground" }));

    expect(await within(playground).findByText("assignment pong")).toBeInTheDocument();
    const post = calls.find(
      (call) => call.url === "/admin/api/v1/llm/provider-models/pm_gemma/playground",
    );
    expect(post?.init.method).toBe("POST");
    expect(bodyOf(post!)).toMatchObject({
      mode: "assignment",
      assignment_id: "assign_chat_manager",
      prompt: "Say pong through the assignment.",
      max_tokens: null,
      temperature: null,
    });
  });

  it("creates direct assignments through drag and drop", async () => {
    const calls = installFetch();
    renderAssignment("chat.manager");
    const dialog = screen.getByRole("dialog", { name: "chat.manager" });
    const transfer = dataTransfer();

    fireEvent.dragStart(providerModelRow(dialog, "Fast Chat"), {
      dataTransfer: transfer,
    });
    fireEvent.drop(
      pickerColumn(dialog, "Selected chain").querySelector("ol")!,
      { dataTransfer: transfer },
    );

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/assignments" &&
            call.init.method === "POST",
        ),
      ).toBe(true);
    });
    const post = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/assignments" &&
        call.init.method === "POST",
    )!;
    expect(bodyOf(post)).toMatchObject({
      capability: "chat.manager",
      provider_model_id: "pm_fast",
      priority: 1,
    });
  });

  it("removes, keyboard-reorders, and updates thinking on selected rows", async () => {
    const twoRungGraph: LlmGraphPayload = {
      ...baseGraph,
      assignments: [
        baseGraph.assignments[0]!,
        baseGraph.assignments[1]!,
        {
          ...baseGraph.assignments[1]!,
          id: "assign_chat_manager_fallback",
          provider_model_id: "pm_fast",
          priority: 1,
          thinking_level_override: "high",
          effective_thinking_level: "high",
        },
      ],
    };
    const calls = installFetch();
    renderAssignment("chat.manager", twoRungGraph);
    const dialog = screen.getByRole("dialog", { name: "chat.manager" });

    expect(within(dialog).queryByRole("button", { name: /Move .* up/ })).not.toBeInTheDocument();
    expect(providerModelRow(dialog, "Fast Chat")).toHaveAttribute(
      "aria-keyshortcuts",
      "Alt+ArrowUp Alt+ArrowDown",
    );
    const fastChat = providerModelRow(dialog, "Fast Chat");
    const fastChatThinking = within(fastChat).getByLabelText(
      /Thinking override for Fast Chat via OpenRouter/,
    );
    expect(fastChatThinking).toHaveValue("high");
    expect(
      within(fastChat).queryByText("Thinking", { selector: ".form-field__label" }),
    ).not.toBeInTheDocument();
    expect(
      within(fastChat).queryByText(/Thinking override for Fast Chat/),
    ).not.toBeInTheDocument();

    const [firstThinkingOverride] = within(dialog).getAllByLabelText(
      /Thinking override/,
    );
    if (!firstThinkingOverride) throw new Error("Expected a thinking override control.");

    fireEvent.change(firstThinkingOverride, {
      target: { value: "medium" },
    });

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/assignments/assign_chat_manager" &&
            call.init.method === "PUT",
        ),
      ).toBe(true);
    });
    const thinkingPut = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/assignments/assign_chat_manager" &&
        call.init.method === "PUT",
    )!;
    expect(bodyOf(thinkingPut)).toEqual({ thinking_level_override: "medium" });

    fireEvent.change(fastChatThinking, {
      target: { value: "inherit" },
    });

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url ===
              "/admin/api/v1/llm/assignments/assign_chat_manager_fallback" &&
            call.init.method === "PUT",
        ),
      ).toBe(true);
    });
    const inheritPut = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/assignments/assign_chat_manager_fallback" &&
        call.init.method === "PUT",
    )!;
    expect(bodyOf(inheritPut)).toEqual({ thinking_level_override: null });

    fireEvent.keyDown(providerModelRow(dialog, "Fast Chat"), {
      key: "ArrowUp",
      altKey: true,
    });

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/assignments/reorder" &&
            call.init.method === "PATCH",
        ),
      ).toBe(true);
    });
    const reorder = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/assignments/reorder" &&
        call.init.method === "PATCH",
    )!;
    expect(bodyOf(reorder)).toEqual([
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
        calls.some(
          (call) =>
            call.url ===
              "/admin/api/v1/llm/assignments/assign_chat_manager_fallback" &&
            call.init.method === "DELETE",
        ),
      ).toBe(true);
    });
  });

  it("reorders and removes selected provider-models through drag and drop", async () => {
    const twoRungGraph: LlmGraphPayload = {
      ...baseGraph,
      assignments: [
        baseGraph.assignments[0]!,
        baseGraph.assignments[1]!,
        {
          ...baseGraph.assignments[1]!,
          id: "assign_chat_manager_fallback",
          provider_model_id: "pm_fast",
          priority: 1,
        },
      ],
    };
    const calls = installFetch();
    renderAssignment("chat.manager", twoRungGraph);
    const dialog = screen.getByRole("dialog", { name: "chat.manager" });
    const selectedColumn = pickerColumn(dialog, "Selected chain");
    const gemmaItem = providerModelRow(dialog, "Gemma 4 31B IT").closest("li");
    if (!(gemmaItem instanceof HTMLElement)) throw new Error("Gemma item not found");

    fireEvent.dragStart(providerModelRow(dialog, "Fast Chat"), {
      dataTransfer: dataTransfer(),
    });
    fireEvent.dragOver(gemmaItem, { dataTransfer: dataTransfer(), clientY: 0 });
    expect(gemmaItem).toHaveClass("llm-assignment-picker__selected--drop-before");
    fireEvent.drop(gemmaItem, { dataTransfer: dataTransfer() });
    expect(gemmaItem).not.toHaveClass("llm-assignment-picker__selected--drop-before");

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/assignments/reorder" &&
            call.init.method === "PATCH",
        ),
      ).toBe(true);
    });
    const reorder = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/assignments/reorder" &&
        call.init.method === "PATCH",
    )!;
    expect(bodyOf(reorder)).toEqual([
      {
        capability: "chat.manager",
        ids_in_priority_order: [
          "assign_chat_manager_fallback",
          "assign_chat_manager",
        ],
      },
    ]);

    fireEvent.dragStart(providerModelRow(dialog, "Fast Chat"), {
      dataTransfer: dataTransfer(),
    });
    fireEvent.drop(pickerColumn(dialog, "Available provider-models"), {
      dataTransfer: dataTransfer(),
    });

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url ===
              "/admin/api/v1/llm/assignments/assign_chat_manager_fallback" &&
            call.init.method === "DELETE",
        ),
      ).toBe(true);
    });
    expect(selectedColumn).toBeInTheDocument();
  });

  it("does not offer inheritance creation for the default capability", () => {
    installFetch();
    renderAssignment("default");
    const dialog = screen.getByRole("dialog", { name: "default" });

    expect(within(dialog).queryByLabelText(/Parent capability/)).not.toBeInTheDocument();
    expect(
      within(dialog).queryByRole("button", { name: "Create inheritance" }),
    ).not.toBeInTheDocument();
  });
});
