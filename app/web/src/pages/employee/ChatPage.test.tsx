import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setAuthenticated } from "@/auth/authStore";
import { __resetAuthStoreForTests } from "@/auth/useAuth";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { makeQueryClient } from "@/lib/queryClient";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { installFetch, jsonResponse } from "@/test/helpers";
import type { AuthMe } from "@/auth/types";
import ChatPage from "./ChatPage";

type GrantRole = "manager" | "worker" | "client";

// One pending worker-chat approval so the inline decide card renders.
function approvalPayload(inlineChannel: string) {
  return {
    id: "appr_1",
    workspace_id: "ws_1",
    requester_actor_id: null,
    for_user_id: "usr_1",
    inline_channel: inlineChannel,
    resolved_user_mode: "strict",
    status: "pending",
    decided_by: null,
    decided_at: null,
    decision_note_md: null,
    expires_at: null,
    created_at: "2026-05-09T08:00:00Z",
    action_json: {
      tool_name: "stays.create",
      card_summary: "Create stay at Oak House?",
      card_risk: "medium",
    },
    result_json: null,
  };
}

function authMe(grantRole: GrantRole): AuthMe {
  return {
    user_id: "usr_1",
    display_name: "User One",
    email: "user@example.com",
    current_workspace_id: "ws_1",
    is_deployment_admin: false,
    available_workspaces: [
      {
        workspace: {
          id: "ws_1",
          name: "Acme",
          timezone: "UTC",
          default_currency: "USD",
          default_country: "US",
          default_locale: "en",
        },
        grant_role: grantRole,
        binding_org_id: grantRole === "client" ? "org_1" : null,
        source: "workspace_grant",
      },
    ],
  };
}

interface RenderChatOptions {
  approvalChannel?: string;
  failLog?: boolean;
  queryClient?: QueryClient;
}

function renderChat(grantRole: GrantRole, options: RenderChatOptions = {}): string[] {
  const requestedPaths: string[] = [];
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("ws_1");
  setAuthenticated(authMe(grantRole));
  installFetch(({ url, init }) => {
    const parsed = new URL(url, "http://crewday.test");
    requestedPaths.push((init?.method ?? "GET") + " " + parsed.pathname);
    if (parsed.pathname.endsWith("/api/v1/agent/employee/log")) {
      if (options.failLog) return jsonResponse({ detail: "boom" }, 500);
      return jsonResponse([{ at: "2026-05-09T08:00:00Z", kind: "agent", body: "Employee log" }]);
    }
    if (parsed.pathname.endsWith("/api/v1/agent/manager/log")) {
      if (options.failLog) return jsonResponse({ detail: "boom" }, 500);
      return jsonResponse([{ at: "2026-05-09T08:00:00Z", kind: "agent", body: "Manager log" }]);
    }
    if (parsed.pathname.endsWith("/api/v1/approvals")) {
      const data = options.approvalChannel
        ? [approvalPayload(options.approvalChannel)]
        : [];
      return jsonResponse({ data, next_cursor: null, has_more: false });
    }
    if (/\/api\/v1\/approvals\/[^/]+\/(approve|deny|reject)$/u.test(parsed.pathname)) {
      expect(init?.method).toBe("POST");
      return jsonResponse({ id: "appr_1", status: "approved" });
    }
    if (parsed.pathname.endsWith("/api/v1/agent/employee/message")) {
      expect(init?.method).toBe("POST");
      return jsonResponse({ at: "2026-05-09T08:01:00Z", kind: "agent", body: "Employee reply" });
    }
    if (parsed.pathname.endsWith("/api/v1/agent/manager/message")) {
      expect(init?.method).toBe("POST");
      return jsonResponse({ at: "2026-05-09T08:01:00Z", kind: "agent", body: "Manager reply" });
    }
    throw new Error(`Unscripted fetch: ${url}`);
  });

  const queryClient =
    options.queryClient ??
    new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });

  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/chat"]}>
        <WorkspaceProvider>
          <Routes>
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/" element={<main data-testid="home">Home</main>} />
          </Routes>
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return requestedPaths;
}

