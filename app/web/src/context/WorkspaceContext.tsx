import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { persistWorkspace, readWorkspaceCookie } from "@/lib/preferences";
import { registerWorkspaceSlugGetter } from "@/lib/api";
import { registerQueryKeyWorkspaceGetter } from "@/lib/queryKeys";

// §02 — active workspace context. Server is authoritative via the
// `crewday_workspace` cookie; this hook mirrors it so the UI can
// react synchronously to a switch without waiting for /me to
// re-fetch. Switching invalidates every workspace-scoped query so
// the next paint reflects the new tenant.
//
// The stored value is the workspace **slug** (used in URLs and query
// keys). The prop name is `workspaceId` for historical reasons; the
// spec (§14 "Data layer") uses "slug" throughout. This provider also
// wires `lib/api` and `lib/queryKeys` to read the slug lazily so
// every fetch and every cache entry stays slug-namespaced without
// threading the value through every call-site.

interface WorkspaceCtx {
  workspaceId: string | null;
  setWorkspaceId: (wsid: string) => void;
}

const Ctx = createContext<WorkspaceCtx | null>(null);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [workspaceId, setWorkspaceIdState] = useState<string | null>(() => readWorkspaceCookie());
  const queryClient = useQueryClient();

  // Keep a ref the module-scope getters read from, so a slug switch is
  // visible to `fetchJson` and `qk.*` on the very next call without
  // having to re-register. Updated during render — React runs the
  // parent's body before children render, so every child's first
  // `useQuery({ queryKey: qk.*() })` and every `fetchJson` call below
  // this provider reads the correct slug, including on the very first
  // mount when the cookie already resolves to a tenant.
  const slugRef = useRef<string | null>(workspaceId);
  slugRef.current = workspaceId;

  // Register synchronously (not in `useEffect`) so children mounting
  // below this provider see the getter *before* their render fires
  // `qk.*()` or `fetchJson` — otherwise the first render would cache
  // the query under `["w", "_", ...]` while the fetch itself resolves
  // to `/w/<slug>/...`, leaving subsequent SSE invalidations unable to
  // match the stranded cache entry. `registeredRef` keeps this a
  // one-time side effect so concurrent rerenders don't re-install the
  // same getter every frame.
  const registeredRef = useRef(false);
  if (!registeredRef.current) {
    const getter = (): string | null => slugRef.current;
    registerWorkspaceSlugGetter(getter);
    registerQueryKeyWorkspaceGetter(getter);
    registeredRef.current = true;
  }

  const setWorkspaceId = useCallback((wsid: string) => {
    setWorkspaceIdState(wsid);
    persistWorkspace(wsid);
    // Drop every cached entry — every query is potentially scoped to
    // the previous workspace. /me will re-fetch with the new context.
    queryClient.invalidateQueries();
  }, [queryClient]);

  const value = useMemo(() => ({ workspaceId, setWorkspaceId }), [workspaceId, setWorkspaceId]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWorkspace(): WorkspaceCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useWorkspace must be used inside <WorkspaceProvider>");
  return v;
}
