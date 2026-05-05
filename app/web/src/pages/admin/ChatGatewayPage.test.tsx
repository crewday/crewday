import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import type {
  AdminChatOverrideRow,
  AdminChatProvider,
  AdminChatProviderTemplate,
} from "@/types/api";
import ChatGatewayPage from "./ChatGatewayPage";
import appSource from "../../App.tsx?raw";
import { jsonResponse } from "@/test/helpers";

interface FakeResponse {
  status?: number;
  body: unknown;
}

interface FetchCall {
  url: string;
  init: RequestInit;
}

function installFetch(scripted: Record<string, FakeResponse[]> = {}) {
  const calls: FetchCall[] = [];
  const queues = new Map<string, FakeResponse[]>();
  for (const [path, responses] of Object.entries({
    "/admin/api/v1/chat/providers": [{ body: providers() }],
    "/admin/api/v1/chat/templates": [{ body: templates() }],
    "/admin/api/v1/chat/overrides": [{ body: overrides() }],
    ...scripted,
  })) {
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
      <MemoryRouter initialEntries={["/admin/chat-gateway"]}>
        <ChatGatewayPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function providers(): AdminChatProvider[] {
  return [
    {
      channel_kind: "offapp_whatsapp",
      label: "WhatsApp",
      phone_display: "Deployment default",
      status: "connected",
      last_webhook_at: "2026-04-25T12:00:00+00:00",
      last_webhook_error: null,
      webhook_url: "https://app.example.test/webhooks/chat/meta_whatsapp",
      verify_token_stub: "***",
      credentials: [
        {
          field: "webhook_signature_secret",
          label: "Webhook signature secret",
          display_stub: "***",
          set: true,
          updated_at: null,
          updated_by: null,
        },
      ],
      templates: [],
      per_workspace_soft_cap: 250,
      daily_outbound_cap: 1000,
      outbound_24h: 12,
      delivery_error_rate_pct: 1.5,
    },
    {
      channel_kind: "offapp_telegram",
      label: "Telegram",
      phone_display: "Not configured",
      status: "not_configured",
      last_webhook_at: null,
      last_webhook_error: null,
      webhook_url: "",
      verify_token_stub: "",
      credentials: [],
      templates: [],
      per_workspace_soft_cap: 0,
      daily_outbound_cap: 1000,
      outbound_24h: 0,
      delivery_error_rate_pct: 0,
    },
  ];
}

function templates(): AdminChatProviderTemplate[] {
  return [
    {
      name: "chat_agent_nudge",
      purpose: "Agent follow-up outside the 24-hour session window",
      status: "pending",
      last_sync_at: null,
      rejection_reason: null,
    },
  ];
}

function overrides(): AdminChatOverrideRow[] {
  return [
    {
      workspace_id: "ws_1",
      workspace_name: "Smoke House",
      channel_kind: "offapp_whatsapp",
      phone_display: "+1 555 123 9999",
      status: "connected",
      created_at: "2026-04-01T00:00:00+00:00",
      reason: "Dedicated Meta account",
    },
  ];
}

function jsonBody(call: FetchCall): unknown {
  return JSON.parse(String(call.init.body));
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
});

describe("<ChatGatewayPage>", () => {
  it("stays under the deployment-admin layout route", () => {
    expect(appSource).toMatch(
      /<Route element={<AdminLayout \/>}>[\s\S]*<Route path="\/admin\/chat-gateway" element={<AdminChatGatewayPage \/>} \/>/,
    );
  });

  it("renders provider, template, and override sections from admin APIs", async () => {
    const fetcher = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("WhatsApp")).toBeInTheDocument();
      expect(screen.getByText("Active providers")).toBeInTheDocument();
      expect(screen.getByText("chat_agent_nudge")).toBeInTheDocument();
      expect(screen.getByText("Webhook signature secret")).toBeInTheDocument();
      expect(screen.getByText("Per-workspace outbound caps")).toBeInTheDocument();
      expect(screen.getByText("Smoke House")).toBeInTheDocument();
      expect(screen.getByText("Dedicated Meta account")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("copies the webhook URL with the success animation", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const fetcher = installFetch();
    try {
      render(<Harness />);
      await screen.findByText("WhatsApp");

      const webhookLabel = screen.getAllByText("Webhook URL")[0];
      if (!webhookLabel) throw new Error("Missing webhook label");
      const webhookRow = webhookLabel.closest(".chat-gateway-panel__webhook");
      if (!(webhookRow instanceof HTMLElement)) throw new Error("Missing webhook copy row");
      fireEvent.click(within(webhookRow).getByRole("button", { name: /copy/i }));

      await waitFor(() => {
        expect(writeText).toHaveBeenCalledWith(
          "https://app.example.test/webhooks/chat/meta_whatsapp",
        );
        expect(within(webhookRow).getByRole("button", { name: /copied/i })).toBeInTheDocument();
      });
    } finally {
      fetcher.restore();
    }
  });

  it("sends a test inbound payload and renders the dispatcher result", async () => {
    const fetcher = installFetch({
      "/admin/api/v1/chat/test-inbound": [
        {
          body: {
            correlation_id: "corr_1",
            message_id: "msg_1",
            binding_id: "bind_1",
            channel_id: "chan_1",
            dispatch_status: "enqueued",
            agent_invoked: true,
            latency_ms: 17,
            failure_reason: null,
          },
        },
      ],
    });
    try {
      render(<Harness />);
      await screen.findByText("WhatsApp");

      fireEvent.change(screen.getByLabelText("From"), {
        target: { value: "+15550001111" },
      });
      fireEvent.change(screen.getByLabelText("Inbound message"), {
        target: { value: "Need an arrival update" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Send test inbound" }));

      expect(await screen.findByText("corr_1")).toBeInTheDocument();
      expect(screen.getByText("msg_1")).toBeInTheDocument();
      expect(screen.getByText("enqueued")).toBeInTheDocument();
      const post = fetcher.calls.find((call) =>
        call.url.endsWith("/admin/api/v1/chat/test-inbound"),
      );
      expect(post?.init.method).toBe("POST");
      expect(jsonBody(post!)).toEqual({
        channel_kind: "offapp_whatsapp",
        external_contact: "+15550001111",
        body_md: "Need an arrival update",
        language_hint: "en",
      });
    } finally {
      fetcher.restore();
    }
  });

  it("renders the test inbound failure inline", async () => {
    const fetcher = installFetch({
      "/admin/api/v1/chat/test-inbound": [
        {
          status: 409,
          body: { detail: "chat_gateway_provider_not_configured" },
        },
      ],
    });
    try {
      render(<Harness />);
      await screen.findByText("WhatsApp");

      fireEvent.click(screen.getByRole("button", { name: "Send test inbound" }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "chat_gateway_provider_not_configured",
      );
    } finally {
      fetcher.restore();
    }
  });
});
