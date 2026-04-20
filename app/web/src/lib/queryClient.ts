// TanStack Query v5 client factory. Spec §14 "Data layer" +
// "SSE-driven invalidation" + "Optimistic mutations" pin the defaults
// we expose here:
//
// - `staleTime: 30_000` — SSE drives freshness; the 30s window keeps
//   cheap navigations within a window from re-fetching on every
//   remount while still recovering quickly after a missed event.
// - `retry: 2` with exponential backoff — recover from transient
//   5xx / network blips without hammering the server. 4xx are
//   client-side bugs and skipped.
// - `retryDelay` exponential (500ms, 1s, 2s, capped at 30s).
// - `refetchOnWindowFocus: false` — SSE invalidates; focus-based
//   polling would double-fetch on tab-switch.
// - Mutations default `retry: 0` so an optimistic rollback is fired
//   exactly once on failure.

import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";

const QUERY_STALE_MS = 30_000;
const QUERY_GC_MS = 5 * 60_000;
const QUERY_RETRY_MAX = 2;
const RETRY_DELAY_BASE_MS = 500;
const RETRY_DELAY_MAX_MS = 30_000;

function statusOf(error: unknown): number | null {
  if (error instanceof ApiError) return error.status;
  // Some transports attach `.status` without inheriting ApiError
  // (e.g. custom middleware in tests); support both shapes so 4xx
  // bugs still skip the retry ladder.
  const maybe = (error as { status?: unknown } | null)?.status;
  return typeof maybe === "number" ? maybe : null;
}

function shouldRetry(failureCount: number, error: unknown): boolean {
  const status = statusOf(error);
  // 4xx is our own bug — retrying just hides it. 5xx and network
  // errors (no status) get up to QUERY_RETRY_MAX retries.
  if (status !== null && status >= 400 && status < 500) return false;
  return failureCount < QUERY_RETRY_MAX;
}

function retryDelay(attemptIndex: number): number {
  // attemptIndex is 0-based for the *next* attempt; 500ms → 1s → 2s.
  const delay = RETRY_DELAY_BASE_MS * 2 ** attemptIndex;
  return Math.min(delay, RETRY_DELAY_MAX_MS);
}

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: QUERY_STALE_MS,
        gcTime: QUERY_GC_MS,
        refetchOnWindowFocus: false,
        retry: shouldRetry,
        retryDelay,
      },
      mutations: { retry: 0 },
    },
  });
}
