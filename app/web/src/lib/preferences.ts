// Cookie-backed preferences shim — INCOMPLETE.
//
// Reads return sane defaults and writers are no-ops. cd-knp1 was the
// task that *should* have delivered the live impl alongside the
// context wiring; it closed without porting this file (acceptance
// criterion "WorkspaceContext persists + restores the active slug"
// missed). No follow-up Beads task exists yet — see TODO below.
//
// Symptom for the next agent: any code path that depends on a cookie
// preference (active workspace slug for `fetchJson` rewrites, theme,
// nav/agent-rail collapse) silently degrades. `WorkspaceProvider`
// boots with `workspaceId = null` until `WorkspaceGate` adopts a slug
// from `/api/v1/auth/me`; if that adoption fails (e.g. the
// id-vs-slug mismatch in `WorkspaceGate.currentSlug`), every API
// call goes to bare `/api/v1/...` and 404s.
//
// Reference impl: `mocks/web/src/lib/preferences.ts` (cookie reads,
// sendBeacon writes) — port that file plus the matching server
// endpoints (`/switch/<role>`, `/theme/set/<theme>`,
// `/workspaces/switch/<id>`, `/agent/sidebar/<state>`,
// `/nav/sidebar/<state>` — none currently mounted in `app/api`).
import type { Role, Theme } from "@/types/api";

export function readRoleCookie(): Role {
  return "manager";
}

export function readWorkspaceCookie(): string | null {
  return null;
}

export function readThemeCookie(): Theme {
  return "system";
}

export function readAgentCollapsedCookie(): boolean | null {
  return null;
}

export function initialAgentCollapsed(): boolean {
  return true;
}

export function readNavCollapsedCookie(): boolean | null {
  return null;
}

export function initialNavCollapsed(): boolean {
  return false;
}

export function persistRole(_role: Role): void {
  /* placeholder */
}

export function persistTheme(_theme: Theme): void {
  /* placeholder */
}

export function persistWorkspace(_workspaceId: string): void {
  /* placeholder */
}

export function persistAgentCollapsed(_state: "open" | "collapsed"): void {
  /* placeholder */
}

export function persistNavCollapsed(_state: "open" | "collapsed"): void {
  /* placeholder */
}
