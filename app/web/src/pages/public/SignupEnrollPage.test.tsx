// crewday — SignupEnrollPage component test.
//
// Pins the `/signup/enroll` surface against the documented two-step
// "passkey ceremony then login" flow (§03 "Self-serve signup"
// steps 3-4). Signup finish does NOT stamp a session cookie, so the
// SPA must follow up with a regular passkey login before navigating —
// the happy-path test below asserts that explicit ordering.
//
// What this covers:
//   1. No handoff state — user deep-linked without going through
//      verify; the "Signup link required" error view renders and
//      neither passkey endpoint fires.
//   2. Happy path — render with handoff state in router state, fill
//      in display name, submit. The full chain runs:
//        POST /signup/passkey/start
//        navigator.credentials.create  (registration)
//        POST /signup/passkey/finish
//        POST /auth/passkey/login/start
//        navigator.credentials.get     (login)
//        POST /auth/passkey/login/finish
//        GET  /auth/me                 (loginWithPasskey post-finish)
//      Then we navigate to the role landing.
//   3. Cancelled ceremony — `navigator.credentials.create` throws
//      `NotAllowedError`; the page surfaces the inline notice; no
//      finish endpoint is called; the form re-arms.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactElement, type ReactNode } from "react";
import SignupEnrollPage from "./SignupEnrollPage";
import type { SignupEnrollHandoff } from "./SignupVerifyPage";
import { AuthProvider, __resetAuthStoreForTests } from "@/auth";
import { __resetApiProvidersForTests } from "@/lib/api";
import { installFetchRoutes, type FakeResponse } from "@/test/helpers";

// ── Test harness ──────────────────────────────────────────────────

function installFetch(scripted: Record<string, FakeResponse[]>) {
  return installFetchRoutes(scripted, { match: "endsWith" });
}

const buf = (...vals: number[]): ArrayBuffer => new Uint8Array(vals).buffer;

function fakeAttestation(): Credential {
  return {
    id: "fake-credential-id",
    rawId: buf(0xaa, 0xbb),
    type: "public-key",
    response: {
      clientDataJSON: buf(0x02),
      attestationObject: buf(0x03),
      getTransports: () => ["internal"],
    },
    getClientExtensionResults: () => ({}),
    authenticatorAttachment: "platform",
  } as unknown as Credential;
}

function fakeAssertion(): Credential {
  return {
    id: "fake-credential-id",
    rawId: buf(0xcc, 0xdd),
    type: "public-key",
    response: {
      authenticatorData: buf(0x04),
      clientDataJSON: buf(0x05),
      signature: buf(0x06),
      userHandle: null,
    },
    getClientExtensionResults: () => ({}),
    authenticatorAttachment: "platform",
  } as unknown as Credential;
}

function installCredentials(behaviour: {
  create?: () => Promise<Credential> | Credential;
  get?: () => Promise<Credential> | Credential;
}): { create: ReturnType<typeof vi.fn>; get: ReturnType<typeof vi.fn> } {
  const createSpy = vi.fn(async () =>
    behaviour.create ? behaviour.create() : fakeAttestation(),
  );
  const getSpy = vi.fn(async () =>
    behaviour.get ? behaviour.get() : fakeAssertion(),
  );
  const nav = globalThis.navigator as unknown as {
    credentials?: { create?: unknown; get?: unknown };
  };
  if (!nav.credentials) {
    (nav as { credentials: unknown }).credentials = {};
  }
  (nav.credentials as { create: unknown; get: unknown }).create = createSpy;
  (nav.credentials as { create: unknown; get: unknown }).get = getSpy;
  return { create: createSpy, get: getSpy };
}

function Harness({
  state,
  children,
}: {
  state?: SignupEnrollHandoff | null;
  children?: ReactNode;
}): ReactElement {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[{ pathname: "/signup/enroll", state: state ?? null }]}>
        <AuthProvider>
          <Routes>
            <Route path="/signup/enroll" element={<SignupEnrollPage />} />
            <Route path="/signup" element={<LocationProbe testid="landed-signup" />} />
            <Route path="/today" element={<LocationProbe testid="landed-today" />} />
            <Route path="/dashboard" element={<LocationProbe testid="landed-dashboard" />} />
            <Route path="/portfolio" element={<LocationProbe testid="landed-portfolio" />} />
            <Route path="*" element={<LocationProbe testid="landed-other" />} />
          </Routes>
          {children}
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function LocationProbe({ testid }: { testid: string }): ReactElement {
  const loc = useLocation();
  return <span data-testid={testid}>{loc.pathname}</span>;
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
  const nav = globalThis.navigator as unknown as { credentials?: { create?: unknown; get?: unknown } };
  if (nav.credentials) {
    delete (nav.credentials as { create?: unknown }).create;
    delete (nav.credentials as { get?: unknown }).get;
  }
});

