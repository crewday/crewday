/**
 * Workspace-relative authenticated-routes manifest for the production SPA.
 *
 * This file is the sitemap subset of the named frontend route manifest.
 * It is consumed by two surfaces:
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
 * Workspace routes are stored without `/w/<slug>` so the e2e walker
 * can start from stable workspace-relative paths and let the
 * authenticated app redirect them to the active seed workspace. The
 * app's canonical emitted URLs still carry `/w/<slug>/...`; admin
 * routes stay bare because the admin shell is deployment-scope.
 *
 * Note: this is distinct from both `dist/_routes.json` and
 * `cli/crewday/_surface.json`. `dist/_routes.json` is the named
 * frontend route vocabulary for agent links. `cli/crewday/_surface.json`
 * describes the HTTP / CLI operation surface generated from OpenAPI.
 * They are separate artefacts with different schemas and consumers.
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
 *   parameters. Workspace entries are the route paths below
 *   `/w/<slug>/`; the live app canonicalizes them to the active
 *   workspace during the walk.
 *
 * The underlying named manifest is hand-maintained for v1. If drift
 * becomes a recurring problem, a follow-up Beads task can add an
 * AST-based check that compares it to the JSX in `App.tsx`.
 */

import { FRONTEND_ROUTES } from "./_manifest";

export const AUTHENTICATED_ROUTES: readonly string[] = FRONTEND_ROUTES.filter(
  (route) => route.authenticatedSurface,
).map((route) => route.template);

export type AuthenticatedRoute = (typeof AUTHENTICATED_ROUTES)[number];
