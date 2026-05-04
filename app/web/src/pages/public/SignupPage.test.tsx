// crewday — SignupPage component test.
//
// Pins the `/signup` surface against the documented `/signup/start`
// contract (§03 step 1). Mirrors the harness shape used by
// RecoverPage.test (scripted-fetch FIFO) — no shared helper module
// because the existing public-page suites all duplicate this scaffold
// on purpose so each file stays readable on its own.
//
// What this covers:
//   1. Happy path (202) — form + email + slug submit; confirmation
//      view replaces the form; focus pivots to the heading.
//   2. Closed deployment (404) — `/signup/*` returns 404 when
//      capabilities flip signup off; the SPA must surface the
//      "signups are closed" view rather than the generic fallback.
//   3. Slug 409 (`slug_taken`) with one-click suggestion adoption.
//   4. Slug 409 (`slug_reserved` / `slug_homoglyph_collision` /
//      `slug_in_grace_period`) — distinct copy per kind.
//   5. Turnstile — env-gated widget renders, threads captcha_token,
//      and resets after 422 captcha errors.
//   6. 429 rate-limit — danger notice; form stays visible.
//   7. 500 generic — fallback copy; form stays visible.
//   8. Concurrency guard — synchronous double-submit coalesces.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactElement } from "react";
import SignupPage from "./SignupPage";
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

