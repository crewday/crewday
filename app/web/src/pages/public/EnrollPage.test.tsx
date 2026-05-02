// crewday — EnrollPage component test.
//
// Pins the `/recover/enroll` surface end-to-end against the real
// `@/auth` provider so a regression in the verify → ceremony →
// refresh → redirect chain (cd-3x3t) cannot ship silently.
//
// What this covers:
//   1. Verifying state — `verifyRecoveryToken` is in flight; the
//      "Checking your link…" view is announced via `role="status"`.
//   2. Ready state — verify resolves; the "Register passkey" button
//      renders with the documented test-id.
//   3. Happy path — clicking "Register passkey" runs the full
//      ceremony (start + navigator.credentials.create + finish), then
//      `refresh()` re-hydrates `/auth/me` and the page navigates to
//      the role landing.
//   4. Missing-token error — landing on `/recover/enroll` without a
//      `?token=...` query short-circuits to the error view with the
//      "Request a new link" affordance hidden (`canRetry: false`).
//   5. Expired-token error (410) — verify rejects with `ApiError(410)`;
//      the user lands on the error view WITH the "Request a new link"
//      link, since the link is replayable from `/recover`.
//   6. Enroll-failure error — start succeeds, the navigator throws a
//      `NotAllowedError` (user dismissed the prompt). The page stays
//      on the ready view but surfaces the inline `enroll-error`
//      notice; no navigation happens.
//
// Mirrors the harness in `RecoverPage.test.tsx` (scripted-fetch FIFO
// per URL suffix) plus the `installCredentialsGet` pattern from
// `LoginPage.test.tsx`. We don't reach for a shared helper module
// because none of the existing tests do — the duplication is small
// and kept local on purpose so each suite stays readable on its own.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactElement, type ReactNode } from "react";
import EnrollPage from "./EnrollPage";
import { AuthProvider, __resetAuthStoreForTests } from "@/auth";
import { __resetApiProvidersForTests } from "@/lib/api";

// ── Test harness ──────────────────────────────────────────────────

interface FakeResponse {
  status: number;
  body?: unknown;
}

/**
 * Scripted `fetch`. One FIFO queue per URL suffix so a multi-request
 * test (verify → start → finish → /me) can assert on order without
 * fighting a shared `responses[]`. Mirrors `RecoverPage.test.tsx`.
 */
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

/**
 * Duck-typed WebAuthn attestation that satisfies `encodeAttestation`'s
 * structural check. jsdom doesn't define `PublicKeyCredential`, so a
 * real instance is impossible here.
 */
function fakeAttestation(): Credential {
  const buf = (...vals: number[]): ArrayBuffer => new Uint8Array(vals).buffer;
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

/** Install a scripted `navigator.credentials.create`. Returns the spy. */
function installCredentialsCreate(
  behaviour: () => Promise<Credential> | Credential,
): ReturnType<typeof vi.fn> {
  const spy = vi.fn(async () => behaviour());
  const nav = globalThis.navigator as unknown as {
    credentials?: { create?: unknown };
  };
  if (!nav.credentials) {
    (nav as { credentials: unknown }).credentials = {};
  }
  (nav.credentials as { create: unknown }).create = spy;
  return spy;
}

function LocationProbe({ testid }: { testid: string }): ReactElement {
  const loc = useLocation();
  return <span data-testid={testid}>{loc.pathname + loc.search}</span>;
}

function Harness({
  initial,
  children,
}: {
  initial: string;
  children?: ReactNode;
}): ReactElement {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <AuthProvider>
          <Routes>
            <Route path="/recover/enroll" element={<EnrollPage />} />
            <Route path="/recover" element={<LocationProbe testid="landed-recover" />} />
            <Route path="/today" element={<LocationProbe testid="landed-today" />} />
            <Route path="/dashboard" element={<LocationProbe testid="landed-dashboard" />} />
            <Route path="*" element={<LocationProbe testid="landed-other" />} />
          </Routes>
          {children}
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

/** Flush a microtask so the bootstrap probe / fetch-then-fetch chain settles. */
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
  // Clear the navigator.credentials.create stub so the next test starts
  // from a clean slate.
  const nav = globalThis.navigator as unknown as { credentials?: { create?: unknown } };
  if (nav.credentials) delete (nav.credentials as { create?: unknown }).create;
});

// ── Tests ─────────────────────────────────────────────────────────

