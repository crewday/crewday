// crewday — shared post-login landing logic.
//
// Multiple public pages (LoginPage, EnrollPage, SignupVerifyPage,
// SignupEnrollPage) need to land a freshly-authenticated user on the
// right home for their grant role. Keeping the role → URL map in one
// place means a new role bucket (e.g. `client`, future `analyst`)
// updates one file, not four.
//
// LoginPage layers a `?next=…` priority + admin-path safeguard on top
// of this helper (see `pickLandingForLogin` in `LoginPage.tsx`); the
// other surfaces have no `next` to honour, so they call
// :func:`pickRoleLanding` directly.
//
// The map mirrors `RoleHome` in `App.tsx` and §14 "Role selector".

import type { AuthMe } from "./types";

export const ROLE_LANDING: Record<string, string> = {
  worker: "/today",
  client: "/portfolio",
  manager: "/dashboard",
  admin: "/dashboard",
  guest: "/",
};

/**
 * Pick the landing URL from the user's first available workspace
 * grant role. Returns `/` when no role signal is present —
 * `<RoleHome>` at the root then routes sensibly (or
 * `<WorkspaceGate>` renders the "no access yet" empty state).
 */
export function pickRoleLanding(user: AuthMe | null): string {
  const first = user?.available_workspaces?.[0];
  const role = first?.grant_role;
  if (role && ROLE_LANDING[role]) return ROLE_LANDING[role];
  return "/";
}
