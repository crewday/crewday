import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, cleanup } from "@testing-library/react";
import { renderHook } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";
import {
  AuthProvider,
  __resetAuthStoreForTests,
  getAuthState,
  WorkspaceGate,
  useAuth,
} from ".";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests, getActiveWorkspaceSlug, registerWorkspaceSlugGetter } from "@/lib/api";
import { installFetchRoutes, type FakeResponse } from "@/test/helpers";

function installFetch(scripted: Record<string, FakeResponse[]>) {
  return installFetchRoutes(scripted, { match: "endsWith" });
}

function Providers({ children, initial = "/" }: { children: ReactNode; initial?: string }) {
  const [qc] = useState(() => new QueryClient());
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <AuthProvider>{children}</AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  document.cookie = "crewday_workspace=; path=/; max-age=0";
});

afterEach(() => {
  cleanup();
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  document.cookie = "crewday_workspace=; path=/; max-age=0";
});

describe("useAuthBootstrap, initial /auth/me probe", () => {
  it("populates the store with the authenticated user when /auth/me returns 200", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [{
        status: 200,
        body: {
          user_id: "01HZ_USER",
          display_name: "Eve",
          email: "eve@example.com",
          available_workspaces: [],
          current_workspace_id: null,
        },
      }],
    });

    try {
      render(
        <Providers>
          <div>app</div>
        </Providers>,
      );
      // One microtask flush is enough for the probe to resolve.
      await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
      expect(getAuthState().status).toBe("authenticated");
      expect(getAuthState().user?.user_id).toBe("01HZ_USER");
    } finally {
      restore();
    }
  });

  it("settles in `unauthenticated` when /auth/me returns 204 (no session)", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [{ status: 204, body: null }],
    });
    try {
      render(
        <Providers>
          <div>app</div>
        </Providers>,
      );
      await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
      expect(getAuthState().status).toBe("unauthenticated");
      expect(getAuthState().user).toBeNull();
    } finally {
      restore();
    }
  });

  it("still handles a 401 from /auth/me as an invalid or expired session", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [{ status: 401, body: { detail: "session invalid" } }],
    });
    try {
      render(
        <Providers>
          <div>app</div>
        </Providers>,
      );
      await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
      expect(getAuthState().status).toBe("unauthenticated");
      expect(getAuthState().user).toBeNull();
    } finally {
      restore();
    }
  });
});

describe("useAuth, logout sequence", () => {
  it("calls /auth/logout, clears the store, and navigates to /login", async () => {
    const { calls, restore } = installFetch({
      "/api/v1/auth/me": [{
        status: 200,
        body: {
          user_id: "01HZ_USER",
          display_name: "Eve",
          email: "eve@example.com",
          available_workspaces: [],
          current_workspace_id: null,
        },
      }],
      "/api/v1/auth/logout": [{ status: 200, body: {} }],
    });

    let location = "";
    function LocationProbe() {
      const loc = useLocation();
      location = loc.pathname + loc.search;
      return null;
    }

    function LogoutTrigger() {
      const { logout, isAuthenticated } = useAuth();
      if (!isAuthenticated) return <span data-testid="not-auth">not-auth</span>;
      return (
        <button type="button" data-testid="logout" onClick={() => { void logout(); }}>
          logout
        </button>
      );
    }

    try {
      render(
        <Providers>
          <Routes>
            <Route path="/" element={<LogoutTrigger />} />
            <Route path="/login" element={<LocationProbe />} />
          </Routes>
        </Providers>,
      );
      // Resolve the bootstrap probe.
      await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
      expect(getAuthState().status).toBe("authenticated");

      const btn = document.querySelector("[data-testid=logout]") as HTMLButtonElement | null;
      expect(btn).not.toBeNull();

      await act(async () => {
        btn!.click();
        await new Promise((r) => setTimeout(r, 0));
      });

      expect(getAuthState().status).toBe("unauthenticated");
      expect(location).toBe("/login");
      const logoutCall = calls.find((c) => c.url.endsWith("/api/v1/auth/logout"));
      expect(logoutCall).toBeDefined();
      expect(logoutCall!.init.method).toBe("POST");
    } finally {
      restore();
    }
  });

  it("logs out locally even if the server endpoint fails", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { restore } = installFetch({
      "/api/v1/auth/me": [{
        status: 200,
        body: {
          user_id: "01HZ_USER",
          display_name: "Eve",
          email: "eve@example.com",
          available_workspaces: [],
          current_workspace_id: null,
        },
      }],
      "/api/v1/auth/logout": [{ status: 500, body: { detail: "boom" } }],
    });

    try {
      const wrapper = ({ children }: { children: ReactNode }) => (
        <Providers>{children}</Providers>
      );
      const { result } = renderHook(() => useAuth(), { wrapper });
      await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
      expect(result.current.isAuthenticated).toBe(true);

      await act(async () => {
        await result.current.logout();
      });

      expect(getAuthState().status).toBe("unauthenticated");
    } finally {
      restore();
      warnSpy.mockRestore();
    }
  });
});

