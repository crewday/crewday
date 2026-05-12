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

function installFetch(): FetchCall[] {
  const calls: FetchCall[] = [];
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({}),
    } as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return calls;
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

describe("LlmAssignmentModal", () => {
  it("shows explicit inheritance as inheritance-only controls with an inherited summary", () => {
    installFetch();
    renderAssignment("chat.admin", inheritedGraph);
    const dialog = screen.getByRole("dialog", { name: "chat.admin" });

    expect(within(dialog).getByText(/inherits the chain owned by/)).toBeInTheDocument();
    expect(within(dialog).getByText("Gemma 4 31B IT")).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/Change inheritance/)).toHaveValue(
      "chat.manager",
    );
    expect(
      within(dialog).queryByLabelText("Direct provider-model chain"),
    ).not.toBeInTheDocument();
    expect(within(dialog).queryByText("Available provider-models")).not.toBeInTheDocument();
    const parentValues = Array.from(
      within(dialog).getByLabelText(/Change inheritance/).querySelectorAll("option"),
      (option) => option.value,
    );
    expect(parentValues).not.toContain("voice.transcribe");
    expect(parentValues).not.toContain("chat.admin");
  });

  it("changes and removes explicit inheritance through the inheritance endpoints", async () => {
    const calls = installFetch();
    renderAssignment("chat.admin", inheritedGraph);
    const dialog = screen.getByRole("dialog", { name: "chat.admin" });

    fireEvent.change(within(dialog).getByLabelText(/Change inheritance/), {
      target: { value: "default" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Change inheritance" }));

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

    fireEvent.click(within(dialog).getByRole("button", { name: "Remove inheritance" }));

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
    expect(within(dialog).getAllByText(/Thinking off/).length).toBeGreaterThan(0);

    const textOnly = providerModelRow(dialog, "Text Only");
    expect(within(textOnly).getByText("missing function_calling")).toBeInTheDocument();
    expect(within(textOnly).getByRole("button", { name: "Add" })).toBeDisabled();

    fireEvent.click(within(providerModelRow(dialog, "Fast Chat")).getByRole("button", {
      name: "Add",
    }));

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
      extra_api_params: {},
      required_capabilities: ["chat", "function_calling"],
      is_enabled: true,
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

  it("removes and reorders selected direct provider-models", async () => {
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

    fireEvent.click(
      within(dialog).getByRole("button", {
        name: "Move Fast Chat via OpenRouter up",
      }),
    );

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

    fireEvent.click(within(providerModelRow(dialog, "Fast Chat")).getByRole("button", {
      name: "Remove",
    }));

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
    fireEvent.drop(gemmaItem, { dataTransfer: dataTransfer() });

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
