import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useWorkspace } from "@/context/WorkspaceContext";
import { getAuthState, subscribeAuth } from "@/auth";
import { connectEventStream, type SseStatus } from "@/lib/sse";

// §14 "SSE-driven invalidation" — one `EventSource('/w/${slug}/events')`
// per active workspace. Re-established on workspace switch (and on
// transport drops, via exponential backoff handled inside
// `connectEventStream`). When no workspace is picked yet (pre-/me, or
// the user hasn't chosen one), we fall back to `/events` so the server
// can still push workspace-agnostic events (e.g. onboarding, admin)
// before the SPA knows its tenant.
//
// The transport is only opened while the user is authenticated. A
// logout flips `useAuth().isAuthenticated` to `false`, which tears
// down this effect and closes the underlying `EventSource` — keeping
// the §"Logout clears storage + closes SSE" acceptance criterion
// honest without `SseContext` knowing about cookies or storage.
//
// Message dispatch (query invalidation, `setQueryData` fan-out, agent
// typing flag) lives in `@/lib/sse`. This provider only owns the
// lifecycle of the transport + the React-facing status state that
// `useSseConnection` exposes to reconnect badges.

interface SseCtxValue {
  status: SseStatus;
  lastEventId: string | null;
}

const SseCtx = createContext<SseCtxValue | null>(null);

export function SseProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const { workspaceId } = useWorkspace();
  // Read directly from the auth store rather than `useAuth()` so this
  // provider doesn't drag a `useNavigate()` dependency into trees
  // that legitimately mount it without a `<Router>` (the unit tests
  // for the SSE lifecycle, for one).
  const authStatus = useSyncExternalStore(
    subscribeAuth,
    () => getAuthState().status,
    () => getAuthState().status,
  );

  const [status, setStatus] = useState<SseStatus>("closed");
  const [lastEventId, setLastEventId] = useState<string | null>(null);

  // Keep setters in a ref so the effect's identity is stable against
  // parent re-renders that produce new setter references. React's
  // `useState` setters are already referentially stable, but threading
  // through a ref makes the callbacks we hand to `connectEventStream`
  // stable across mounts too — and keeps the effect's dep list honest.
  const statusRef = useRef(setStatus);
  statusRef.current = setStatus;
  const lastIdRef = useRef(setLastEventId);
  lastIdRef.current = setLastEventId;

  useEffect(() => {
    if (typeof EventSource === "undefined") {
      setStatus("closed");
      return;
    }
    // Only open the stream once auth is positively resolved. On the
    // unauthenticated leg the user is bouncing between /login and the
    // protected tree; opening a stream the server will refuse anyway
    // is wasted reconnect chatter (and would leak a transport across
    // a logout). The `'loading'` leg also defers — the bootstrap
    // probe usually settles in a single tick.
    if (authStatus !== "authenticated") {
      setStatus("closed");
      return;
    }

    // A workspace switch is a fresh stream on the server (different
    // workspace_id, different sequence). Drop the cached last-event
    // id so the new connection doesn't carry a stale reference.
    setLastEventId(null);

    const conn = connectEventStream({
      slug: workspaceId,
      qc,
      onStatus: (next) => statusRef.current(next),
      onLastEventId: (id) => lastIdRef.current(id),
    });

    return () => {
      conn.close();
    };
  }, [qc, workspaceId, authStatus]);

  const value = useMemo<SseCtxValue>(
    () => ({ status, lastEventId }),
    [status, lastEventId],
  );

  return <SseCtx.Provider value={value}>{children}</SseCtx.Provider>;
}

/**
 * Subscribe to the live SSE connection state.
 *
 * Returns `{ status, lastEventId }` so components (e.g. the header
 * reconnect badge) can reflect transport health without reaching
 * into the transport itself.
 */
export function useSseConnection(): SseCtxValue {
  const v = useContext(SseCtx);
  // Fall back to a closed/null state when the hook is called outside
  // `<SseProvider>`. Throwing would make the hook unusable in
  // storybook / styleguide / shallow tests that don't mount the
  // full provider tree; the fallback is a safe no-op ("no live
  // stream").
  if (!v) return { status: "closed", lastEventId: null };
  return v;
}
