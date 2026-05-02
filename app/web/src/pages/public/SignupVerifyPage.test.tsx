// crewday — SignupVerifyPage component test.
//
// Pins the `/signup/verify` (and `/auth/magic/:token`) surface against
// the documented `/api/v1/signup/verify` contract (§03 step 2).
//
// What this covers:
//   1. Verifying state — POST in flight; the "Confirming your link…"
//      view is announced via `role="status"`.
//   2. Happy path — verify resolves; SPA navigates to `/signup/enroll`
//      with the `signup_session_id` + `desired_slug` threaded through
//      router state. Token rides as `?token=…` query.
//   3. Path-param happy path — the `/auth/magic/:token` mailer-default
//      shape lands on the same page; the path param is read in place
//      of the query.
//   4. Missing token (no `?token=`, no path param) — error view; verify
//      endpoint is NEVER called; "Start over" affordance is NOT shown
//      (canRetry = false).
//   5. Expired token (410) — error view + "Start over" link to /signup.
//   6. Invalid/already-consumed token (400/409) — same expired copy +
//      retry affordance (the SPA collapses these into one branch).
//   7. 429 rate-limit — distinct copy, retry affordance.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactElement, type ReactNode } from "react";
import SignupVerifyPage from "./SignupVerifyPage";
import { AuthProvider, __resetAuthStoreForTests } from "@/auth";
import { __resetApiProvidersForTests } from "@/lib/api";

// ── Test harness ──────────────────────────────────────────────────

interface FakeResponse {
  status: number;
  body?: unknown;
}

