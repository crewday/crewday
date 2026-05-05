// crewday — production `/login` surface.
//
// Passkey is the only credential (§03 Principles): a single "Use
// passkey" button drives the discoverable-credential ceremony; the
// server identifies the user via the authenticator handle, so the
// page never asks for an email. Users who have lost every device
// recover via `/recover` (§03 "Self-service lost-device recovery");
// magic links never issue a session on their own.
//
// Visual contract mirrors `mocks/web/src/pages/public/LoginPage.tsx`
// verbatim — every semantic class (`login__card`, `login__brand`,
// `login__primary`, …) is preserved so the mock's CSS applies without
// a second stylesheet. The only addition is the `login__notice`
// element (`.login__notice[--danger]` in globals.css): a small inline
// slot that surfaces passkey-ceremony errors. Absent from the mock
// (which has no interactive state) but required by §14 "Error
// handling". The button copy is wrapped in a `<span>` so pending
// copy can swap in without disturbing the icon slot — the span has
// no styling of its own, it's just a DOM seam.
//
// Auth plumbing lands in `@/auth` (cd-kc7u): `useAuth()` exposes the
// store + `loginWithPasskey()`, which itself calls
// `runPasskeyLoginCeremony()` and surfaces `PasskeyCancelledError` /
// `PasskeyUnsupportedError` for branch-on-class UX. 401 / 429 arrive
// as `ApiError`; we let the central handler own the store reset and
// only translate the message for the user.

import { useCallback, useMemo, useRef, useState } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { KeyRound } from "lucide-react";
import {
  sanitizeNext,
  useAuth,
} from "@/auth";
import type { AuthMe } from "@/auth";
import { messageForLoginError } from "./publicAuthMappers";

// Landing pages for each grant-role bucket. Mirrors `RoleHome` in
// `App.tsx` (and §14 "Role selector") so the redirect after login
// matches the redirect from `/`. Falls back to `/` when the user has
// no available workspaces — `WorkspaceGate` will then render the
// "no access yet" empty state.
const ROLE_LANDING: Record<string, string> = {
  worker: "/today",
  client: "/portfolio",
  manager: "/dashboard",
  admin: "/dashboard",
  guest: "/",
};

/**
 * Pick the landing URL for a freshly-logged-in user.
 *
 * Priority (higher wins):
 *   1. `?next=<path>` passed through `sanitizeNext` (defence in depth
 *      — the sanitiser that guards the emission points in
 *      `<RequireAuth>` and `createOnUnauthorized` is re-applied here
 *      because the LoginPage is the consumer that hands the value to
 *      `<Navigate to={...}>`). Same-origin `/admin/*` paths additionally
 *      require `is_deployment_admin` — a phishing link of the form
 *      `/login?next=/admin/dashboard` must not drop a worker on the
 *      admin shell they didn't ask to see (cd-28s7).
 *   2. The user's first available workspace grant role, mapped
 *      through `ROLE_LANDING`.
 *   3. `/` as a last resort — the `<RoleHome>` at the root already
 *      routes sensibly when no role signal is present.
 */
function pickLanding(next: string | null, user: AuthMe | null): string {
  if (next && !isAdminPathBlockedFor(next, user)) return next;
  const first = user?.available_workspaces?.[0];
  const role = first?.grant_role;
  if (role && ROLE_LANDING[role]) return ROLE_LANDING[role];
  return "/";
}

/**
 * True when `next` points at the deployment admin surface and the
 * caller is not a deployment admin. The caller drops `next` and falls
 * back to the role landing — see `pickLanding`. Trailing-slash and
 * query/fragment variants of `/admin` are all caught.
 */
function isAdminPathBlockedFor(next: string, user: AuthMe | null): boolean {
  if (user?.is_deployment_admin === true) return false;
  // `sanitizeNext` already rejected anything that doesn't start with a
  // single `/`, so a simple prefix check is safe — no `//admin.evil.com`
  // smuggling, no scheme prefix.
  return next === "/admin" || next.startsWith("/admin/")
    || next.startsWith("/admin?") || next.startsWith("/admin#");
}

