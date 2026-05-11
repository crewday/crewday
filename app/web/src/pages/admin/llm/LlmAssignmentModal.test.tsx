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

const baseGraph = graph as LlmGraphPayload;

describe("LlmAssignmentModal", () => {
  it("uses a searchable provider-model control and saves the selected id", async () => {
    const calls = installFetch();
    renderAssignment("chat.manager");
    const dialog = screen.getByRole("dialog", { name: "chat.manager" });

    const providerModel = within(dialog).getByRole("combobox", {
      name: /^Provider-model/,
    });
    fireEvent.focus(providerModel);
    fireEvent.change(providerModel, { target: { value: "fast" } });
    fireEvent.mouseDown(within(dialog).getByRole("option", { name: /Fast Chat/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Save rung" }));

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/assignments/assign_chat_manager" &&
            call.init.method === "PUT",
        ),
      ).toBe(true);
    });
    const put = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/assignments/assign_chat_manager" &&
        call.init.method === "PUT",
    )!;
    expect(bodyOf(put)).toMatchObject({ provider_model_id: "pm_fast" });
  });

  it("keeps missing-capability provider-model choices visible but disabled", () => {
    installFetch();
    renderAssignment("chat.manager");
    const dialog = screen.getByRole("dialog", { name: "chat.manager" });

    fireEvent.change(within(dialog).getByLabelText(/Required capabilities/), {
      target: { value: "audio_input" },
    });
    expect(within(dialog).getByText("missing audio_input")).toBeInTheDocument();

    const providerModel = within(dialog).getByRole("combobox", {
      name: /^Provider-model/,
    });
    fireEvent.focus(providerModel);
    const disabledOption = within(dialog).getAllByRole("option", {
      name: /missing audio_input/,
    })[0]!;
    expect(disabledOption).toHaveAttribute("aria-disabled", "true");
    expect(within(dialog).getByRole("button", { name: "Save rung" })).toBeDisabled();
  });

  it("preserves read-only deployment-default assignment controls", () => {
    installFetch();
    renderAssignment("default");
    const dialog = screen.getByRole("dialog", { name: "default" });

    expect(within(dialog).getByRole("combobox", { name: /^Provider-model/ })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Save rung" })).toBeDisabled();
  });
});
