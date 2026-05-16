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

import { MutationCache, QueryCache, QueryClient, type Mutation, type Query } from "@tanstack/react-query";
import { ApiError, toDisplayError, type DisplayError } from "@/lib/api";
import { publishGlobalErrorToast, type ErrorToastEvent } from "@/lib/errorToastBus";

const QUERY_STALE_MS = 30_000;
const QUERY_GC_MS = 5 * 60_000;
const QUERY_RETRY_MAX = 2;
const RETRY_DELAY_BASE_MS = 500;
const RETRY_DELAY_MAX_MS = 30_000;

interface QueryClientFactoryOptions {
  onErrorToast?: (event: ErrorToastEvent) => void;
}

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

export function makeQueryClient(options: QueryClientFactoryOptions = {}): QueryClient {
  const onErrorToast = options.onErrorToast ?? publishGlobalErrorToast;
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error, query) => {
        const displayError = displayErrorForToast(error);
        if (!displayError || !shouldToastQueryError(error, displayError, query)) return;
        onErrorToast({ error: displayError, source: "query" });
      },
    }),
    mutationCache: new MutationCache({
      onError: (error, _variables, _context, mutation) => {
        const displayError = displayErrorForToast(error);
        if (!displayError || !shouldToastMutationError(error, displayError, mutation)) return;
        onErrorToast({ error: displayError, source: "mutation" });
      },
    }),
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

function displayErrorForToast(error: unknown): DisplayError | null {
  if (isAbortError(error)) return null;
  return toDisplayError(error);
}

function shouldToastQueryError(
  error: unknown,
  displayError: DisplayError,
  query: Query<unknown, unknown, unknown, readonly unknown[]>,
): boolean {
  if (suppressesGlobalErrorToast(query.meta)) return false;
  if (!hasBackgroundData(query)) return false;
  return shouldToastDisplayError(error, displayError);
}

function shouldToastMutationError(
  error: unknown,
  displayError: DisplayError,
  mutation: Mutation<unknown, unknown, unknown, unknown>,
): boolean {
  if (suppressesGlobalErrorToast(mutation.meta)) return false;
  if (typeof mutation.options.onError === "function") return false;
  return shouldToastDisplayError(error, displayError);
}

function shouldToastDisplayError(error: unknown, displayError: DisplayError): boolean {
  if (statusOf(error) === 401) return false;
  if (displayError.fieldErrors.length > 0) return false;
  return true;
}

function hasBackgroundData(query: Query<unknown, unknown, unknown, readonly unknown[]>): boolean {
  return query.state.data !== undefined;
}

function suppressesGlobalErrorToast(meta: unknown): boolean {
  if (typeof meta !== "object" || meta === null) return false;
  const record = meta as Record<string, unknown>;
  return record.suppressErrorToast === true || record.errorToast === false;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
