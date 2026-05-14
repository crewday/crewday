import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { LlmGraphPayload, LlmProvider } from "@/types";
import { graph } from "@/pages/admin/LlmPage.testData";
import LlmRegistryModals from "./LlmRegistryModals";
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

function playgroundSection(): HTMLElement {
  return screen.getByRole("region", { name: "Playground" });
}

function elementById(id: string): HTMLElement {
  const element = document.getElementById(id);
  if (!(element instanceof HTMLElement)) throw new Error(`Expected #${id}.`);
  return element;
}

function expectControlBeforeHelp(control: HTMLElement, help: HTMLElement) {
  expect(control.compareDocumentPosition(help) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
    Node.DOCUMENT_POSITION_FOLLOWING,
  );
}

describe("LlmRegistryModals", () => {
  it("renders the edit-only provider-model playground as a direct-only smoke test", () => {
    installFetch();
    renderRegistry(baseGraph, { kind: "providerModel", mode: "edit", id: "pm_gemma" });

    const playground = playgroundSection();
    const systemPrompt = within(playground).getByLabelText("System prompt Optional");
    const prompt = within(playground).getByLabelText("Prompt Required");
    expect(systemPrompt.compareDocumentPosition(prompt)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(within(playground).getByLabelText("Prompt Required")).toBeInTheDocument();
    expect(
      within(playground).queryByRole("button", { name: "Via assignment" }),
    ).not.toBeInTheDocument();
    expect(within(playground).queryByLabelText("Assignment Required")).not.toBeInTheDocument();
    expect(within(playground).queryByLabelText("Temperature Optional")).not.toBeInTheDocument();
    expect(within(playground).queryByLabelText("Max tokens Optional")).not.toBeInTheDocument();
    expect(
      within(playground).queryByLabelText("Image URL Optional"),
    ).not.toBeInTheDocument();
    const actionButtons = within(playground).getAllByRole("button").map((button) => button.textContent);
    expect(actionButtons.at(-1)).toBe("Run playground");
    expect(within(playground).queryByRole("button", { name: "Clear result" })).not.toBeInTheDocument();

    cleanup();
    renderRegistry(baseGraph, { kind: "providerModel", mode: "create" });
    expect(screen.queryByRole("region", { name: "Playground" })).not.toBeInTheDocument();
  });

  it("hides assignment mode when no enabled assignment can use the provider-model", () => {
    installFetch();
    renderRegistry(baseGraph, { kind: "providerModel", mode: "edit", id: "pm_text" });

    const playground = playgroundSection();
    expect(within(playground).queryByRole("button", { name: "Via assignment" })).not.toBeInTheDocument();
    expect(
      within(playground).getByRole("button", { name: "Run playground" }),
    ).toBeInTheDocument();
  });

  it("shows image input for vision-capable provider-models", () => {
    installFetch();
    const testGraph: LlmGraphPayload = {
      ...baseGraph,
      models: baseGraph.models.map((model) =>
        model.id === "model_gemma"
          ? { ...model, capabilities: [...model.capabilities, "vision"] }
          : model,
      ),
    };
    renderRegistry(testGraph, { kind: "providerModel", mode: "edit", id: "pm_gemma" });

    const playground = playgroundSection();
    expect(within(playground).getByLabelText("Image URL Optional")).toBeInTheDocument();
    expect(within(playground).getByLabelText("Upload playground image")).toBeInTheDocument();
  });

  it("runs a direct playground prompt without saving the provider-model", async () => {
    const calls = installFetch((url) =>
      url === "/admin/api/v1/llm/provider-models/pm_gemma/playground"
        ? {
            body: {
              status: "ok",
              assistant_text: "pong",
              reasoning_text: "brief check",
              model_used: "google/gemma-4-31b-it",
              provider_used: "OpenRouter",
              provider_model_id: "pm_gemma",
              assignment_id: null,
              latency_ms: 42,
              input_tokens: 6,
              output_tokens: 1,
              reasoning_tokens: 0,
              finish_reason: "stop",
              stop_reason: "stop",
              cost_usd: "0.000001",
              cost_cents: 0,
              error_message: null,
            },
          }
        : { body: {} },
    );
    renderRegistry(baseGraph, { kind: "providerModel", mode: "edit", id: "pm_gemma" });

    const playground = playgroundSection();
    fireEvent.change(within(playground).getByLabelText("Prompt Required"), {
      target: { value: "Say pong in one word." },
    });
    fireEvent.keyDown(within(playground).getByLabelText("Prompt Required"), { key: "Enter" });
    expect(
      calls.some(
        (call) =>
          call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
          call.init.method === "PUT",
      ),
    ).toBe(false);
    fireEvent.click(within(playground).getByRole("button", { name: "Run playground" }));

    expect(await within(playground).findByText("pong")).toBeInTheDocument();
    expect(within(playground).getByText("OpenRouter")).toBeInTheDocument();
    expect(within(playground).getByText("42 ms")).toBeInTheDocument();

    const post = calls.find(
      (call) => call.url === "/admin/api/v1/llm/provider-models/pm_gemma/playground",
    );
    expect(post?.init.method).toBe("POST");
    expect(bodyOf(post!)).toMatchObject({
      mode: "direct",
      prompt: "Say pong in one word.",
      max_tokens: null,
      temperature: null,
      assignment_id: null,
      image_url: null,
    });
    expect(
      calls.some(
        (call) =>
          call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
          call.init.method === "PUT",
      ),
    ).toBe(false);
  });

  it("submits uploaded playground images as multipart form data", async () => {
    const calls = installFetch((url) =>
      url === "/admin/api/v1/llm/provider-models/pm_gemma/playground"
        ? {
            body: {
              status: "ok",
              assistant_text: "vision pong",
              reasoning_text: null,
              model_used: "google/gemma-4-31b-it",
              provider_used: "OpenRouter",
              provider_model_id: "pm_gemma",
              assignment_id: null,
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
    const testGraph: LlmGraphPayload = {
      ...baseGraph,
      models: baseGraph.models.map((model) =>
        model.id === "model_gemma"
          ? { ...model, capabilities: [...model.capabilities, "vision"] }
          : model,
      ),
    };
    renderRegistry(testGraph, { kind: "providerModel", mode: "edit", id: "pm_gemma" });

    const playground = playgroundSection();
    fireEvent.change(within(playground).getByLabelText("Prompt Required"), {
      target: { value: "Describe this image." },
    });
    const image = new File(["image-bytes"], "receipt.png", { type: "image/png" });
    fireEvent.change(within(playground).getByLabelText("Upload playground image"), {
      target: { files: [image] },
    });
    fireEvent.click(within(playground).getByRole("button", { name: "Run playground" }));

    expect(await within(playground).findByText("vision pong")).toBeInTheDocument();
    const post = calls.find(
      (call) => call.url === "/admin/api/v1/llm/provider-models/pm_gemma/playground",
    );
    expect(post?.init.body).toBeInstanceOf(FormData);
    const form = post!.init.body as FormData;
    expect(form.get("mode")).toBe("direct");
    expect(form.get("prompt")).toBe("Describe this image.");
    expect((form.get("image_file") as File).name).toBe("receipt.png");
  });

  it("does not send unsupported system prompt or temperature playground fields", async () => {
    const calls = installFetch((url) =>
      url === "/admin/api/v1/llm/provider-models/pm_gemma/playground"
        ? {
            body: {
              status: "ok",
              assistant_text: "pong",
              reasoning_text: null,
              model_used: "google/gemma-4-31b-it",
              provider_used: "OpenRouter",
              provider_model_id: "pm_gemma",
              assignment_id: null,
              latency_ms: 42,
              input_tokens: 6,
              output_tokens: 1,
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
    const testGraph: LlmGraphPayload = {
      ...baseGraph,
      provider_models: baseGraph.provider_models.map((pm) =>
        pm.id === "pm_gemma"
          ? {
              ...pm,
              supports_system_prompt: false,
              supports_temperature: false,
              temperature_override: 0.7,
            }
          : pm,
      ),
    };
    renderRegistry(testGraph, { kind: "providerModel", mode: "edit", id: "pm_gemma" });

    const playground = playgroundSection();
    expect(
      within(playground).getByRole("textbox", { name: /^System prompt/ }),
    ).toBeDisabled();
    expect(
      within(playground).queryByRole("spinbutton", { name: /^Temperature/ }),
    ).not.toBeInTheDocument();
    fireEvent.change(within(playground).getByLabelText("Prompt Required"), {
      target: { value: "Say pong." },
    });
    fireEvent.click(within(playground).getByRole("button", { name: "Run playground" }));

    expect(await within(playground).findByText("pong")).toBeInTheDocument();
    const post = calls.find(
      (call) => call.url === "/admin/api/v1/llm/provider-models/pm_gemma/playground",
    );
    expect(bodyOf(post!)).toMatchObject({
      system_prompt: null,
      temperature: null,
    });
  });

  it("validates playground input and displays provider failures", async () => {
    const calls = installFetch((url) =>
      url === "/admin/api/v1/llm/provider-models/pm_gemma/playground"
        ? {
            body: {
              status: "error",
              assistant_text: null,
              reasoning_text: null,
              model_used: "google/gemma-4-31b-it",
              provider_used: "OpenRouter",
              provider_model_id: "pm_gemma",
              assignment_id: null,
              latency_ms: 90,
              input_tokens: null,
              output_tokens: null,
              reasoning_tokens: null,
              finish_reason: null,
              stop_reason: null,
              cost_usd: null,
              cost_cents: null,
              error_message: "Provider rejected the request.",
            },
          }
        : { body: {} },
    );
    renderRegistry(baseGraph, { kind: "providerModel", mode: "edit", id: "pm_gemma" });

    const playground = playgroundSection();
    fireEvent.click(within(playground).getByRole("button", { name: "Run playground" }));
    expect(await within(playground).findByRole("alert")).toHaveTextContent(
      "Prompt is required.",
    );
    expect(
      calls.some((call) => call.url.endsWith("/playground")),
    ).toBe(false);

    fireEvent.change(within(playground).getByLabelText("Prompt Required"), {
      target: { value: "Trigger provider failure." },
    });
    fireEvent.click(within(playground).getByRole("button", { name: "Run playground" }));

    expect(await within(playground).findByText("Failure")).toBeInTheDocument();
    expect(
      within(playground).getByText("Provider rejected the request."),
    ).toBeInTheDocument();
  });

  it("renders playground API failures without exposing raw server detail", async () => {
    installFetch((url) =>
      url === "/admin/api/v1/llm/provider-models/pm_gemma/playground"
        ? {
            status: 422,
            body: {
              type: "https://crewday.dev/errors/validation",
              title: "Validation error",
              status: 422,
              detail: "provider secret sk-live-should-not-render",
              error: "provider_api_key_missing",
            },
          }
        : { body: {} },
    );
    renderRegistry(baseGraph, { kind: "providerModel", mode: "edit", id: "pm_gemma" });

    const playground = playgroundSection();
    fireEvent.change(within(playground).getByLabelText("Prompt Required"), {
      target: { value: "Say pong." },
    });
    fireEvent.click(within(playground).getByRole("button", { name: "Run playground" }));

    expect(await within(playground).findByRole("alert")).toHaveTextContent(
      "The provider client is not configured for playground runs.",
    );
    expect(
      within(playground).queryByText(/sk-live-should-not-render/),
    ).not.toBeInTheDocument();
  });

  it("clears playground results and resets playground inputs", async () => {
    installFetch((url) =>
      url === "/admin/api/v1/llm/provider-models/pm_gemma/playground"
        ? {
            body: {
              status: "ok",
              assistant_text: "pong",
              reasoning_text: null,
              model_used: "google/gemma-4-31b-it",
              provider_used: "OpenRouter",
              provider_model_id: "pm_gemma",
              assignment_id: null,
              latency_ms: 42,
              input_tokens: 6,
              output_tokens: 1,
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
    renderRegistry(baseGraph, { kind: "providerModel", mode: "edit", id: "pm_gemma" });

    const playground = playgroundSection();
    const prompt = within(playground).getByLabelText("Prompt Required");
    fireEvent.change(prompt, { target: { value: "Say pong." } });
    fireEvent.change(within(playground).getByLabelText("System prompt Optional"), {
      target: { value: "Be terse." },
    });
    fireEvent.click(within(playground).getByRole("button", { name: "Run playground" }));
    expect(await within(playground).findByText("pong")).toBeInTheDocument();

    fireEvent.click(within(playground).getByRole("button", { name: "Reset playground" }));
    expect(prompt).toHaveValue("");
    expect(within(playground).getByLabelText("System prompt Optional")).toHaveValue("");
    expect(within(playground).queryByText("pong")).not.toBeInTheDocument();
  });

  it("saves model thinking defaults from the fixed dropdown values", async () => {
    const calls = installFetch();
    renderRegistry(baseGraph, { kind: "model", mode: "edit", id: "model_gemma" });

    const thinking = screen.getByLabelText(/Thinking level/);
    const strategy = screen.getByLabelText(/Thinking strategy/);
    expect(thinking).toHaveValue("disabled");
    expect(strategy).toHaveValue("none");
    expect(screen.getByRole("option", { name: "disabled" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "low" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "medium" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "high" })).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "None / provider default" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "Gemma system token" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "GLM extra body" })).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "OpenRouter reasoning body" }),
    ).toBeInTheDocument();

    fireEvent.change(thinking, { target: { value: "medium" } });
    fireEvent.change(strategy, { target: { value: "gemma_system_token" } });
    fireEvent.click(screen.getByRole("button", { name: "Save model" }));

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/models/model_gemma" &&
            call.init.method === "PUT",
        ),
      ).toBe(true);
    });
    const put = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/models/model_gemma" &&
        call.init.method === "PUT",
    )!;
    expect(bodyOf(put)).toMatchObject({
      thinking_level: "medium",
      thinking_strategy: "gemma_system_token",
    });
  });

  it("shows inherited provider-model thinking and saves explicit overrides", async () => {
    const calls = installFetch();
    const testGraph: LlmGraphPayload = {
      ...baseGraph,
      models: baseGraph.models.map((model) =>
        model.id === "model_gemma" ? { ...model, thinking_level: "medium" } : model,
      ),
      provider_models: baseGraph.provider_models.map((pm) =>
        pm.id === "pm_gemma"
          ? {
              ...pm,
              thinking_level_override: null,
              effective_thinking_level: "medium",
            }
          : pm,
      ),
    };
    renderRegistry(testGraph, { kind: "providerModel", mode: "edit", id: "pm_gemma" });

    const thinking = screen.getByLabelText(/Thinking level/);
    expect(thinking).toHaveValue("inherit");
    expect(within(thinking).getByRole("option", { name: "Model default" })).toBeInTheDocument();
    expect(screen.getByText("Inherited model default: medium. Effective: medium.")).toBeInTheDocument();
    expect(thinking).toHaveAttribute(
      "aria-describedby",
      "llm-provider-model-thinking-help",
    );

    fireEvent.change(thinking, { target: { value: "high" } });
    expect(
      screen.queryByText("Inherited model default: medium. Effective: medium."),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save provider-model" }));

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
            call.init.method === "PUT",
        ),
      ).toBe(true);
    });
    const put = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
        call.init.method === "PUT",
    )!;
    expect(bodyOf(put)).toMatchObject({ thinking_level_override: "high" });
  });

  it("renders provider-model field help after the associated controls", () => {
    installFetch();
    renderRegistry(baseGraph, { kind: "providerModel", mode: "create" });

    const thinking = screen.getByLabelText(/Thinking level/);
    const thinkingHelp = elementById("llm-provider-model-thinking-help");
    const extraParams = screen.getByLabelText(/Extra API params/);
    const extraParamsHelp = elementById("llm-provider-model-extra-help");

    expect(thinking).toHaveAttribute(
      "aria-describedby",
      "llm-provider-model-thinking-help",
    );
    expect(thinkingHelp).toHaveTextContent(
      "Inherited model default: disabled. Effective: disabled.",
    );
    expectControlBeforeHelp(thinking, thinkingHelp);
    expect(extraParams).toHaveAttribute(
      "aria-describedby",
      "llm-provider-model-extra-help",
    );
    expect(extraParamsHelp).toHaveTextContent(
      "JSON object merged into provider requests for this row.",
    );
    expectControlBeforeHelp(extraParams, extraParamsHelp);
  });

  it("ignores malformed provider-model thinking values before save", async () => {
    const calls = installFetch();
    const testGraph: LlmGraphPayload = {
      ...baseGraph,
      provider_models: baseGraph.provider_models.map((pm) =>
        pm.id === "pm_gemma"
          ? {
              ...pm,
              thinking_level_override: "high",
              effective_thinking_level: "high",
            }
          : pm,
      ),
    };
    renderRegistry(testGraph, { kind: "providerModel", mode: "edit", id: "pm_gemma" });

    fireEvent.change(screen.getByLabelText(/Thinking level/), {
      target: { value: "unsupported" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save provider-model" }));

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
            call.init.method === "PUT",
        ),
      ).toBe(true);
    });
    const put = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
        call.init.method === "PUT",
    )!;
    expect(bodyOf(put)).toMatchObject({ thinking_level_override: "high" });
  });

  it("clears provider-model thinking overrides back to the model default", async () => {
    const calls = installFetch();
    const testGraph: LlmGraphPayload = {
      ...baseGraph,
      models: baseGraph.models.map((model) =>
        model.id === "model_gemma" ? { ...model, thinking_level: "medium" } : model,
      ),
      provider_models: baseGraph.provider_models.map((pm) =>
        pm.id === "pm_gemma"
          ? {
              ...pm,
              thinking_level_override: "high",
              effective_thinking_level: "high",
            }
          : pm,
      ),
    };
    renderRegistry(testGraph, { kind: "providerModel", mode: "edit", id: "pm_gemma" });

    const thinking = screen.getByLabelText(/Thinking level/);
    expect(thinking).toHaveValue("high");
    fireEvent.change(thinking, { target: { value: "inherit" } });
    expect(screen.getByText("Inherited model default: medium. Effective: medium.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save provider-model" }));

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
            call.init.method === "PUT",
        ),
      ).toBe(true);
    });
    const put = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
        call.init.method === "PUT",
    )!;
    expect(bodyOf(put)).toMatchObject({ thinking_level_override: null });
  });

  it("inherits and overrides provider-model thinking strategy separately from level", async () => {
    const calls = installFetch();
    const testGraph: LlmGraphPayload = {
      ...baseGraph,
      models: baseGraph.models.map((model) =>
        model.id === "model_gemma"
          ? { ...model, thinking_strategy: "gemma_system_token" }
          : model,
      ),
      provider_models: baseGraph.provider_models.map((pm) =>
        pm.id === "pm_gemma"
          ? {
              ...pm,
              thinking_level_override: "high",
              effective_thinking_level: "high",
              thinking_strategy_override: null,
              effective_thinking_strategy: "gemma_system_token",
            }
          : pm,
      ),
    };
    renderRegistry(testGraph, { kind: "providerModel", mode: "edit", id: "pm_gemma" });

    const level = screen.getByLabelText(/Thinking level/);
    const strategy = screen.getByLabelText(/Thinking strategy/);
    expect(level).toHaveValue("high");
    expect(strategy).toHaveValue("inherit");
    expect(within(strategy).getByRole("option", { name: "Model default" })).toBeInTheDocument();
    expect(
      within(strategy).getByRole("option", { name: "None / provider default" }),
    ).toBeInTheDocument();
    expect(
      within(strategy).getByRole("option", { name: "OpenRouter reasoning body" }),
    ).toBeInTheDocument();
    expect(strategy).toHaveAttribute(
      "aria-describedby",
      "llm-provider-model-thinking-strategy-help",
    );
    expect(
      screen.getByText(
        "Inherited model default: Gemma system token. Effective: Gemma system token.",
      ),
    ).toBeInTheDocument();

    fireEvent.change(strategy, { target: { value: "openrouter_extra_body" } });
    expect(
      screen.getByText(
        "Model default: Gemma system token. Effective: OpenRouter reasoning body.",
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save provider-model" }));

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
            call.init.method === "PUT",
        ),
      ).toBe(true);
    });
    const put = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
        call.init.method === "PUT",
    )!;
    expect(bodyOf(put)).toMatchObject({
      thinking_level_override: "high",
      thinking_strategy_override: "openrouter_extra_body",
    });
  });

  it("clears provider-model thinking strategy overrides back to the model default", async () => {
    const calls = installFetch();
    const testGraph: LlmGraphPayload = {
      ...baseGraph,
      models: baseGraph.models.map((model) =>
        model.id === "model_gemma"
          ? { ...model, thinking_strategy: "gemma_system_token" }
          : model,
      ),
      provider_models: baseGraph.provider_models.map((pm) =>
        pm.id === "pm_gemma"
          ? {
              ...pm,
              thinking_strategy_override: "glm_extra_body",
              effective_thinking_strategy: "glm_extra_body",
            }
          : pm,
      ),
    };
    renderRegistry(testGraph, { kind: "providerModel", mode: "edit", id: "pm_gemma" });

    const strategy = screen.getByLabelText(/Thinking strategy/);
    expect(strategy).toHaveValue("glm_extra_body");
    fireEvent.change(strategy, { target: { value: "inherit" } });
    expect(
      screen.getByText(
        "Inherited model default: Gemma system token. Effective: Gemma system token.",
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save provider-model" }));

    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
            call.init.method === "PUT",
        ),
      ).toBe(true);
    });
    const put = calls.find(
      (call) =>
        call.url === "/admin/api/v1/llm/provider-models/pm_gemma" &&
        call.init.method === "PUT",
    )!;
    expect(bodyOf(put)).toMatchObject({ thinking_strategy_override: null });
  });

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
