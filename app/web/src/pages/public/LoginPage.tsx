// crewday — production `/login` surface.
//
// Passkey is the only credential (§03 Principles): a single "Use
// passkey" button drives the discoverable-credential ceremony; the
// server identifies the user via the authenticator handle, so the
// page never asks for an email. Users who have lost every device
// recover via `/recover` (§03 "Self-service lost-device recovery");
// magic links never issue a session on their own.
//
// Every semantic class (`login__card`, `login__brand`,
// `login__primary`, …) is part of the production CSS contract. The
// `login__notice` element (`.login__notice[--danger]` in globals.css)
// surfaces passkey-ceremony errors required by §14 "Error handling".
// The button copy is wrapped in a `<span>` so pending copy can swap in
// without disturbing the icon slot — the span has no styling of its own,
// it's just a DOM seam.
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
  pickLoginLanding,
  sanitizeNext,
  useAuth,
} from "@/auth";
import { messageForLoginError } from "./publicAuthMappers";

type FormState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "error"; message: string; tone: "info" | "danger" };

type LoginLocationState = {
  notice?: unknown;
};

export default function LoginPage() {
  const { isAuthenticated, loginWithPasskey, user } = useAuth();
  const location = useLocation();
  const initialNotice = loginNotice(location.state);
  const [form, setForm] = useState<FormState>(
    initialNotice ? { kind: "error", message: initialNotice, tone: "info" } : { kind: "idle" },
  );
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
    return <Navigate to={pickLoginLanding(safeNext, user)} replace />;
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

function loginNotice(state: unknown): string | null {
  if (!state || typeof state !== "object" || Array.isArray(state)) return null;
  const notice = (state as LoginLocationState).notice;
  return typeof notice === "string" && notice ? notice : null;
}
