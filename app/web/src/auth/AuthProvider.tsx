import { type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { useAuthBootstrap } from "./useAuth";

// Thin wrapper that runs the one-shot `useAuthBootstrap()` effect.
// Mounted once at the app root (between `<BrowserRouter>` and the
// other providers) so the auth store + 401 handler are wired before
// any protected route mounts. `/styleguide` is the one exception: it is
// a public dev/staging visual baseline, so it must not emit the
// unauthenticated `/auth/me` probe.
//
// We deliberately do *not* render a loading spinner here — the
// initial probe is fast (one `/auth/me` call) and any UI flash would
// land outside the route shell, where it has nowhere to live. The
// `<RequireAuth>` guard handles the `'loading'` state at the route
// boundary instead. Styleguide routes are a public dev/staging family,
// so subpages skip the probe too.
export function AuthProvider({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  useAuthBootstrap(!(pathname === "/styleguide" || pathname.startsWith("/styleguide/")));
  return <>{children}</>;
}
