import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { RoleProvider } from "@/context/RoleContext";
import { ThemeProvider } from "@/context/ThemeContext";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { setAuthenticated } from "@/auth/authStore";
import { __resetAuthStoreForTests } from "@/auth/useAuth";
import * as preferences from "@/lib/preferences";
import PreviewShell from "@/layouts/PreviewShell";

function installRuntimeFetch(demoMode: boolean): () => void {
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request) => {
    const resolved = typeof url === "string" ? url : url.toString();
    if (resolved !== "/api/v1/runtime/info") {
      throw new Error(`Unexpected fetch call: ${resolved}`);
    }
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ runtime: { demo_mode: demoMode } }),
    } as unknown as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return () => {
    (globalThis as { fetch: typeof fetch }).fetch = original;
  };
}

function renderShell(path = "/today") {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ThemeProvider>
          <RoleProvider>
            <WorkspaceProvider>
              <Routes>
                <Route element={<PreviewShell />}>
                  <Route path="/today" element={<div>today page</div>} />
                  <Route path="/login" element={<div>login page</div>} />
                  <Route path="/recover" element={<div>recover page</div>} />
                  <Route path="/accept/:token" element={<div>accept page</div>} />
                  <Route path="/guest/:token" element={<div>guest page</div>} />
                  <Route path="/w/:slug/guest/:token" element={<div>workspace guest page</div>} />
                </Route>
              </Routes>
            </WorkspaceProvider>
          </RoleProvider>
        </ThemeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  __resetAuthStoreForTests();
  vi.restoreAllMocks();
});

describe("<PreviewShell> demo banner", () => {
  it("renders the demo banner when runtime demo mode is on", async () => {
    const restore = installRuntimeFetch(true);
    try {
      renderShell();
      expect(
        await screen.findByText("Demo data - resets on inactivity"),
      ).toBeInTheDocument();
      expect(screen.queryByText("PREVIEW")).not.toBeInTheDocument();
      expect(screen.queryByText("Interactive mocks · no real data")).not.toBeInTheDocument();
    } finally {
      restore();
    }
  });

  it("omits the demo banner and mocks preview copy when runtime demo mode is off", async () => {
    const restore = installRuntimeFetch(false);
    try {
      renderShell();
      await waitFor(() => {
        expect(globalThis.fetch).toHaveBeenCalledWith(
          "/api/v1/runtime/info",
          expect.objectContaining({ method: "GET" }),
        );
      });
      expect(screen.queryByText("Demo data - resets on inactivity")).toBeNull();
      expect(screen.queryByText("PREVIEW")).not.toBeInTheDocument();
      expect(screen.queryByText("Interactive mocks · no real data")).not.toBeInTheDocument();
      expect(screen.queryByRole("navigation", { name: "Shell controls" })).toBeNull();
    } finally {
      restore();
    }
  });

  it("does not show authority mode controls in authenticated workspace chrome", async () => {
    const restore = installRuntimeFetch(false);
    try {
      setAuthenticated({
        user_id: "usr_manager",
        display_name: "Manager User",
        email: "manager@example.com",
        current_workspace_id: "ws_1",
        is_deployment_admin: false,
        available_workspaces: [],
      });
      renderShell();

      await waitFor(() => {
        expect(globalThis.fetch).toHaveBeenCalledWith(
          "/api/v1/runtime/info",
          expect.objectContaining({ method: "GET" }),
        );
      });
      expect(screen.queryByRole("button", { name: "Employee" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Manager" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Client" })).toBeNull();
      expect(screen.queryByRole("link", { name: "§ styleguide" })).toBeNull();
      expect(screen.queryByRole("button", { name: /Theme:/ })).toBeNull();
    } finally {
      restore();
    }
  });

  it("uses the active workspace grant instead of a stale role cookie for shell role state", async () => {
    vi.spyOn(preferences, "readRoleCookie").mockReturnValue("client");
    vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("ws_1");
    const restore = installRuntimeFetch(false);
    try {
      setAuthenticated({
        user_id: "usr_worker",
        display_name: "Worker User",
        email: "worker@example.com",
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
            grant_role: "worker",
            binding_org_id: null,
            source: "workspace_grant",
          },
        ],
      });
      const view = renderShell();

      expect(await screen.findByText("today page")).toBeInTheDocument();
      expect(view.container.querySelector(".surface")).toHaveAttribute("data-role", "employee");
    } finally {
      restore();
    }
  });

  it.each(["/login", "/recover", "/accept/tok_1", "/guest/tok_1", "/w/dev/guest/tok_1"])(
    "does not show shell controls on public route %s",
    async (path) => {
      const restore = installRuntimeFetch(false);
      try {
        renderShell(path);

        await waitFor(() => {
          expect(globalThis.fetch).toHaveBeenCalledWith(
            "/api/v1/runtime/info",
            expect.objectContaining({ method: "GET" }),
          );
        });
        expect(screen.queryByRole("navigation", { name: "Shell controls" })).toBeNull();
        expect(screen.queryByRole("button", { name: "Employee" })).toBeNull();
        expect(screen.queryByRole("button", { name: "Manager" })).toBeNull();
        expect(screen.queryByRole("button", { name: "Client" })).toBeNull();
        expect(screen.queryByRole("link", { name: "§ styleguide" })).toBeNull();
      } finally {
        restore();
      }
    },
  );
});
