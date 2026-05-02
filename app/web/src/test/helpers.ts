// crewday — shared Vitest fetch / response helpers.
//
// Hoisted from per-suite duplicates (cd-mpwm). Keeps lib tests on a
// pure-TS import path; JSX-only helpers live in `./render.tsx`.
//
// Three flavours of `installFetch` are exposed because the existing
// suites genuinely need different ergonomics:
//   - `installFetch(handler)` — full control: every request runs
//     through the caller's function. Use this when route logic depends
//     on body/method/query in non-trivial ways.
//   - `installFetchSequence(responses)` — FIFO list, one response per
//     call. Mirrors the lib/api / lib/expenses / lib/approvals shape.
//   - `installFetchRoutes(routes, options?)` — FIFO queue keyed by
//     pathname (default) or URL suffix. Mirrors the page-suite shape.
//
// Every flavour returns `{ calls, restore }` AND registers an
// `afterEach(restore)` so suites don't have to remember the cleanup.

import { afterEach, vi } from "vitest";

/** Captured fetch invocation. */
export interface FetchCall {
  url: string;
  init: RequestInit;
}

/** Minimal request shape passed to a custom fetch handler. */
export interface FetchRequest {
  url: string;
  init: RequestInit;
}

/** Response builder input — keep it permissive at the boundary. */
export interface FakeResponse {
  status?: number;
  body?: unknown;
}

/**
 * Build a JSON `Response` for use in fetch stubs. Defaults to 200.
 *
 * The shape is what `fetchJson` (and other consumers in this repo)
 * actually read: `ok`, `status`, `statusText`, and `text()`. We avoid
 * `json()` because some helpers explicitly call `text()` then parse.
 *
 * `body === undefined | null` → empty string text body so callers can
 * stub 204 / empty-error responses without ceremony.
 */
export function jsonResponse(body: unknown, status = 200): Response {
  const ok = status >= 200 && status < 300;
  const text =
    body === undefined || body === null
      ? ""
      : typeof body === "string"
        ? body
        : JSON.stringify(body);
  return {
    ok,
    status,
    statusText: ok ? "OK" : "Error",
    text: async () => text,
  } as unknown as Response;
}

function swapGlobalFetch(spy: typeof globalThis.fetch): () => void {
  const original = globalThis.fetch;
  (globalThis as { fetch: typeof fetch }).fetch = spy;
  return () => {
    (globalThis as { fetch: typeof fetch }).fetch = original;
  };
}

/**
 * Install a custom `fetch` handler. The handler receives `{ url, init }`
 * and returns (or resolves to) a `Response`. The shared `calls` array
 * captures every invocation in order so suites can assert on
 * URL / method / body without instrumenting the handler.
 *
 * Cleanup is auto-registered via `afterEach` — calling `restore()`
 * yourself is still fine (idempotent).
 */
export function installFetch(
  handler: (req: FetchRequest) => Response | Promise<Response>,
): { calls: FetchCall[]; restore: () => void } {
  const calls: FetchCall[] = [];
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const captured: FetchCall = { url: resolved, init: init ?? {} };
    calls.push(captured);
    return handler(captured);
  }) as unknown as typeof globalThis.fetch;

  let restored = false;
  const undo = swapGlobalFetch(spy);
  const restore = (): void => {
    if (restored) return;
    restored = true;
    undo();
  };
  afterEach(restore);
  return { calls, restore };
}

/**
 * Install a sequence-based fetch stub: the n-th call shifts the n-th
 * response off the array. Throws on overrun or unexpected calls so
 * tests fail loudly rather than silently re-using a stale fixture.
 *
 * This is the shape `lib/api.test.ts`, `lib/expenses.test.ts`, and the
 * passkey tests already use — only the data layer matters there, so a
 * scripted FIFO is enough.
 */
export function installFetchSequence(
  responses: FakeResponse[],
): { calls: FetchCall[]; restore: () => void } {
  const queue = [...responses];
  return installFetch(({ url }) => {
    const next = queue.shift();
    if (!next) throw new Error(`Unexpected fetch call: ${url}`);
    return jsonResponse(next.body, next.status ?? 200);
  });
}

/** How `installFetchRoutes` matches the route key against each request. */
export type RouteMatch = "pathname" | "endsWith";

/**
 * Install a route-table fetch stub. Each route key has its own FIFO
 * queue of responses.
 *
 * - `pathname` (default) — strict equality against the resolved URL's
 *   pathname (`/admin/api/v1/usage/summary`). List every pathname
 *   exactly; there is no prefix/longest-match magic. Best for admin
 *   tests where the workspace prefix isn't part of the URL.
 * - `endsWith` — match when the resolved URL ends with the key. Keys
 *   are sorted longest-first so a more specific suffix
 *   (`/api/v1/me/workspaces`) wins over a shorter one (`/api/v1/me`).
 *   Best when stubs are written as full path tails like
 *   `/api/v1/auth/passkey/login/start`.
 */
export function installFetchRoutes(
  routes: Record<string, FakeResponse[]>,
  options: { match?: RouteMatch } = {},
): { calls: FetchCall[]; restore: () => void } {
  const match = options.match ?? "pathname";
  const queues: Record<string, FakeResponse[]> = {};
  for (const [key, responses] of Object.entries(routes)) {
    queues[key] = [...responses];
  }
  // Longest-first sort only matters for `endsWith` mode (so the more
  // specific suffix wins). In `pathname` mode the lookup is strict
  // equality — the order is irrelevant but the sort is harmless.
  const keys = Object.keys(queues).sort((a, b) => b.length - a.length);

  return installFetch(({ url }) => {
    const target =
      match === "pathname"
        ? new URL(url, "http://crewday.test").pathname
        : url;
    const key =
      match === "pathname"
        ? keys.find((candidate) => target === candidate)
        : keys.find((candidate) => target.endsWith(candidate));
    if (!key) throw new Error(`Unscripted fetch: ${url}`);
    const next = queues[key]!.shift();
    if (!next) throw new Error(`No more responses for: ${url}`);
    return jsonResponse(next.body, next.status ?? 200);
  });
}
