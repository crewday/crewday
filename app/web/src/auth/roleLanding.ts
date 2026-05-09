// crewday — shared post-login landing logic.
//
// Multiple public pages (LoginPage, EnrollPage, SignupVerifyPage,
// SignupEnrollPage) need to land a freshly-authenticated user on the
// right workspace-prefixed home for their grant role. Keeping the
// role → URL map in one place means a new role bucket (e.g. `client`,
// future `analyst`) updates one file, not four.
//
// The map mirrors `RoleHome` in `App.tsx` and §14 "Role selector".

import type { AuthMe } from "./types";
import { workspaceRoute } from "@/lib/workspaceRoutes";
import type { AvailableWorkspace } from "@/types/auth";

export const ROLE_LANDING: Record<string, string> = {
  worker: "/today",
  client: "/portfolio",
  manager: "/dashboard",
  admin: "/dashboard",
  guest: "/",
};

const PUBLIC_BARE_PATHS = [
  "/accept",
  "/auth/magic",
  "/guest",
  "/healthz",
  "/login",
  "/readyz",
  "/recover",
  "/signup",
  "/styleguide",
  "/version",
];

export function landingForGrantRole(
  grantRole: string | null | undefined,
  workspaceSlug?: string | null,
): string {
  const routePath = baseLandingForGrantRole(grantRole);
  return workspaceRoute(workspaceSlug, routePath);
}

function baseLandingForGrantRole(grantRole: string | null | undefined): string {
  if (grantRole && ROLE_LANDING[grantRole]) return ROLE_LANDING[grantRole];
  return "/dashboard";
}

/**
 * Pick the default post-auth landing URL. Single-workspace users go
 * directly to their workspace-prefixed role home; multi-workspace
 * users stop at the bare-host chooser; zero-workspace users fall
 * through to `/` so `<WorkspaceGate>` can render the empty state.
 */
export function pickRoleLanding(user: AuthMe | null): string {
  const workspaces = user?.available_workspaces ?? [];
  if (workspaces.length === 0) return "/";
  if (workspaces.length > 1) return "/select-workspace";
  const workspace = workspaces[0];
  if (!workspace) return "/";
  return landingForWorkspace(workspace);
}

export function landingForWorkspace(workspace: AvailableWorkspace): string {
  return landingForGrantRole(workspace.grant_role, workspaceSlug(workspace));
}

export function pickLoginLanding(next: string | null, user: AuthMe | null): string {
  if (next) {
    const nextLanding = landingFromSafeNext(next, user);
    if (nextLanding) return nextLanding;
  }
  return pickRoleLanding(user);
}

export function workspaceSlug(workspace: AvailableWorkspace): string {
  return workspace.workspace.id;
}

function landingFromSafeNext(next: string, user: AuthMe | null): string | null {
  if (isAdminPath(next)) {
    return user?.is_deployment_admin === true ? next : null;
  }
  if (isWorkspacePrefixedPath(next)) {
    return workspaceSlugFromPath(next, user) ? next : null;
  }
  if (isPublicBarePath(next)) return next;
  const workspace = activeWorkspace(user);
  if (!workspace) return null;
  return workspaceRoute(workspaceSlug(workspace), next);
}

function activeWorkspace(user: AuthMe | null): AvailableWorkspace | null {
  const workspaces = user?.available_workspaces ?? [];
  if (workspaces.length === 0) return null;
  const current = user?.current_workspace_id;
  if (current) {
    const match = workspaces.find((workspace) =>
      workspace.workspace_id === current || workspaceSlug(workspace) === current,
    );
    if (match) return match;
  }
  return workspaces.length === 1 ? (workspaces[0] ?? null) : null;
}

function isAdminPath(path: string): boolean {
  return path === "/admin" || path.startsWith("/admin/")
    || path.startsWith("/admin?") || path.startsWith("/admin#");
}

function isWorkspacePrefixedPath(path: string): boolean {
  return path === "/w" || path.startsWith("/w/");
}

function workspaceSlugFromPath(path: string, user: AuthMe | null): string | null {
  const match = /^\/w\/([^/?#]+)/.exec(path);
  if (!match?.[1]) return null;
  let slug: string;
  try {
    slug = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  const workspaces = user?.available_workspaces ?? [];
  const allowed = workspaces.some((workspace) => workspaceSlug(workspace) === slug);
  return allowed ? slug : null;
}

function isPublicBarePath(path: string): boolean {
  if (path === "/") return true;
  return PUBLIC_BARE_PATHS.some((prefix) =>
    path === prefix || path.startsWith(`${prefix}/`)
      || path.startsWith(`${prefix}?`) || path.startsWith(`${prefix}#`),
  );
}
