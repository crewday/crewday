import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, cleanup } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode } from "react";
import { RequireAuth } from "./RequireAuth";
import { __resetAuthStoreForTests } from "./useAuth";
import { setAuthenticated, setLoading, setUnauthenticated } from "./authStore";
import type { AuthMe } from "./types";

const SAMPLE_USER: AuthMe = {
  user_id: "01HZ_USER",
  display_name: "Cara",
  email: "cara@example.com",
  available_workspaces: [],
  current_workspace_id: null,
  is_deployment_admin: false,
};

function App({ initial = "/today", children }: { initial?: string; children?: ReactNode }) {
  const [qc] = [new QueryClient()];
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route path="/login" element={<div>login page</div>} />
          <Route element={<RequireAuth />}>
            <Route path="/" element={<div>protected root</div>} />
            <Route path="/today" element={<div>protected today</div>} />
            <Route path="/property/:id" element={<div>protected property</div>} />
          </Route>
          {children}
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetAuthStoreForTests();
});

afterEach(() => {
  cleanup();
  __resetAuthStoreForTests();
  vi.unstubAllEnvs();
  delete window.__CREWDAY__;
});

describe("<RequireAuth>", () => {
  it("renders the loading hold-pattern while auth state is `loading`", () => {
    setLoading();
    render(<App />);
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByText(/Checking your session/i)).toBeInTheDocument();
    expect(screen.queryByText("protected today")).toBeNull();
  });

  it("redirects to the local login fallback when unauthenticated on / and no public site is configured", () => {
    setUnauthenticated();
    render(<App initial="/" />);
    expect(screen.getByText("login page")).toBeInTheDocument();
    expect(screen.queryByText("protected root")).toBeNull();
  });

  it("sends unauthenticated bare-root visitors to the configured public site", async () => {
    setUnauthenticated();
    window.__CREWDAY__ = { publicSiteUrl: "https://crew.day" };
    const redirectExternal = vi.fn();
    const qc = new QueryClient();

    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route path="/login" element={<div>login page</div>} />
            <Route element={<RequireAuth redirectExternal={redirectExternal} />}>
              <Route path="/" element={<div>protected root</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(redirectExternal).toHaveBeenCalledWith("https://crew.day/");
    expect(screen.getByRole("status")).toHaveTextContent("Opening crew.day");
    expect(screen.queryByText("login page")).toBeNull();
    expect(screen.queryByText("protected today")).toBeNull();
  });

  it("keeps same-origin public-site routes off the login fallback", async () => {
    setUnauthenticated();
    window.__CREWDAY__ = { publicSiteUrl: "/mocks/landing/" };
    const redirectExternal = vi.fn();
    const qc = new QueryClient();

    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route path="/login" element={<div>login page</div>} />
            <Route element={<RequireAuth redirectExternal={redirectExternal} />}>
              <Route path="/" element={<div>protected root</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(redirectExternal).toHaveBeenCalledWith(
      `${window.location.origin}/mocks/landing/`,
    );
    expect(redirectExternal).not.toHaveBeenCalledWith("https://crew.day/");
    expect(screen.queryByText("login page")).toBeNull();
    expect(screen.queryByText("protected root")).toBeNull();
  });

  it("uses same-origin public-site routes from Vite env when the server bootstrap is absent", async () => {
    setUnauthenticated();
    vi.stubEnv("VITE_CREWDAY_PUBLIC_SITE_URL", "/mocks/landing/");
    const redirectExternal = vi.fn();
    const qc = new QueryClient();

    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route path="/login" element={<div>login page</div>} />
            <Route element={<RequireAuth redirectExternal={redirectExternal} />}>
              <Route path="/" element={<div>protected root</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(redirectExternal).toHaveBeenCalledWith(
      `${window.location.origin}/mocks/landing/`,
    );
    expect(redirectExternal).not.toHaveBeenCalledWith("https://crew.day/");
    expect(screen.queryByText("login page")).toBeNull();
    expect(screen.queryByText("protected root")).toBeNull();
  });

  it("uses the Vite public-site env when the server bootstrap is absent", async () => {
    setUnauthenticated();
    vi.stubEnv("VITE_CREWDAY_PUBLIC_SITE_URL", "https://crew.day");
    const redirectExternal = vi.fn();
    const qc = new QueryClient();

    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route path="/login" element={<div>login page</div>} />
            <Route element={<RequireAuth redirectExternal={redirectExternal} />}>
              <Route path="/" element={<div>protected root</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(redirectExternal).toHaveBeenCalledWith("https://crew.day/");
    expect(screen.getByRole("status")).toHaveTextContent("Opening crew.day");
    expect(screen.queryByText("login page")).toBeNull();
    expect(screen.queryByText("protected root")).toBeNull();
  });

  it("keeps the local login fallback when the server bootstrap explicitly unsets the public site", () => {
    setUnauthenticated();
    vi.stubEnv("VITE_CREWDAY_PUBLIC_SITE_URL", "https://crew.day");
    window.__CREWDAY__ = { publicSiteUrl: null };

    render(<App initial="/" />);

    expect(screen.getByText("login page")).toBeInTheDocument();
    expect(screen.queryByText("protected root")).toBeNull();
  });

  it("keeps protected app routes on the login fallback when a public site is configured", () => {
    setUnauthenticated();
    window.__CREWDAY__ = { publicSiteUrl: "/mocks/landing/" };
    const redirectExternal = vi.fn();
    function LoginProbe() {
      const loc = useLocation();
      return <span data-testid="loc">{loc.pathname + loc.search}</span>;
    }
    const qc = new QueryClient();

    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/today"]}>
          <Routes>
            <Route path="/login" element={<LoginProbe />} />
            <Route element={<RequireAuth redirectExternal={redirectExternal} />}>
              <Route path="/today" element={<div>protected today</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.getByTestId("loc")).toHaveTextContent("/login?next=%2Ftoday");
    expect(redirectExternal).not.toHaveBeenCalled();
    expect(screen.queryByText("protected today")).toBeNull();
  });

  it("preserves search and hash in the `next` parameter", () => {
    setUnauthenticated();
    function LoginProbe() {
      const loc = useLocation();
      return <span data-testid="loc">{loc.pathname + loc.search}</span>;
    }
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/property/abc?tab=tasks#row=12"]}>
          <Routes>
            <Route path="/login" element={<LoginProbe />} />
            <Route element={<RequireAuth />}>
              <Route path="/property/:id" element={<div>protected property</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const loc = screen.getByTestId("loc");
    expect(loc.textContent).toContain("/login");
    expect(loc.textContent).toContain("?next=");
    // The `next` payload must round-trip the original path + query.
    const url = new URL("http://localhost" + (loc.textContent ?? ""));
    const next = url.searchParams.get("next");
    expect(next).toBe("/property/abc?tab=tasks#row=12");
  });

  it("renders the protected child when the user is authenticated", () => {
    setAuthenticated(SAMPLE_USER);
    render(<App initial="/today" />);
    expect(screen.getByText("protected today")).toBeInTheDocument();
  });

  it("supports the `children` shape (non-Outlet integration)", () => {
    setAuthenticated(SAMPLE_USER);
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <RequireAuth>
            <span>inline child</span>
          </RequireAuth>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByText("inline child")).toBeInTheDocument();
  });
});