describe("useAuth, 401 mid-session", () => {
  it("a 401 from a protected fetch flips the store to unauthenticated and navigates to /login", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [{
        status: 200,
        body: {
          user_id: "01HZ_USER",
          display_name: "Eve",
          email: "eve@example.com",
          available_workspaces: [],
          current_workspace_id: null,
        },
      }],
      "/api/v1/tasks": [{ status: 401, body: { detail: "session expired" } }],
    });

    let location = "";
    function LocationProbe() {
      const loc = useLocation();
      location = loc.pathname;
      return null;
    }

    try {
      render(
        <Providers initial="/today">
          <Routes>
            <Route path="/today" element={<div>today</div>} />
            <Route path="/login" element={<LocationProbe />} />
          </Routes>
        </Providers>,
      );
      await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
      expect(getAuthState().status).toBe("authenticated");

      // Trigger a 401 from a non-auth endpoint, the registered
      // handler should set the store to unauthenticated and navigate.
      const { fetchJson } = await import("@/lib/api");
      await act(async () => {
        await expect(fetchJson("/api/v1/tasks")).rejects.toBeDefined();
        await new Promise((r) => setTimeout(r, 0));
      });
      expect(getAuthState().status).toBe("unauthenticated");
      expect(location).toBe("/login");
    } finally {
      restore();
    }
  });

  it("a masked workspace /api/v1/me 404 clears a stale workspace and returns an authenticated user to workspace selection", async () => {
    document.cookie = "crewday_workspace=acme; path=/";
    const { restore } = installFetch({
      "/api/v1/auth/me": [{
        status: 200,
        body: {
          user_id: "01HZ_USER",
          display_name: "Eve",
          email: "eve@example.com",
          available_workspaces: [
            {
              workspace_id: "beta",
              workspace: {
                id: "beta",
                name: "Beta",
                timezone: "UTC",
                default_currency: "USD",
                default_country: "US",
                default_locale: "en",
              },
              grant_role: "manager",
              binding_org_id: null,
              source: "workspace_grant",
            },
            {
              workspace_id: "gamma",
              workspace: {
                id: "gamma",
                name: "Gamma",
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
          current_workspace_id: null,
        },
      }],
      "/w/acme/api/v1/me": [{
        status: 404,
        body: {
          type: "https://crewday.dev/errors/not_found",
          title: "Not found",
          status: 404,
          instance: "/w/acme/api/v1/me",
        },
      }],
    });

    let location = "";
    function LocationProbe() {
      const loc = useLocation();
      location = loc.pathname;
      return <WorkspaceGate />;
    }

    try {
      render(
        <Providers initial="/w/acme/">
          <WorkspaceProvider>
            <Routes>
              <Route path="/w/acme/" element={<div>workspace</div>} />
              <Route path="/" element={<LocationProbe />} />
              <Route path="/login" element={<LocationProbe />} />
            </Routes>
          </WorkspaceProvider>
        </Providers>,
      );
      await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
      expect(getAuthState().status).toBe("authenticated");
      expect(getActiveWorkspaceSlug()).toBe("acme");

      const { fetchJson } = await import("@/lib/api");
      await act(async () => {
        await expect(fetchJson("/api/v1/me")).rejects.toBeDefined();
        await new Promise((r) => setTimeout(r, 0));
      });

      expect(getAuthState().status).toBe("authenticated");
      expect(getActiveWorkspaceSlug()).toBeNull();
      expect(location).toBe("/");
      expect(document.body.textContent).toContain("Pick a workspace");
    } finally {
      restore();
    }
  });

  it("an ordinary workspace entity 404 remains a normal not-found error", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [{
        status: 200,
        body: {
          user_id: "01HZ_USER",
          display_name: "Eve",
          email: "eve@example.com",
          available_workspaces: [],
          current_workspace_id: null,
        },
      }],
      "/w/acme/api/v1/tasks/missing": [{
        status: 404,
        body: {
          type: "https://crewday.dev/errors/not_found",
          title: "Not found",
          status: 404,
          instance: "/w/acme/api/v1/tasks/missing",
        },
      }],
    });

    let location = "";
    function LocationProbe() {
      const loc = useLocation();
      location = loc.pathname;
      return null;
    }

    try {
      render(
        <Providers initial="/today">
          <Routes>
            <Route path="/today" element={<><div>today</div><LocationProbe /></>} />
            <Route path="/login" element={<LocationProbe />} />
          </Routes>
        </Providers>,
      );
      await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
      expect(getAuthState().status).toBe("authenticated");

      registerWorkspaceSlugGetter(() => "acme");
      const { fetchJson } = await import("@/lib/api");
      await act(async () => {
        await expect(fetchJson("/api/v1/tasks/missing")).rejects.toBeDefined();
        await new Promise((r) => setTimeout(r, 0));
      });

      expect(getAuthState().status).toBe("authenticated");
      expect(location).toBe("/today");
    } finally {
      restore();
    }
  });

  it("a 401 from /auth/me probe does NOT trigger the redirect handler (avoids bootstrap loop)", async () => {
    // Bootstrap probe returns 401. The store should land on
    // `unauthenticated` via the probe's own catch, not via the 401
    // handler, and crucially, the navigate-to-/login redirect must
    // not fire (the route-level <RequireAuth> handles that, not
    // the central handler).
    const { calls, restore } = installFetch({
      "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
    });

    let navCount = 0;
    function NavCounter() {
      const loc = useLocation();
      navCount += 1;
      return <span data-testid="path">{loc.pathname}</span>;
    }

    try {
      render(
        <Providers initial="/today">
          <Routes>
            <Route path="*" element={<NavCounter />} />
          </Routes>
        </Providers>,
      );
      await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
      expect(getAuthState().status).toBe("unauthenticated");
      // Only the initial render counts, no extra navigation fired.
      expect(navCount).toBe(1);
      // Sanity: the only fetch was the probe itself.
      expect(calls).toHaveLength(1);
    } finally {
      restore();
    }
  });
});