describe("<EnrollPage> — verifying state", () => {
  it("renders the 'Checking your link' view while verifyRecoveryToken is in flight", async () => {
    // verify never resolves; the `/auth/me` bootstrap probe is
    // independent and we still answer it so the AuthProvider settles
    // into `unauthenticated` (so the early Navigate-if-already-signed-
    // in branch doesn't fire).
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
      if (resolved.endsWith("/api/v1/recover/passkey/verify?token=tok-pending")) {
        return await new Promise<Response>((resolve) => {
          resolveVerify = resolve;
        });
      }
      throw new Error(`Unscripted fetch: ${resolved}`);
    });
    (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;

    try {
      render(<Harness initial="/recover/enroll?token=tok-pending" />);
      await flush();

      // Verifying view: `role="status"` + the documented copy.
      const status = screen.getByRole("status");
      expect(status.textContent).toContain("Checking your link");
      // The ready-state register button must NOT be in the DOM yet.
      expect(screen.queryByTestId("enroll-register")).toBeNull();
      // The error view must NOT be in the DOM either.
      expect(screen.queryByText("We couldn't use this link")).toBeNull();
    } finally {
      // Resolve the hanging verify so the React tree can unmount cleanly.
      resolveVerify?.({
        ok: true,
        status: 200,
        statusText: "OK",
        text: async () => JSON.stringify({ recovery_session_id: "rs_late" }),
      } as unknown as Response);
      await flush();
      (globalThis as { fetch: typeof fetch }).fetch = original;
    }
  });
});

describe("<EnrollPage> — ready state", () => {
  it("renders the 'Register passkey' button after the token verifies", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
      "/api/v1/recover/passkey/verify?token=tok-good": [
        { status: 200, body: { recovery_session_id: "rs_1" } },
      ],
    });

    try {
      render(<Harness initial="/recover/enroll?token=tok-good" />);
      await flush();

      const button = screen.getByTestId("enroll-register") as HTMLButtonElement;
      expect(button.textContent).toContain("Register passkey");
      expect(button.disabled).toBe(false);
      // No error notice while idle.
      expect(screen.queryByTestId("enroll-error")).toBeNull();
    } finally {
      restore();
    }
  });
});

describe("<EnrollPage> — happy path", () => {
  it("runs the full ceremony, calls refresh(), and navigates to the role landing", async () => {
    const { calls, restore } = installFetch({
      // Bootstrap probe before login: no session.
      "/api/v1/auth/me": [
        { status: 401, body: { detail: "no session" } },
        // Post-enrol refresh re-fetches /auth/me to hydrate the user.
        {
          status: 200,
          body: {
            user_id: "01HZ_USER",
            display_name: "Maria",
            email: "maria@example.com",
            available_workspaces: [
              {
                workspace: {
                  id: "ws_1",
                  name: "Villa Sud",
                  timezone: "UTC",
                  default_currency: "EUR",
                  default_country: "FR",
                  default_locale: "fr",
                },
                grant_role: "worker",
                binding_org_id: null,
                source: "workspace_grant",
              },
            ],
            current_workspace_id: null,
            is_deployment_admin: false,
          },
        },
      ],
      "/api/v1/recover/passkey/verify?token=tok-good": [
        { status: 200, body: { recovery_session_id: "rs_1" } },
      ],
      "/api/v1/recover/passkey/start": [
        { status: 200, body: { challenge_id: "ch_1", options: { challenge: "AQID", rp: { name: "crew.day" }, user: { id: "AQID", name: "u", displayName: "U" }, pubKeyCredParams: [] } } },
      ],
      "/api/v1/recover/passkey/finish": [
        {
          status: 200,
          body: {
            user_id: "01HZ_USER",
            credential_id: "cred_new",
            revoked_credential_count: 1,
            revoked_session_count: 2,
          },
        },
      ],
    });
    const createSpy = installCredentialsCreate(() => fakeAttestation());

    try {
      render(<Harness initial="/recover/enroll?token=tok-good" />);
      await flush();

      const button = screen.getByTestId("enroll-register") as HTMLButtonElement;
      await act(async () => {
        fireEvent.click(button);
        await new Promise((r) => setTimeout(r, 0));
      });
      // Three microtask flushes: (1) start fetch, (2) navigator.create
      // resolve, (3) finish fetch + refresh /auth/me + Navigate effect.
      await flush();
      await flush();
      await flush();

      // navigator.credentials.create was called exactly once.
      expect(createSpy).toHaveBeenCalledTimes(1);

      // Wire order: verify, start, finish, /auth/me (bootstrap was
      // already counted before the click).
      const after = calls.map((c) => c.url);
      const verifyIdx = after.findIndex((u) => u.endsWith("/api/v1/recover/passkey/verify?token=tok-good"));
      const startIdx = after.findIndex((u) => u.endsWith("/api/v1/recover/passkey/start"));
      const finishIdx = after.findIndex((u) => u.endsWith("/api/v1/recover/passkey/finish"));
      const meIdxs = after
        .map((u, i) => (u.endsWith("/api/v1/auth/me") ? i : -1))
        .filter((i) => i !== -1);
      expect(verifyIdx).toBeGreaterThanOrEqual(0);
      expect(startIdx).toBeGreaterThan(verifyIdx);
      expect(finishIdx).toBeGreaterThan(startIdx);
      // The post-enrol /auth/me refresh fires AFTER finish.
      expect(meIdxs.length).toBeGreaterThanOrEqual(2);
      expect(meIdxs[meIdxs.length - 1]!).toBeGreaterThan(finishIdx);

      // Worker role lands on /today.
      expect(screen.getByTestId("landed-today").textContent).toBe("/today");
    } finally {
      restore();
    }
  });
});