beforeEach(() => {
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<ChatPage>", () => {
  it("uses employee agent endpoints for worker sessions", async () => {
    const requestedPaths = renderChat("worker");

    expect(await screen.findByText("Employee log")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      expect(requestedPaths).toContain("POST /w/ws_1/api/v1/agent/employee/message");
    });
    expect(requestedPaths).not.toContain("POST /w/ws_1/api/v1/agent/manager/message");
  });

  it("uses manager agent endpoints for manager sessions", async () => {
    const requestedPaths = renderChat("manager");

    expect(await screen.findByText("Manager log")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      expect(requestedPaths).toContain("POST /w/ws_1/api/v1/agent/manager/message");
    });
    expect(requestedPaths).not.toContain("POST /w/ws_1/api/v1/agent/employee/message");
  });

  it("redirects sessions without an embedded workspace chat agent", async () => {
    renderChat("client");

    expect(await screen.findByTestId("home")).toBeInTheDocument();
  });

  it("wires the manager decide card to the /approvals/{id}/{decision} contract", async () => {
    const requestedPaths = renderChat("manager", { approvalChannel: "web_owner_sidebar" });

    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));

    await waitFor(() => {
      expect(requestedPaths).toContain("POST /w/ws_1/api/v1/approvals/appr_1/approve");
    });
    // Never the dead legacy route keyed by array index.
    expect(
      requestedPaths.some((p) => p.includes("/api/v1/chat/action/")),
    ).toBe(false);
  });

  it("posts a reject decision to the deny alias", async () => {
    const requestedPaths = renderChat("manager", { approvalChannel: "web_owner_sidebar" });

    fireEvent.click(await screen.findByRole("button", { name: "Reject" }));

    await waitFor(() => {
      expect(requestedPaths).toContain("POST /w/ws_1/api/v1/approvals/appr_1/deny");
    });
  });

  it("fetches approvals for a worker session and wires the own-conversation decide card", async () => {
    // The server scopes /approvals to the caller (cd-uu806): a worker fetch
    // returns their own web_worker_chat rows, so the decide card renders and
    // posts to the shared /approvals/{id}/{decision} contract.
    const requestedPaths = renderChat("worker", { approvalChannel: "web_worker_chat" });

    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));

    await waitFor(() => {
      expect(requestedPaths).toContain("POST /w/ws_1/api/v1/approvals/appr_1/approve");
    });
    expect(requestedPaths.some((p) => p === "GET /w/ws_1/api/v1/approvals")).toBe(true);
  });

  it("shows a retryable error state when the chat log fails to load", async () => {
    renderChat("worker", { failLog: true });

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("toasts when a decide mutation fails", async () => {
    const toasts: Array<{ source: string }> = [];
    const queryClient = makeQueryClient({
      onErrorToast: (event) => toasts.push({ source: event.source }),
    });
    queryClient.setDefaultOptions({
      queries: { retry: false },
      mutations: { retry: false },
    });
    // Fail the decide POST by scripting a 500 for it.
    vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("ws_1");
    setAuthenticated(authMe("manager"));
    installFetch(({ url, init }) => {
      const parsed = new URL(url, "http://crewday.test");
      if (parsed.pathname.endsWith("/api/v1/agent/manager/log")) {
        return jsonResponse([{ at: "2026-05-09T08:00:00Z", kind: "agent", body: "Manager log" }]);
      }
      if (parsed.pathname.endsWith("/api/v1/approvals")) {
        return jsonResponse({
          data: [approvalPayload("web_owner_sidebar")],
          next_cursor: null,
          has_more: false,
        });
      }
      if (/\/api\/v1\/approvals\/[^/]+\/approve$/u.test(parsed.pathname)) {
        expect(init?.method).toBe("POST");
        return jsonResponse({ detail: "boom" }, 500);
      }
      throw new Error(`Unscripted fetch: ${url}`);
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/chat"]}>
          <WorkspaceProvider>
            <Routes>
              <Route path="/chat" element={<ChatPage />} />
              <Route path="/" element={<main data-testid="home">Home</main>} />
            </Routes>
          </WorkspaceProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));

    await waitFor(() => {
      expect(toasts).toContainEqual({ source: "mutation" });
    });
  });
});