function Harness(): ReactElement {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/signup"]}>
        <SignupPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function flush(): Promise<void> {
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

beforeEach(() => {
  __resetApiProvidersForTests();
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

interface CaptchaOptions {
  sitekey: string;
  callback: (token: string) => void;
  "expired-callback": () => void;
  "error-callback": () => void;
}

function installTurnstile(): {
  render: ReturnType<typeof vi.fn>;
  reset: ReturnType<typeof vi.fn>;
  remove: ReturnType<typeof vi.fn>;
  renders: CaptchaOptions[];
} {
  const renders: CaptchaOptions[] = [];
  const render = vi.fn((_container: HTMLElement, options: CaptchaOptions) => {
    renders.push(options);
    return "turnstile-widget-1";
  });
  const reset = vi.fn();
  const remove = vi.fn();
  vi.stubGlobal("turnstile", { render, reset, remove });
  return { render, reset, remove, renders };
}

// ── Tests ─────────────────────────────────────────────────────────

describe("<SignupPage> — happy path (202)", () => {
  it("submits email + slug, then renders the 'check your email' confirmation", async () => {
    const { calls, restore } = installFetch({
      "/api/v1/signup/start": [{ status: 202, body: { status: "accepted" } }],
    });

    try {
      render(<Harness />);

      expect(screen.getByText("Start your workspace")).toBeInTheDocument();
      const email = screen.getByTestId("signup-email") as HTMLInputElement;
      const slug = screen.getByTestId("signup-slug") as HTMLInputElement;
      fireEvent.change(email, { target: { value: "maria@example.com" } });
      fireEvent.change(slug, { target: { value: "Villa-Sud" } });

      await act(async () => {
        fireEvent.submit(email.closest("form")!);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();

      expect(calls).toHaveLength(1);
      expect(calls[0]!.init.method).toBe("POST");
      const sent = JSON.parse(calls[0]!.init.body as string) as Record<string, unknown>;
      // Slug is lowercased before send; email is trimmed (no change here).
      expect(sent).toEqual({
        email: "maria@example.com",
        desired_slug: "villa-sud",
      });

      // Confirmation view replaces the form — `role="status"` for
      // assistive tech, focus pivots to the heading.
      const sentPanel = screen.getByTestId("signup-sent");
      expect(sentPanel.getAttribute("role")).toBe("status");
      const heading = screen.getByText("Check your email");
      expect(heading).toBeInTheDocument();
      expect(document.activeElement).toBe(heading);
      // Form inputs are gone.
      expect(screen.queryByTestId("signup-email")).toBeNull();
      expect(screen.queryByTestId("signup-submit")).toBeNull();
    } finally {
      restore();
    }
  });
});

describe("<SignupPage> — closed deployment (404)", () => {
  it("renders the 'signups are closed' view when /signup/start 404s", async () => {
    const { restore } = installFetch({
      "/api/v1/signup/start": [
        { status: 404, body: { detail: "signups are disabled" } },
      ],
    });

    try {
      render(<Harness />);

      const email = screen.getByTestId("signup-email") as HTMLInputElement;
      fireEvent.change(email, { target: { value: "maria@example.com" } });
      fireEvent.change(screen.getByTestId("signup-slug"), { target: { value: "villa" } });

      await act(async () => {
        fireEvent.submit(email.closest("form")!);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();

      expect(screen.getByTestId("signup-closed")).toBeInTheDocument();
      expect(screen.getByText("Signups are closed")).toBeInTheDocument();
      // Form is replaced — the user can't keep submitting against a
      // capability-off deployment.
      expect(screen.queryByTestId("signup-email")).toBeNull();
    } finally {
      restore();
    }
  });
});

describe("<SignupPage> — slug 409 with suggestion", () => {
  it("renders the slug-taken notice and accepts the suggestion in one click", async () => {
    const { restore } = installFetch({
      "/api/v1/signup/start": [
        {
          status: 409,
          body: {
            detail: {
              error: "slug_taken",
              suggested_alternative: "villa-sud-2",
            },
          },
        },
      ],
    });

    try {
      render(<Harness />);

      const email = screen.getByTestId("signup-email") as HTMLInputElement;
      const slug = screen.getByTestId("signup-slug") as HTMLInputElement;
      fireEvent.change(email, { target: { value: "maria@example.com" } });
      fireEvent.change(slug, { target: { value: "villa-sud" } });

      await act(async () => {
        fireEvent.submit(email.closest("form")!);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();

      const notice = screen.getByTestId("signup-slug-error");
      expect(notice.textContent).toContain("already in use");
      const accept = screen.getByTestId("signup-slug-accept");
      expect(accept.textContent).toContain("villa-sud-2");

      // Click the suggestion → the slug input adopts the alt; the
      // notice disappears so the user can re-submit cleanly.
      fireEvent.click(accept);
      expect(slug.value).toBe("villa-sud-2");
      expect(screen.queryByTestId("signup-slug-error")).toBeNull();
    } finally {
      restore();
    }
  });
});

describe("<SignupPage> — slug 409 variants", () => {
  it.each([
    {
      kind: "slug_reserved",
      body: { detail: { error: "slug_reserved" } },
      copy: /reserved by crew\.day/i,
    },
    {
      kind: "slug_homoglyph_collision",
      body: {
        detail: { error: "slug_homoglyph_collision", colliding_slug: "vi11a" },
      },
      copy: /too close to an existing workspace.*vi11a/i,
    },
    {
      kind: "slug_in_grace_period",
      body: { detail: { error: "slug_in_grace_period" } },
      copy: /recently released/i,
    },
  ])("renders the right copy for $kind", async ({ body, copy }) => {
    const { restore } = installFetch({
      "/api/v1/signup/start": [{ status: 409, body }],
    });
    try {
      render(<Harness />);
      const email = screen.getByTestId("signup-email") as HTMLInputElement;
      fireEvent.change(email, { target: { value: "maria@example.com" } });
      fireEvent.change(screen.getByTestId("signup-slug"), {
        target: { value: "villa" },
      });
      await act(async () => {
        fireEvent.submit(email.closest("form")!);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();
      const notice = screen.getByTestId("signup-slug-error");
      expect(notice.textContent ?? "").toMatch(copy);
      // No suggestion button on these variants.
      expect(screen.queryByTestId("signup-slug-accept")).toBeNull();
    } finally {
      restore();
    }
  });
});

describe("<SignupPage> — 422 captcha_required", () => {
  it("surfaces the friendly fallback copy when no widget site key is configured", async () => {
    const { restore } = installFetch({
      "/api/v1/signup/start": [
        { status: 422, body: { detail: { error: "captcha_required" } } },
      ],
    });
    try {
      render(<Harness />);
      const email = screen.getByTestId("signup-email") as HTMLInputElement;
      fireEvent.change(email, { target: { value: "maria@example.com" } });
      fireEvent.change(screen.getByTestId("signup-slug"), {
        target: { value: "villa" },
      });
      await act(async () => {
        fireEvent.submit(email.closest("form")!);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();
      const notice = screen.getByTestId("signup-error");
      expect(notice.textContent).toMatch(/CAPTCHA/);
      // Form stays visible so the user can navigate to /login.
      expect(screen.getByTestId("signup-submit")).toBeInTheDocument();
    } finally {
      restore();
    }
  });
});

describe("<SignupPage> — Turnstile", () => {
  it("renders the widget when configured and sends captcha_token", async () => {
    vi.stubEnv("VITE_TURNSTILE_SITE_KEY", "site-key-123");
    const turnstile = installTurnstile();
    const { calls, restore } = installFetch({
      "/api/v1/signup/start": [{ status: 202, body: { status: "accepted" } }],
    });

    try {
      render(<Harness />);
      expect(screen.getByTestId("signup-turnstile")).toBeInTheDocument();
      expect(turnstile.render).toHaveBeenCalledTimes(1);
      expect(turnstile.renders[0]!.sitekey).toBe("site-key-123");

      await act(async () => {
        turnstile.renders[0]!.callback("captcha-token-1");
      });
      const email = screen.getByTestId("signup-email") as HTMLInputElement;
      fireEvent.change(email, { target: { value: "maria@example.com" } });
      fireEvent.change(screen.getByTestId("signup-slug"), {
        target: { value: "villa" },
      });
      await act(async () => {
        fireEvent.submit(email.closest("form")!);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();

      expect(calls).toHaveLength(1);
      const sent = JSON.parse(calls[0]!.init.body as string) as Record<string, unknown>;
      expect(sent).toEqual({
        email: "maria@example.com",
        desired_slug: "villa",
        captcha_token: "captcha-token-1",
      });
    } finally {
      restore();
    }
  });

  it.each(["captcha_required", "captcha_failed"])(
    "resets the widget and re-prompts after %s",
    async (error) => {
      vi.stubEnv("VITE_TURNSTILE_SITE_KEY", "site-key-123");
      const turnstile = installTurnstile();
      const { restore } = installFetch({
        "/api/v1/signup/start": [
          { status: 422, body: { detail: { error } } },
        ],
      });

      try {
        render(<Harness />);
        await act(async () => {
          turnstile.renders[0]!.callback("captcha-token-1");
        });
        const email = screen.getByTestId("signup-email") as HTMLInputElement;
        fireEvent.change(email, { target: { value: "maria@example.com" } });
        fireEvent.change(screen.getByTestId("signup-slug"), {
          target: { value: "villa" },
        });
        await act(async () => {
          fireEvent.submit(email.closest("form")!);
          await new Promise((r) => setTimeout(r, 0));
        });
        await flush();

        expect(turnstile.reset).toHaveBeenCalledWith("turnstile-widget-1");
        const notice = screen.getByTestId("signup-error");
        expect(notice.textContent).toMatch(/CAPTCHA check/i);
        expect(screen.getByTestId("signup-turnstile")).toBeInTheDocument();
      } finally {
        restore();
      }
    },
  );
});

describe("<SignupPage> — 429 rate-limit", () => {
  it("surfaces the rate-limit notice and re-arms the form", async () => {
    const { restore } = installFetch({
      "/api/v1/signup/start": [
        { status: 429, body: { detail: "Too many requests." } },
      ],
    });
    try {
      render(<Harness />);
      const email = screen.getByTestId("signup-email") as HTMLInputElement;
      fireEvent.change(email, { target: { value: "spammer@example.com" } });
      fireEvent.change(screen.getByTestId("signup-slug"), {
        target: { value: "villa" },
      });
      await act(async () => {
        fireEvent.submit(email.closest("form")!);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();
      const notice = screen.getByTestId("signup-error");
      expect(notice.textContent).toMatch(/Too many signup attempts/i);
      const submit = screen.getByTestId("signup-submit") as HTMLButtonElement;
      expect(submit.disabled).toBe(false);
      expect(screen.queryByTestId("signup-sent")).toBeNull();
    } finally {
      restore();
    }
  });
});

describe("<SignupPage> — 500 generic fallback", () => {
  it("surfaces the generic copy without leaking the server detail", async () => {
    const { restore } = installFetch({
      "/api/v1/signup/start": [
        { status: 500, body: { detail: "Boom: leaked stack trace" } },
      ],
    });
    try {
      render(<Harness />);
      const email = screen.getByTestId("signup-email") as HTMLInputElement;
      fireEvent.change(email, { target: { value: "maria@example.com" } });
      fireEvent.change(screen.getByTestId("signup-slug"), {
        target: { value: "villa" },
      });
      await act(async () => {
        fireEvent.submit(email.closest("form")!);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();
      const notice = screen.getByTestId("signup-error");
      expect(notice.textContent).toMatch(/couldn't reach the signup service/i);
      // Server detail must NOT bleed into UI copy.
      expect(notice.textContent ?? "").not.toContain("Boom");
    } finally {
      restore();
    }
  });
});

describe("<SignupPage> — concurrency guard", () => {
  it("coalesces a synchronous double-submit into a single request", async () => {
    const { calls, restore } = installFetch({
      "/api/v1/signup/start": [{ status: 202, body: { status: "accepted" } }],
    });
    try {
      render(<Harness />);
      const email = screen.getByTestId("signup-email") as HTMLInputElement;
      fireEvent.change(email, { target: { value: "maria@example.com" } });
      fireEvent.change(screen.getByTestId("signup-slug"), {
        target: { value: "villa" },
      });
      await act(async () => {
        const form = email.closest("form")!;
        fireEvent.submit(form);
        fireEvent.submit(form);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();
      const postCalls = calls.filter((c) => c.url.endsWith("/api/v1/signup/start"));
      expect(postCalls.length).toBe(1);
      expect(screen.getByTestId("signup-sent")).toBeInTheDocument();
    } finally {
      restore();
    }
  });
});