// ── Tests ─────────────────────────────────────────────────────────

describe("<SignupEnrollPage> — no handoff state", () => {
  it("renders the 'signup link required' error view and never POSTs", async () => {
    const { calls, restore } = installFetch({
      "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
    });

    try {
      render(<Harness state={null} />);
      await flush();

      expect(screen.getByTestId("signup-enroll-no-handoff")).toBeInTheDocument();
      expect(screen.getByText(/signup session has expired/i)).toBeInTheDocument();
      // Start over → /signup.
      const link = screen.getByText("Start over");
      expect(link.closest("a")?.getAttribute("href")).toBe("/signup");
      // No passkey-related fetch ever fired.
      expect(
        calls.some((c) => c.url.includes("/api/v1/signup/passkey")),
      ).toBe(false);
      // Form inputs not present.
      expect(screen.queryByTestId("signup-enroll-name")).toBeNull();
      expect(screen.queryByTestId("signup-enroll-submit")).toBeNull();
    } finally {
      restore();
    }
  });
});

describe("<SignupEnrollPage> — happy path", () => {
  it("runs the full ceremony, then runs passkey login, refreshes /me, and navigates to the role landing", async () => {
    // Two `/auth/me` responses: one for the bootstrap probe (401, no
    // session), then the post-login probe inside `loginWithPasskey`.
    // The post-login probe must return an authenticated /me envelope
    // or the redirect-effect's `isAuthenticated` flag never flips.
    const meAuthed = {
      status: 200,
      body: {
        user_id: "01HZ_USER",
        display_name: "Maria",
        email: "maria@example.com",
        available_workspaces: [
          {
            workspace: {
              id: "ws_villa",
              name: "Villa Sud",
              timezone: "Europe/Paris",
              default_currency: "EUR",
              default_country: "FR",
              default_locale: "fr",
            },
            grant_role: "manager",
            binding_org_id: null,
            source: "workspace_grant",
          },
        ],
        current_workspace_id: null,
        is_deployment_admin: false,
      },
    };
    const { calls, restore } = installFetch({
      "/api/v1/auth/me": [
        { status: 401, body: { detail: "no session" } },
        meAuthed,
      ],
      "/api/v1/signup/passkey/start": [
        {
          status: 200,
          body: {
            challenge_id: "ch_signup",
            options: {
              challenge: "AQID",
              rp: { name: "crew.day" },
              user: { id: "AQID", name: "u", displayName: "U" },
              pubKeyCredParams: [],
            },
          },
        },
      ],
      "/api/v1/signup/passkey/finish": [
        {
          status: 200,
          body: { workspace_slug: "villa-sud", redirect: "/w/villa-sud/dashboard" },
        },
      ],
      "/api/v1/auth/passkey/login/start": [
        {
          status: 200,
          body: {
            challenge_id: "ch_login",
            options: { challenge: "AQID" },
          },
        },
      ],
      "/api/v1/auth/passkey/login/finish": [
        { status: 200, body: { user_id: "01HZ_USER" } },
      ],
    });
    const creds = installCredentials({});

    try {
      render(
        <Harness
          state={{ signupSessionId: "ss_villa", desiredSlug: "villa-sud" }}
        />,
      );
      await flush();

      // Form is rendered with timezone hint (jsdom defaults to UTC or
      // the host's TZ — either is fine, just assert the hint is there).
      const name = screen.getByTestId("signup-enroll-name") as HTMLInputElement;
      fireEvent.change(name, { target: { value: "Maria Aubry" } });
      expect(screen.getByTestId("signup-enroll-timezone")).toBeInTheDocument();

      await act(async () => {
        fireEvent.click(screen.getByTestId("signup-enroll-submit"));
        await new Promise((r) => setTimeout(r, 0));
      });
      // Many microtasks chained: signup-start + create + signup-finish
      // + login-start + get + login-finish + /me + refresh /me + Navigate.
      for (let i = 0; i < 8; i += 1) await flush();

      // Navigator was called once for register, once for login.
      expect(creds.create).toHaveBeenCalledTimes(1);
      expect(creds.get).toHaveBeenCalledTimes(1);

      const urls = calls.map((c) => c.url);
      const idx = (suffix: string): number =>
        urls.findIndex((u) => u.endsWith(suffix));
      const lastIdx = (suffix: string): number => {
        for (let i = urls.length - 1; i >= 0; i -= 1) {
          if (urls[i]!.endsWith(suffix)) return i;
        }
        return -1;
      };
      const signupStart = idx("/api/v1/signup/passkey/start");
      const signupFinish = idx("/api/v1/signup/passkey/finish");
      const loginStart = idx("/api/v1/auth/passkey/login/start");
      const loginFinish = idx("/api/v1/auth/passkey/login/finish");
      const meIdxs = urls
        .map((u, i) => (u.endsWith("/api/v1/auth/me") ? i : -1))
        .filter((i) => i !== -1);
      // Strict ordering: signup ceremony → login ceremony → /me x2.
      expect(signupStart).toBeGreaterThan(-1);
      expect(signupFinish).toBeGreaterThan(signupStart);
      expect(loginStart).toBeGreaterThan(signupFinish);
      expect(loginFinish).toBeGreaterThan(loginStart);
      // /me fires twice: the bootstrap probe (401) on mount, then
      // the post-login probe inside `loginWithPasskey` (200). The
      // last /me must land AFTER login finish.
      expect(meIdxs.length).toBeGreaterThanOrEqual(2);
      expect(lastIdx("/api/v1/auth/me")).toBeGreaterThan(loginFinish);

      // Manager role lands on /dashboard.
      expect(screen.getByTestId("landed-dashboard").textContent).toBe("/dashboard");

      // Body of signup-finish carries display_name + timezone.
      const finishCall = calls.find((c) =>
        c.url.endsWith("/api/v1/signup/passkey/finish"),
      );
      const body = JSON.parse(finishCall!.init.body as string) as Record<string, unknown>;
      expect(body.display_name).toBe("Maria Aubry");
      expect(body.signup_session_id).toBe("ss_villa");
      expect(body.challenge_id).toBe("ch_signup");
      expect(typeof body.timezone).toBe("string");
    } finally {
      restore();
    }
  });
});

