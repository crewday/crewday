import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useEffect, useRef } from "react";
import { useAuth } from "./useAuth";
import { sanitizeNext } from "./onUnauthorized";
import { configuredPublicSiteUrl } from "@/lib/runtimeConfig";

function defaultExternalRedirect(url: string): void {
  window.location.replace(url);
}

// §14 "Auth" — route guard that defers child rendering until the
// auth store has resolved. Three terminal states:
//
//   - `loading`        → render the holding pattern (no redirect yet).
//   - `unauthenticated`→ `<Navigate to="/login?next=...">`.
//   - `authenticated`  → `<Outlet />` (children mount).
//
// Public routes (login, recover, accept invite, guest, signup) are
// **not** wrapped with this component in `App.tsx`; they live in
// their own `<Route element={<PublicLayout />}>` branch and render
// regardless of session state. Centralising the whitelist here would
// duplicate the router config — better to gate at the route level.
//
// The `next` query parameter survives the bounce: a deep-link to
// `/property/abc?tab=tasks` becomes `/login?next=%2Fproperty%2Fabc%3Ftab%3Dtasks`,
// and the LoginPage replays it on success. The encoded value goes
// through `sanitizeNext()` so protocol-ish / off-origin inputs are
// dropped before the bounce — a defence-in-depth guard that matches
// the central 401 handler's posture.

export function RequireAuth({
  children,
  redirectExternal = defaultExternalRedirect,
}: {
  children?: React.ReactNode;
  redirectExternal?: (url: string) => void;
}) {
  const { status } = useAuth();
  const location = useLocation();

  if (status === "loading") {
    // Minimal hold pattern. The styles live in globals.css under
    // `.auth-hold` so any future redesign (skeleton, spinner, animated
    // wordmark) is one CSS edit away — the component contract stays
    // "render *something* without flashing /login or the chrome".
    return (
      <output className="auth-hold" aria-live="polite" aria-busy="true">
        <span className="auth-hold__label">Checking your session…</span>
      </output>
    );
  }

  if (status === "unauthenticated") {
    const publicSiteUrl = location.pathname === "/" ? configuredPublicSiteUrl() : null;
    if (publicSiteUrl) {
      return <ExternalRedirect to={publicSiteUrl} redirectExternal={redirectExternal} />;
    }
    const here = location.pathname + location.search + location.hash;
    const safeHere = sanitizeNext(here);
    const target = safeHere ? `/login?next=${encodeURIComponent(safeHere)}` : "/login";
    return <Navigate to={target} replace />;
  }

  // Authenticated. Two integration shapes are supported:
  //
  //   <Route element={<RequireAuth />}>...children routes...</Route>   → Outlet
  //   <RequireAuth><MyComponent/></RequireAuth>                         → children
  //
  // The router-level form is what `App.tsx` uses; the props form is
  // there for ad-hoc protected widgets that don't sit on a route.
  return <>{children ?? <Outlet />}</>;
}

export default RequireAuth;

function ExternalRedirect({
  to,
  redirectExternal,
}: {
  to: string;
  redirectExternal: (url: string) => void;
}) {
  const redirectedRef = useRef<string | null>(null);
  useEffect(() => {
    if (redirectedRef.current === to) return;
    redirectedRef.current = to;
    redirectExternal(to);
  }, [redirectExternal, to]);
  return (
    <output className="auth-hold" aria-live="polite" aria-busy="true">
      <span className="auth-hold__label">Opening crew.day…</span>
    </output>
  );
}
