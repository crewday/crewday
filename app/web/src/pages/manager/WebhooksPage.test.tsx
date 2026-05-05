import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import WebhooksPage from "./WebhooksPage";
import type { Webhook, WebhookDelivery } from "@/types/api";
import { jsonResponse } from "@/test/helpers";

function installFetch({ failWebhooks = false }: { failWebhooks?: boolean } = {}) {
  const calls: string[] = [];
  const requests: Array<{ url: string; method: string; body: unknown }> = [];
  const original = globalThis.fetch;
  const webhooks: Webhook[] = [
    {
      id: "wh_1",
      name: "Hermes prod",
      url: "https://hooks.example.test/crewday",
      events: ["task.completed", "approval.pending"],
      active: true,
      paused_reason: null,
      paused_at: null,
      secret_last_4: "1234",
      last_delivery_status: 202,
      last_delivery_at: "2026-04-29T12:00:00Z",
      created_at: "2026-04-29T11:00:00Z",
      updated_at: "2026-04-29T11:00:00Z",
    },
    {
      id: "wh_2",
      name: "New issue pings",
      url: "https://hooks.example.test/new",
      events: ["issue.reported"],
      active: true,
      paused_reason: null,
      paused_at: null,
      secret_last_4: "5678",
      last_delivery_status: null,
      last_delivery_at: null,
      created_at: "2026-04-29T11:10:00Z",
      updated_at: "2026-04-29T11:10:00Z",
    },
  ];
  const deliveries: WebhookDelivery[] = [
    {
      id: "whd_1",
      subscription_id: "wh_1",
      event: "task.completed",
      status: "succeeded",
      attempt: 1,
      response_status: 200,
      error: null,
      created_at: "2026-04-29T12:05:00Z",
      next_attempt_at: null,
    },
  ];
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    // code-health: ignore[nloc] Webhook route fixture keeps create/reveal/test/rotate endpoints visible together.
    const resolved = typeof url === "string" ? url : url.toString();
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
    calls.push(resolved);
    requests.push({ url: resolved, method, body });
    if (resolved === "/api/v1/auth/me") {
      return jsonResponse({
        user_id: "usr_1",
        display_name: "Mina",
        email: "mina@example.com",
        available_workspaces: [],
        current_workspace_id: "ws_1",
      });
    }
    if (resolved === "/api/v1/me/workspaces") {
      return jsonResponse([
        {
          workspace_id: "ws_1",
          slug: "acme",
          name: "Acme",
          current_role: "manager",
          last_seen_at: null,
          settings_override: {},
        },
      ]);
    }
    if (resolved === "/w/acme/api/v1/webhooks") {
      if (failWebhooks) {
        return jsonResponse({ type: "server_error", title: "Server error" }, 500);
      }
      if (method === "POST") {
        const created: Webhook = {
          id: "wh_3",
          name: body.name,
          url: body.url,
          events: body.events,
          active: body.active,
          paused_reason: null,
          paused_at: null,
          secret_last_4: "9999",
          secret: "whsec_created_secret",
          last_delivery_status: null,
          last_delivery_at: null,
          created_at: "2026-04-29T13:00:00Z",
          updated_at: "2026-04-29T13:00:00Z",
        };
        webhooks.push({ ...created, secret: null });
        return jsonResponse(created, 201);
      }
      return jsonResponse(webhooks);
    }
    if (resolved === "/w/acme/api/v1/webhooks/wh_1/deliveries") {
      return jsonResponse(deliveries);
    }
    if (resolved === "/w/acme/api/v1/webhooks/wh_1/test") {
      const delivery: WebhookDelivery = {
        id: "whd_test",
        subscription_id: "wh_1",
        event: "task.completed",
        status: "pending",
        attempt: 0,
        response_status: null,
        error: null,
        created_at: "2026-04-29T13:05:00Z",
        next_attempt_at: "2026-04-29T13:05:00Z",
      };
      deliveries.unshift(delivery);
      return jsonResponse(delivery, 201);
    }
    if (resolved === "/w/acme/api/v1/webhooks/wh_1/rotate-secret") {
      return jsonResponse({
        ...webhooks[0],
        secret_last_4: "0000",
        secret: "whsec_rotated_secret",
        updated_at: "2026-04-29T13:10:00Z",
      });
    }
    throw new Error(`Unexpected fetch call: ${resolved}`);
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    requests,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WorkspaceProvider>
          <WebhooksPage />
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<WebhooksPage>", () => {
  it("loads workspace webhooks and handles subscriptions with no deliveries", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("https://hooks.example.test/crewday")).toBeInTheDocument();
      expect(screen.getByText("202 · active")).toBeInTheDocument();
      expect(screen.getByText("https://hooks.example.test/new")).toBeInTheDocument();
      expect(screen.getByText("never")).toBeInTheDocument();
      expect(screen.getByText("pending")).toBeInTheDocument();
      expect(fake.calls).toContain("/w/acme/api/v1/webhooks");
    } finally {
      fake.restore();
    }
  });

  it("shows the mock failure state on query error", async () => {
    const fake = installFetch({ failWebhooks: true });
    try {
      render(<Harness />);

      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("creates a webhook subscription and shows the returned secret once", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "+ New subscription" }));
      fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Ops bridge" } });
      fireEvent.change(screen.getByLabelText("URL"), {
        target: { value: "https://ops.example.test/crewday" },
      });
      fireEvent.change(screen.getByLabelText("Events"), {
        target: { value: "task.completed, issue.reported" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Create subscription" }));

      expect(await screen.findByText("Save this secret now")).toBeInTheDocument();
      expect(screen.getByLabelText("Plaintext webhook secret")).toHaveTextContent("whsec_created_secret");
      expect(await screen.findByText("https://ops.example.test/crewday")).toBeInTheDocument();
      expect(fake.requests).toContainEqual({
        url: "/w/acme/api/v1/webhooks",
        method: "POST",
        body: {
          name: "Ops bridge",
          url: "https://ops.example.test/crewday",
          events: ["task.completed", "issue.reported"],
          active: true,
        },
      });
    } finally {
      fake.restore();
    }
  });

  it("opens the delivery log and posts a test delivery", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("https://hooks.example.test/crewday")).toBeInTheDocument();
      fireEvent.click(screen.getAllByRole("button", { name: "Log" })[0]!);

      expect(await screen.findByRole("dialog", { name: /Delivery log/ })).toBeInTheDocument();
      expect(await screen.findByText("task.completed")).toBeInTheDocument();
      expect(screen.getByText("200")).toBeInTheDocument();

      fireEvent.click(screen.getAllByRole("button", { name: "Test" })[0]!);
      await waitFor(() => {
        expect(fake.requests).toContainEqual({
          url: "/w/acme/api/v1/webhooks/wh_1/test",
          method: "POST",
          body: null,
        });
      });
      await waitFor(() => {
        expect(screen.getAllByText("pending").length).toBeGreaterThan(1);
      });
      expect(fake.calls).toContain("/w/acme/api/v1/webhooks/wh_1/deliveries");

      fireEvent.keyDown(window, { key: "Escape" });
      await waitFor(() => {
        expect(screen.queryByRole("dialog", { name: /Delivery log/ })).not.toBeInTheDocument();
      });
    } finally {
      fake.restore();
    }
  });

  it("rotates a webhook secret and shows the new secret once", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("https://hooks.example.test/crewday")).toBeInTheDocument();
      fireEvent.click(screen.getAllByRole("button", { name: "Rotate secret" })[0]!);

      expect(await screen.findByText("Save this secret now")).toBeInTheDocument();
      expect(screen.getByLabelText("Plaintext webhook secret")).toHaveTextContent("whsec_rotated_secret");
      expect(fake.requests).toContainEqual({
        url: "/w/acme/api/v1/webhooks/wh_1/rotate-secret",
        method: "POST",
        body: null,
      });
    } finally {
      fake.restore();
    }
  });
});
