import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { RoleProvider } from "@/context/RoleContext";
import { ThemeProvider } from "@/context/ThemeContext";
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
    } finally {
      restore();
    }
  });

  it("omits the demo banner when runtime demo mode is off", async () => {
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
    } finally {
      restore();
    }
  });
});
