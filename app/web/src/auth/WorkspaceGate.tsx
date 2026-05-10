import { Link, Navigate, Outlet, useLocation } from "react-router-dom";
import { useCallback, useEffect, useMemo, useRef } from "react";
import { useAuth } from "./useAuth";
import { landingForWorkspace, workspaceSlug } from "./roleLanding";
import { useWorkspace } from "@/context/WorkspaceContext";
import WorkspacePickList from "@/components/WorkspacePickList";
import { workspaceRelativePathname, workspaceRoute, workspaceSlugFromRoutePath } from "@/lib/workspaceRoutes";
import type { AvailableWorkspace } from "@/types/auth";

// §14 "Workspace selector" — when the caller is authenticated but
// hasn't picked a workspace yet (no `crewday_workspace` cookie set
// on this device, or the cookie was for a workspace they no longer
// belong to), block the protected tree behind a chooser.
//
// Three branches:
//
//   1. Single workspace → adopt it silently. The user never sees the
//      chooser; spec §14 explicitly says "users with exactly one
//      workspace skip this page". We do the adoption here rather
//      than at /select-workspace so a fresh user landing on a
//      /w/<slug>/... deep-link doesn't bounce through an extra screen.
//   2. Multiple workspaces → render the chooser as a modal-style
//      surface above the protected tree. Selecting one writes the
//      cookie via `setWorkspaceId`; the protected tree mounts on
//      the next render.
//   3. Zero workspaces → render the "no access yet" empty state. The
//      user is logged in but has no live grants — usually a brand-
//      new account whose first invite hasn't been redeemed. They
//      can sign out to switch identity.
//
// Public routes (login, recover, accept) are **not** wrapped with
// this component — they don't need a workspace. The router places
// `<WorkspaceGate>` inside the protected branch only.

