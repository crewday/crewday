import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { LlmGraphPayload, LlmProvider } from "@/types";
import { graph } from "@/pages/admin/LlmPage.testData";
import LlmRegistryModals from "./LlmRegistryModals";
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

function renderRegistry(
  testGraph: LlmGraphPayload,
  dialog: Parameters<typeof LlmRegistryModals>[0]["dialog"],
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <LlmRegistryModals
        dialog={dialog}
        providers={testGraph.providers}
        models={testGraph.models}
        providerModels={testGraph.provider_models}
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

describe("LlmRegistryModals", () => {
  it("uses searchable provider and model controls for provider-model joins", async () => {
    const calls = installFetch();
    const backupProvider: LlmProvider = {
      ...baseGraph.providers[0]!,
      id: "prov_backup",
      name: "Backup Gateway",
      endpoint: "https://backup.test/v1",
    };
    const testGraph: LlmGraphPayload = {
      ...baseGraph,
      providers: [...baseGraph.providers, backupProvider],
    };
    renderRegistry(testGraph, { kind: "providerModel", mode: "create" });

    const provider = screen.getByRole("combobox", { name: /^Provider/ });
    fireEvent.focus(provider);
    fireEvent.change(provider, { target: { value: "backup" } });
    fireEvent.mouseDown(screen.getByRole("option", { name: /Backup Gateway/ }));

    const model = screen.getByRole("combobox", { name: /^Model/ });
    fireEvent.focus(model);
    fireEvent.change(model, { target: { value: "fast" } });
    fireEvent.mouseDown(screen.getByRole("option", { name: /Fast Chat/ }));

    fireEvent.change(screen.getByLabelText(/API model id/), {
      target: { value: "backup/fast-chat" },
    });
    fireEvent.change(screen.getByLabelText(/Input cost per 1M/), {
      target: { value: "0.1" },
    });
    fireEvent.change(screen.getByLabelText(/Output cost per 1M/), {
      target: { value: "0.2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create provider-model" }));

    await waitFor(() => {
      expect(calls.some((call) => call.url === "/admin/api/v1/llm/provider-models")).toBe(true);
    });
    const post = calls.find((call) => call.url === "/admin/api/v1/llm/provider-models")!;
    expect(bodyOf(post)).toMatchObject({
      provider_id: "prov_backup",
      model_id: "model_fast",
      api_model_id: "backup/fast-chat",
    });
  });

  it("uses a searchable default provider-model control while preserving provider-model ids", async () => {
    const calls = installFetch();
    renderRegistry(baseGraph, { kind: "provider", mode: "edit", id: "prov_openrouter" });

    const defaultProviderModel = screen.getByRole("combobox", {
      name: /^Default provider-model/,
    });
    fireEvent.focus(defaultProviderModel);
    expect(
      screen.getByRole("option", {
        name: /Text Only.*test\/text-only.*chat/,
      }),
    ).toBeInTheDocument();
    fireEvent.change(defaultProviderModel, { target: { value: "text" } });
    fireEvent.mouseDown(screen.getByRole("option", { name: /Text Only/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save provider" }));

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/providers/prov_openrouter" &&
            call.init.method === "PUT",
        ),
      ).toBe(true);
    });
    const put = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/providers/prov_openrouter" &&
        call.init.method === "PUT",
    )!;
    expect(bodyOf(put)).toMatchObject({ default_model: "pm_text" });
  });
});
