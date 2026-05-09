import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setAuthenticated } from "@/auth/authStore";
import { __resetAuthStoreForTests } from "@/auth/useAuth";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { installFetch, jsonResponse } from "@/test/helpers";
import type { AuthMe } from "@/auth/types";
import ChatPage from "./ChatPage";

type GrantRole = "manager" | "worker" | "client";

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

function renderChat(grantRole: GrantRole): string[] {
  const requestedPaths: string[] = [];
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("ws_1");
  setAuthenticated(authMe(grantRole));
  installFetch(({ url, init }) => {
    const parsed = new URL(url, "http://crewday.test");
    requestedPaths.push(parsed.pathname);
    if (parsed.pathname.endsWith("/api/v1/agent/employee/log")) {
      return jsonResponse([{ at: "2026-05-09T08:00:00Z", kind: "agent", body: "Employee log" }]);
    }
    if (parsed.pathname.endsWith("/api/v1/agent/manager/log")) {
      return jsonResponse([{ at: "2026-05-09T08:00:00Z", kind: "agent", body: "Manager log" }]);
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

  const queryClient = new QueryClient({
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
      expect(requestedPaths).toContain("/w/ws_1/api/v1/agent/employee/message");
    });
    expect(requestedPaths).not.toContain("/w/ws_1/api/v1/agent/manager/message");
  });

  it("uses manager agent endpoints for manager sessions", async () => {
    const requestedPaths = renderChat("manager");

    expect(await screen.findByText("Manager log")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      expect(requestedPaths).toContain("/w/ws_1/api/v1/agent/manager/message");
    });
    expect(requestedPaths).not.toContain("/w/ws_1/api/v1/agent/employee/message");
  });

  it("redirects sessions without an embedded workspace chat agent", async () => {
    renderChat("client");

    expect(await screen.findByTestId("home")).toBeInTheDocument();
  });
});