function installFetch(scripted: Record<string, FakeResponse[]>): {
  calls: Array<{ url: string; init: RequestInit }>;
  restore: () => void;
} {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const original = globalThis.fetch;
  const queues: Record<string, FakeResponse[]> = {};
  for (const [k, v] of Object.entries(scripted)) queues[k] = [...v];
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    const suffix = Object.keys(queues).find((s) => resolved.endsWith(s));
    if (!suffix) throw new Error(`Unscripted fetch: ${resolved}`);
    const next = queues[suffix]!.shift();
    if (!next) throw new Error(`No more responses for: ${resolved}`);
    const ok = next.status >= 200 && next.status < 300;
    const text = next.body === undefined ? "" : JSON.stringify(next.body);
    return {
      ok,
      status: next.status,
      statusText: ok ? "OK" : "Error",
      text: async () => text,
    } as unknown as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

interface SeenState {
  signupSessionId?: unknown;
  desiredSlug?: unknown;
}

function LandedEnroll({ probe }: { probe: { state: SeenState | null } }): ReactElement {
  const loc = useLocation();
  // Stash the location state so the test can assert on it.
  probe.state = (loc.state as SeenState | null) ?? null;
  return <span data-testid="landed-enroll">{loc.pathname}</span>;
}

function Harness({
  initial,
  children,
  enrollProbe,
}: {
  initial: string;
  children?: ReactNode;
  enrollProbe?: { state: SeenState | null };
}): ReactElement {
  const probe = enrollProbe ?? { state: null };
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <AuthProvider>
          <Routes>
            <Route path="/signup/verify" element={<SignupVerifyPage />} />
            <Route path="/auth/magic/:token" element={<SignupVerifyPage />} />
            <Route path="/signup/enroll" element={<LandedEnroll probe={probe} />} />
            <Route path="/signup" element={<span data-testid="landed-signup" />} />
            <Route path="*" element={<span data-testid="landed-other" />} />
          </Routes>
          {children}
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function flush(): Promise<void> {
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

beforeEach(() => {
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
});

afterEach(() => {
  cleanup();
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  vi.unstubAllGlobals();
});

// ── Tests ─────────────────────────────────────────────────────────

describe("<SignupVerifyPage> — verifying state", () => {
  it("renders the 'Confirming your link' view while POST /signup/verify is in flight", async () => {
    let resolveVerify: ((value: Response) => void) | undefined;
    const original = globalThis.fetch;
    const spy = vi.fn(async (url: string | URL | Request) => {
      const resolved = typeof url === "string" ? url : url.toString();
      if (resolved.endsWith("/api/v1/auth/me")) {
        return {
          ok: false,
          status: 401,
          statusText: "Error",
          text: async () => JSON.stringify({ detail: "no session" }),
        } as unknown as Response;
      }
      if (resolved.endsWith("/api/v1/signup/verify")) {
        return await new Promise<Response>((resolve) => {
          resolveVerify = resolve;
        });
      }
      throw new Error(`Unscripted fetch: ${resolved}`);
    });
    (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;

    try {
      render(<Harness initial="/signup/verify?token=tok-pending" />);
      await flush();

      const status = screen.getByTestId("signup-verify-pending");
      expect(status.getAttribute("role")).toBe("status");
      expect(status.textContent).toContain("Confirming your link");
      expect(screen.queryByTestId("signup-verify-error")).toBeNull();
      expect(screen.queryByTestId("landed-enroll")).toBeNull();
    } finally {
      resolveVerify?.({
        ok: true,
        status: 200,
        statusText: "OK",
        text: async () =>
          JSON.stringify({ signup_session_id: "ss_late", desired_slug: "villa" }),
      } as unknown as Response);
      await flush();
      (globalThis as { fetch: typeof fetch }).fetch = original;
    }
  });
});

describe("<SignupVerifyPage> — happy path", () => {
  it("verifies the token and navigates to /signup/enroll with handoff state", async () => {
    const probe: { state: SeenState | null } = { state: null };
    const { calls, restore } = installFetch({
      "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
      "/api/v1/signup/verify": [
        {
          status: 200,
          body: { signup_session_id: "ss_1", desired_slug: "villa-sud" },
        },
      ],
    });

    try {
      render(<Harness initial="/signup/verify?token=tok-good" enrollProbe={probe} />);
      await flush();
      await flush();

      // Verify POST hit the right URL with the token in the body.
      const verifyCall = calls.find((c) => c.url.endsWith("/api/v1/signup/verify"));
      expect(verifyCall).toBeDefined();
      expect(verifyCall!.init.method).toBe("POST");
      const body = JSON.parse(verifyCall!.init.body as string) as Record<string, unknown>;
      expect(body).toEqual({ token: "tok-good" });

      // Navigated to /signup/enroll, with handoff state populated.
      expect(screen.getByTestId("landed-enroll").textContent).toBe("/signup/enroll");
      expect(probe.state).toEqual({
        signupSessionId: "ss_1",
        desiredSlug: "villa-sud",
      });
    } finally {
      restore();
    }
  });

  it("reads the token from the path param on /auth/magic/:token", async () => {
    const probe: { state: SeenState | null } = { state: null };
    const { calls, restore } = installFetch({
      "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
      "/api/v1/signup/verify": [
        {
          status: 200,
          body: { signup_session_id: "ss_2", desired_slug: "ferme-nord" },
        },
      ],
    });

    try {
      render(<Harness initial="/auth/magic/tok-magic" enrollProbe={probe} />);
      await flush();
      await flush();

      const verifyCall = calls.find((c) => c.url.endsWith("/api/v1/signup/verify"));
      expect(verifyCall).toBeDefined();
      const body = JSON.parse(verifyCall!.init.body as string) as Record<string, unknown>;
      expect(body).toEqual({ token: "tok-magic" });

      expect(screen.getByTestId("landed-enroll")).toBeInTheDocument();
      expect(probe.state).toEqual({
        signupSessionId: "ss_2",
        desiredSlug: "ferme-nord",
      });
    } finally {
      restore();
    }
  });
});

describe("<SignupVerifyPage> — error branches", () => {
  it("renders the missing-token error when ?token= is absent and never POSTs", async () => {
    const { calls, restore } = installFetch({
      "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
    });

    try {
      render(<Harness initial="/signup/verify" />);
      await flush();

      const err = screen.getByTestId("signup-verify-error");
      expect(err.textContent).toContain("missing its token");
      // canRetry = false → no "Start over" link.
      expect(screen.queryByText("Start over")).toBeNull();
      // Verify endpoint never fired.
      const verifyCalls = calls.filter((c) => c.url.endsWith("/api/v1/signup/verify"));
      expect(verifyCalls).toHaveLength(0);
    } finally {
      restore();
    }
  });

  it.each([
    { status: 410, label: "expired" },
    { status: 400, label: "invalid" },
    { status: 409, label: "already consumed" },
    { status: 404, label: "not found" },
  ])(
    "renders the expired/invalid copy with retry affordance on $status ($label)",
    async ({ status }) => {
      const { restore } = installFetch({
        "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
        "/api/v1/signup/verify": [{ status, body: { detail: "no good" } }],
      });

      try {
        render(<Harness initial="/signup/verify?token=tok-bad" />);
        await flush();
        await flush();

        const err = screen.getByTestId("signup-verify-error");
        expect(err.textContent).toMatch(/expired, already used, or invalid/i);
        const retry = screen.getByText("Start over");
        expect(retry.closest("a")?.getAttribute("href")).toBe("/signup");
        // The pending view is gone.
        expect(screen.queryByTestId("signup-verify-pending")).toBeNull();
      } finally {
        restore();
      }
    },
  );

  it("surfaces the rate-limit copy on 429 with a retry affordance", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
      "/api/v1/signup/verify": [
        { status: 429, body: { detail: "Too many requests." } },
      ],
    });

    try {
      render(<Harness initial="/signup/verify?token=tok-throttled" />);
      await flush();
      await flush();

      const err = screen.getByTestId("signup-verify-error");
      expect(err.textContent).toMatch(/Too many attempts/i);
      expect(screen.getByText("Start over")).toBeInTheDocument();
    } finally {
      restore();
    }
  });
});