type FormState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "error"; message: string; tone: "info" | "danger" };

export default function LoginPage() {
  const { isAuthenticated, loginWithPasskey, user } = useAuth();
  const location = useLocation();
  const [form, setForm] = useState<FormState>({ kind: "idle" });
  // Concurrency guard. `disabled={pending}` blocks the next click only
  // after React commits the `pending` state, so a rapid double-click
  // (or a keyboard Enter-spam) in the same event tick can enqueue two
  // ceremonies before the attribute is applied. A ref flips
  // synchronously inside the handler, preempting the second call before
  // it hits `runPasskeyLoginCeremony` — otherwise the server sees two
  // `/passkey/login/start` POSTs and the browser's WebAuthn UI
  // rejects the second `navigator.credentials.get()` with
  // `InvalidStateError`.
  const inflightRef = useRef(false);

  // Parse `?next=...` once per pathname/search change. We always filter
  // through `sanitizeNext` — an attacker-crafted `/login?next=https://
  // evil.example/` must NOT reach `<Navigate to={next}>`. This is the
  // defence-in-depth consumption point for cd-g5c2; the emission
  // points in `<RequireAuth>` and `createOnUnauthorized` already
  // filter, but a user can arrive here via a hand-crafted phishing
  // link that skips both.
  const safeNext = useMemo(() => {
    const params = new URLSearchParams(location.search);
    return sanitizeNext(params.get("next"));
  }, [location.search]);

  const onPasskey = useCallback(async () => {
    if (inflightRef.current) return;
    inflightRef.current = true;
    setForm({ kind: "pending" });
    try {
      await loginWithPasskey();
      // The `isAuthenticated` branch below handles the redirect — keep
      // the form in `pending` so the button stays disabled through the
      // commit that mounts `<Navigate>`.
    } catch (err) {
      setForm({ kind: "error", ...messageForLoginError(err) });
    } finally {
      // Drop the guard whether the ceremony resolved or threw. On
      // success `<Navigate>` unmounts us before the next render, so
      // the ref is discarded; on failure the button re-arms.
      inflightRef.current = false;
    }
  }, [loginWithPasskey]);

  // Already-signed-in users who land on /login (bookmark, back-button)
  // get bounced straight to their role landing. `status === 'loading'`
  // (bootstrap probe mid-flight) falls through to the card so the
  // passkey button can render — `<AuthProvider>` will re-run the probe
  // but we don't want to flash a spinner here.
  if (isAuthenticated) {
    return <Navigate to={pickLanding(safeNext, user)} replace />;
  }

  const pending = form.kind === "pending";
  const recoverHref = safeNext ? `/recover?next=${encodeURIComponent(safeNext)}` : "/recover";

  return (
    <div className="surface surface--login">
      <main className="login">
        <div className="login__card">
          <div className="login__brand">
            <span className="desk__logo" aria-hidden="true">◈</span>
            <span className="desk__wordmark">crew.day</span>
          </div>
          <h1 className="login__headline">Sign in with your passkey</h1>
          <p className="login__sub">No passwords, ever. Tap once to unlock the house.</p>
          {form.kind === "error" && (
            <p
              className={
                "login__notice"
                + (form.tone === "danger" ? " login__notice--danger" : "")
              }
              role="alert"
              data-testid="login-error"
            >
              {form.message}
            </p>
          )}
          <button
            className="btn btn--moss btn--lg login__primary"
            type="button"
            onClick={() => { void onPasskey(); }}
            disabled={pending}
            aria-busy={pending}
            data-testid="login-passkey"
          >
            <KeyRound size={18} strokeWidth={1.8} aria-hidden="true" />
            {pending ? "Contacting your authenticator…" : "Use passkey"}
          </button>
          <a href={recoverHref} className="login__recover">Lost your device? Recover access →</a>
        </div>
        <p className="login__footnote">
          First time here? Open the invite link your manager sent.{" "}
          <a href="/accept/demo-abc123" className="link">See what accepting an invite looks like →</a>
        </p>
      </main>
    </div>
  );
}