export function WorkspaceGate({
  children,
  forcePicker = false,
}: {
  children?: React.ReactNode;
  forcePicker?: boolean;
}) {
  // code-health: ignore[ccn nloc] Workspace gate keeps single-workspace adoption, multi-workspace chooser, and no-access empty state in one guard.
  const { user, logout } = useAuth();
  const { workspaceId, setWorkspaceId, clearWorkspaceId } = useWorkspace();
  const location = useLocation();
  const routeSlug = workspaceSlugFromRoutePath(location.pathname);
  // Focused on mount so keyboard users (and screen-reader users on a
  // JAWS / NVDA "forms mode" switch) land inside the dialog rather
  // than in the page chrome beneath. We target the first pickable
  // action (workspace pick, admin deep-link, or sign-out) — the
  // dialog itself stays non-tabbable so Tab / Shift+Tab move through
  // the picks. The ref is widened to `HTMLElement` so it can hold
  // both the `<button>` picks and the `<a>` rendered by
  // `<Link to="/admin/dashboard">` (the admin deep-link surfaced for
  // deployment admins on the empty state).
  const firstActionRef = useRef<HTMLElement | null>(null);
  const setFirstAction = useCallback((node: HTMLElement | null): void => {
    firstActionRef.current = node;
  }, []);

  const available = useMemo(
    () => user?.available_workspaces ?? [],
    [user?.available_workspaces],
  );
  const shouldForcePicker = forcePicker && available.length > 1;
  const selectedWorkspaceSlug = useMemo(() => {
    return activeWorkspaceSlug(available, {
      workspaceId,
    });
  }, [available, workspaceId]);
  const hasValidWorkspace = workspaceId !== null && selectedWorkspaceSlug !== null;

  useEffect(() => {
    if (hasValidWorkspace && !shouldForcePicker) return;
    firstActionRef.current?.focus({ preventScroll: true });
  }, [hasValidWorkspace, shouldForcePicker, user?.available_workspaces?.length, user?.is_deployment_admin]);

  const onlySlug = useMemo(() => {
    if (available.length !== 1) return null;
    const w = available[0];
    return w ? workspaceSlug(w) : null;
  }, [available]);
  const currentSlug = useMemo(() => {
    if (selectedWorkspaceSlug) return selectedWorkspaceSlug;
    return activeWorkspaceSlug(available, {
      workspaceId: user?.current_workspace_id ?? null,
    });
  }, [available, selectedWorkspaceSlug, user?.current_workspace_id]);

  useEffect(() => {
    if (routeSlug) return;
    if (workspaceId === null) return;
    if (hasValidWorkspace) return;
    clearWorkspaceId();
  }, [clearWorkspaceId, hasValidWorkspace, routeSlug, workspaceId]);

  // Auto-adopt for single-workspace users. Runs as an effect so the
  // store update happens outside render (avoids the
  // "setState-during-render" warning) but before the protected tree
  // commits — `setWorkspaceId` triggers a synchronous re-render via
  // the `WorkspaceContext`, and the next pass sees `workspaceId !== null`.
  useEffect(() => {
    if (shouldForcePicker) return;
    if (routeSlug && !hasValidWorkspace) return;
    if (hasValidWorkspace) return;
    if (!onlySlug) return;
    setWorkspaceId(onlySlug);
  }, [hasValidWorkspace, routeSlug, shouldForcePicker, onlySlug, setWorkspaceId]);

  // Server already picked a workspace for this session (cookie was
  // set by the login handler) — surface it without forcing the user
  // through the chooser. The auth-me probe carries
  // `current_workspace_id` exactly so this no-op adoption can happen
  // without a follow-up call.
  useEffect(() => {
    if (shouldForcePicker) return;
    if (routeSlug && !hasValidWorkspace) return;
    if (hasValidWorkspace) return;
    if (!currentSlug) return;
    setWorkspaceId(currentSlug);
  }, [currentSlug, hasValidWorkspace, routeSlug, shouldForcePicker, setWorkspaceId]);

  if (hasValidWorkspace && !shouldForcePicker) return <>{children ?? <Outlet />}</>;

  if (routeSlug) {
    const targetSlug = currentSlug ?? onlySlug;
    if (targetSlug && targetSlug !== routeSlug) {
      return (
        <Navigate
          to={workspaceRoute(targetSlug, workspaceRelativePathname(location.pathname), {
            search: location.search,
            hash: location.hash,
          })}
          replace
        />
      );
    }
  }

  if (!shouldForcePicker && (currentSlug || onlySlug)) return null;

  // From here we know no valid workspace is selected. Render the chooser
  // (or the empty state) instead of the protected tree.

  if (available.length === 0) {
    // Deployment admins with zero workspace grants would otherwise
    // dead-end on this empty state — they can't reach the workspace-
    // scoped `/api/v1/me` that drives the manager-nav "Administration"
    // link, so the only way to /admin/dashboard would be to type the
    // URL by hand. Surface the deep-link as a primary action when the
    // bare-host /auth/me carries `is_deployment_admin: true`. Sign-out
    // stays available as the secondary action; the route is already
    // gated by `<RequireAuth>` outside `<WorkspaceGate>` so this is
    // pure UX polish, not a new permission edge.
    const isAdmin = user?.is_deployment_admin === true;
    return (
      <div className="auth-gate" role="dialog" aria-modal="true" aria-labelledby="auth-gate-title">
        <div className="auth-gate__panel">
          <h1 id="auth-gate-title" className="auth-gate__title">No workspaces yet</h1>
          <p className="auth-gate__sub">
            You're signed in as <strong>{user?.display_name ?? user?.email ?? "this account"}</strong>,
            but you don't have access to any workspaces. Ask your manager to send you an invite,
            or open the link they already sent.
          </p>
          <div className="auth-gate__actions btn-group btn-group--stack">
            {isAdmin && (
              <Link
                ref={setFirstAction}
                to="/admin/dashboard"
                className="btn btn--moss"
              >
                Open admin console
              </Link>
            )}
            <button
              ref={isAdmin ? undefined : setFirstAction}
              type="button"
              className="btn"
              onClick={() => { void logout(); }}
            >
              Sign out
            </button>
          </div>
        </div>
      </div>
    );
  }

  // 2+ workspaces: pick one. Hold-pattern style matches `<RequireAuth>`'s
  // loading state so the transition between the two doesn't flash.
  return (
    <div className="auth-gate" role="dialog" aria-modal="true" aria-labelledby="auth-gate-title">
      <div className="auth-gate__panel">
        <h1 id="auth-gate-title" className="auth-gate__title">Pick a workspace</h1>
        <p className="auth-gate__sub">
          You have access to {available.length} workspaces. Choose one to continue.
        </p>
        <WorkspacePickList
          workspaces={available}
          activeWorkspaceSlug={currentSlug}
          className="auth-gate__list"
          label="Available workspaces"
          setFirstAction={setFirstAction}
          toForWorkspace={landingForWorkspace}
          onPick={(workspace) => setWorkspaceId(workspaceSlug(workspace))}
        />
        <div className="auth-gate__actions">
          <button type="button" className="btn" onClick={() => { void logout(); }}>
            Sign out
          </button>
        </div>
      </div>
    </div>
  );
}

function activeWorkspaceSlug(
  available: AvailableWorkspace[],
  current: { workspaceId: string | null },
): string | null {
  if (!current.workspaceId) return null;
  const match = available.find((w) => {
    const slug = workspaceSlug(w);
    return w.workspace_id === current.workspaceId || slug === current.workspaceId;
  });
  return match ? workspaceSlug(match) : null;
}

export default WorkspaceGate;