describe("<EnrollPage> — error branches", () => {
  it("renders the missing-token error view when ?token= is absent", async () => {
    const { calls, restore } = installFetch({
      "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
    });

    try {
      render(<Harness initial="/recover/enroll" />);
      await flush();

      // Error view rendered — no fetch to /verify ever fired.
      expect(screen.getByText("We couldn't use this link")).toBeInTheDocument();
      expect(screen.getByText(/missing its token/i)).toBeInTheDocument();
      // canRetry = false for missing-token, so the "Request a new
      // link" affordance is suppressed.
      expect(screen.queryByText("Request a new link")).toBeNull();
      // Verify endpoint was NEVER called — the empty-token guard is
      // the whole point of this branch.
      const verifyCalls = calls.filter((c) => c.url.includes("/api/v1/recover/passkey/verify"));
      expect(verifyCalls).toHaveLength(0);
      // The "Back to sign in" link survives so the user is not stranded.
      expect(screen.getByText("← Back to sign in")).toBeInTheDocument();
    } finally {
      restore();
    }
  });

  it("renders the expired-link error view with a 'Request a new link' affordance on 410", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
      "/api/v1/recover/passkey/verify?token=tok-stale": [
        {
          status: 410,
          body: { type: "gone", title: "Link expired", detail: "burned" },
        },
      ],
    });

    try {
      render(<Harness initial="/recover/enroll?token=tok-stale" />);
      await flush();
      await flush();

      expect(screen.getByText("We couldn't use this link")).toBeInTheDocument();
      expect(screen.getByText(/expired, already used, or invalid/i)).toBeInTheDocument();
      // canRetry = true → the "Request a new link" link is rendered
      // (anchor, not a button — the user follows it back to /recover).
      const retry = screen.getByText("Request a new link");
      expect(retry).toBeInTheDocument();
      expect(retry.closest("a")?.getAttribute("href")).toBe("/recover");
      // The register button must NOT render.
      expect(screen.queryByTestId("enroll-register")).toBeNull();
    } finally {
      restore();
    }
  });

  it("surfaces an inline notice when the ceremony fails (NotAllowedError)", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
      "/api/v1/recover/passkey/verify?token=tok-good": [
        { status: 200, body: { recovery_session_id: "rs_1" } },
      ],
      "/api/v1/recover/passkey/start": [
        { status: 200, body: { challenge_id: "ch_1", options: { challenge: "AQID", rp: { name: "crew.day" }, user: { id: "AQID", name: "u", displayName: "U" }, pubKeyCredParams: [] } } },
      ],
      // /finish must NEVER be called — if it is, the unscripted-fetch
      // throw will surface as the test failure we want.
    });
    installCredentialsCreate(() => {
      throw new DOMException("user dismissed", "NotAllowedError");
    });

    try {
      render(<Harness initial="/recover/enroll?token=tok-good" />);
      await flush();

      await act(async () => {
        fireEvent.click(screen.getByTestId("enroll-register"));
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();
      await flush();

      const notice = screen.getByTestId("enroll-error");
      expect(notice.textContent).toContain("Passkey prompt closed");
      // Cancellation is not a danger — info tone only.
      expect(notice.className).not.toContain("login__notice--danger");
      // Button re-arms so the user can retry.
      const button = screen.getByTestId("enroll-register") as HTMLButtonElement;
      expect(button.disabled).toBe(false);
      // No navigation happened — we're still on the enrol surface.
      expect(screen.queryByTestId("landed-today")).toBeNull();
      expect(screen.queryByTestId("landed-dashboard")).toBeNull();
    } finally {
      restore();
    }
  });
});