describe("<SignupEnrollPage> — cancelled ceremony", () => {
  it("surfaces the inline notice and re-arms the form when the user dismisses the prompt", async () => {
    const { calls, restore } = installFetch({
      "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
      "/api/v1/signup/passkey/start": [
        {
          status: 200,
          body: {
            challenge_id: "ch_signup",
            options: {
              challenge: "AQID",
              rp: { name: "crew.day" },
              user: { id: "AQID", name: "u", displayName: "U" },
              pubKeyCredParams: [],
            },
          },
        },
      ],
      // /signup/passkey/finish must NEVER be called — if it is, the
      // unscripted-fetch throw surfaces as the failure we want.
    });
    installCredentials({
      create: () => {
        throw new DOMException("user dismissed", "NotAllowedError");
      },
    });

    try {
      render(
        <Harness
          state={{ signupSessionId: "ss_villa", desiredSlug: "villa-sud" }}
        />,
      );
      await flush();

      const name = screen.getByTestId("signup-enroll-name") as HTMLInputElement;
      fireEvent.change(name, { target: { value: "Maria" } });

      await act(async () => {
        fireEvent.click(screen.getByTestId("signup-enroll-submit"));
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();
      await flush();

      const notice = screen.getByTestId("signup-enroll-error");
      expect(notice.textContent).toContain("Passkey prompt closed");
      // Cancellation is `info` tone, not danger.
      expect(notice.className).not.toContain("login__notice--danger");
      // No finish call. No login call.
      expect(
        calls.some((c) => c.url.endsWith("/api/v1/signup/passkey/finish")),
      ).toBe(false);
      expect(
        calls.some((c) => c.url.endsWith("/api/v1/auth/passkey/login/start")),
      ).toBe(false);
      // Submit re-arms.
      const submit = screen.getByTestId("signup-enroll-submit") as HTMLButtonElement;
      expect(submit.disabled).toBe(false);
    } finally {
      restore();
    }
  });
});
