import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { RoleProvider } from "@/context/RoleContext";
import { ThemeProvider } from "@/context/ThemeContext";
import { setAuthenticated } from "@/auth/authStore";
import { __resetAuthStoreForTests } from "@/auth/useAuth";
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

function renderShell() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/today"]}>
        <ThemeProvider>
          <RoleProvider>
            <Routes>
              <Route element={<PreviewShell />}>
                <Route path="/today" element={<div>today page</div>} />
              </Route>
            </Routes>
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
      expect(screen.getByRole("navigation", { name: "Shell controls" })).toBeInTheDocument();
    } finally {
      restore();
    }
  });

  it("does not show authority mode pills in authenticated workspace chrome", async () => {
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
      expect(screen.getByRole("button", { name: /Theme:/ })).toBeInTheDocument();
    } finally {
      restore();
    }
  });
});
