/**
 * Canonical authenticated-routes manifest for the production SPA.
 *
 * This file is the single source of truth for the SPA's authenticated
 * sitemap. It is consumed by two surfaces:
 *
 * 1. The production Vite build, which serialises this list into
 *    `dist/_surface.json` (see the `crewday:emit-surface-manifest`
 *    plugin in `app/web/vite.config.ts`). Downstream tooling reads
 *    that JSON instead of parsing the `App.tsx` JSX tree.
 * 2. The 360 px viewport sitemap walker
 *    (`tests/e2e/_helpers/sitemap.py`), which loads
 *    `dist/_surface.json` at test time and walks every authenticated
 *    route per `docs/specs/17-testing-quality.md` §"360 px viewport
 *    sitemap".
 *
 * Note: this is distinct from `cli/crewday/_surface.json`, which
 * describes the HTTP / CLI surface — they are separate artefacts
 * with different schemas and different consumers.
 *
 * Inclusion rules (v1):
 *
 * - Every entry is an SPA path nested under the `<Route element=
 *   {<RequireAuth />}>` block in `App.tsx`. `/admin/...` paths are
 *   authenticated but live outside `<WorkspaceGate>`; they are still
 *   in scope for the walker.
 * - `<Navigate>` redirects (`/week`, `/me/schedule`, `/bookings`,
 *   `/shifts`, `/`, `/admin`, `/admin/signup`) are excluded — they
 *   are not pages.
 * - Routes with path parameters (`/task/:tid`, `/asset/:aid`,
 *   `/property/:pid`, `/employee/:eid`, etc.) are excluded for v1
 *   because the e2e walker has no seed data to satisfy the
 *   parameters. `/w/:slug/...` duplicates of parameter-free routes
 *   are likewise omitted because the parameter-free variant already
 *   exercises the same page.
 *
 * The manifest is hand-maintained for v1. If drift becomes a
 * recurring problem, a follow-up Beads task can add an AST-based
 * check that compares this list to the JSX in `App.tsx`.
 */

export const AUTHENTICATED_ROUTES = [
  // Shared (any role; the Shell layout picks the right chrome).
  "/today",
  "/schedule",
  "/my/expenses",
  "/me",
  "/scheduler",
  "/history",
  "/issues/new",

  // Worker-only surfaces.
  "/chat",
  "/asset/scan",

  // Manager-gated pages.
  "/approvals",
  "/dashboard",
  "/leaves",
  "/settings",
  "/webhooks",
  "/chat/channels",
  "/pay",
  "/stays",
  "/instructions",
  "/properties",
  "/documents",
  "/assets",
  "/inventory",
  "/asset_types",
  "/organizations",
  "/employees",
  "/expenses",
  "/templates",
  "/schedules",
  "/permissions",
  "/tokens",
  "/audit",

  // Client-portal pages.
  "/portfolio",
  "/billable_hours",
  "/quotes",
  "/invoices",

  // Deployment-admin shell (inside RequireAuth, outside WorkspaceGate).
  "/admin/dashboard",
  "/admin/chat-gateway",
  "/admin/llm",
  "/admin/agent-docs",
  "/admin/usage",
  "/admin/workspaces",
  "/admin/signups",
  "/admin/settings",
  "/admin/admins",
  "/admin/audit",
] as const;

export type AuthenticatedRoute = (typeof AUTHENTICATED_ROUTES)[number];
