import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import App from "@/App";
import { AuthProvider, __resetAuthStoreForTests } from "@/auth";
import { RoleProvider } from "@/context/RoleContext";
import { SseProvider } from "@/context/SseContext";
import { ThemeProvider } from "@/context/ThemeContext";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { installFetchRoutes, type FakeResponse } from "@/test/helpers";

function installFetch(scripted: Record<string, FakeResponse[]>) {
  return installFetchRoutes(scripted, { match: "endsWith" });
}

function renderAppAt(path: string) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <AuthProvider>
          <ThemeProvider>
            <RoleProvider>
              <WorkspaceProvider>
                <SseProvider>
                  <App />
                </SseProvider>
              </WorkspaceProvider>
            </RoleProvider>
          </ThemeProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
});

describe("/styleguide", () => {
  it("renders shell controls and the styleguide baseline without auth bootstrap", async () => {
    const { calls, restore } = installFetch({
      "/api/v1/runtime/info": [{ body: { runtime: { demo_mode: false } } }],
    });
    try {
      renderAppAt("/styleguide");

      expect(
        await screen.findByRole("heading", { name: "Paper, moss, and a little grit." }),
      ).toBeInTheDocument();
      expect(screen.getByRole("navigation", { name: "Shell controls" })).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "Palette" })).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "Buttons" })).toBeInTheDocument();

      await waitFor(() => {
        expect(calls.some((call) => call.url.endsWith("/api/v1/runtime/info"))).toBe(true);
      });
      expect(calls.some((call) => call.url.endsWith("/api/v1/auth/me"))).toBe(false);
    } finally {
      restore();
    }
  });

  it("still runs auth bootstrap on app routes", async () => {
    const { calls, restore } = installFetch({
      "/api/v1/runtime/info": [{ body: { runtime: { demo_mode: false } } }],
      "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
    });
    try {
      renderAppAt("/today");

      await waitFor(() => {
        expect(calls.some((call) => call.url.endsWith("/api/v1/auth/me"))).toBe(true);
      });
      expect(
        screen.queryByRole("heading", { name: "Paper, moss, and a little grit." }),
      ).not.toBeInTheDocument();
    } finally {
      restore();
    }
  });
});
