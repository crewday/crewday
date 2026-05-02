// crewday — production `/signup/enroll` surface.
//
// Final step of the self-serve signup flow (§03 "Self-serve signup"
// steps 3-4). On entry the SPA holds a `signup_session_id` plus the
// chosen `desired_slug`, threaded forward as router state by
// `SignupVerifyPage`. The user confirms a display name (defaulted to
// the local part of their email when known), the page picks the
// browser's timezone via `Intl.DateTimeFormat().resolvedOptions()`,
// and clicking "Create my workspace" runs the passkey ceremony:
//
//   POST /api/v1/signup/passkey/start  → mint creation options
//   navigator.credentials.create(...)  → user verifies on device
//   POST /api/v1/signup/passkey/finish → server creates workspace + user + passkey atomically
//
// Backend's finish response does **not** stamp a session cookie
// (§03 step 4: "no Set-Cookie"). The SPA must therefore run the
// regular passkey login ceremony immediately afterwards — that's
// what actually authenticates the user. Once `loginWithPasskey()`
// succeeds we read `useAuth().user`, pick the role landing the same
// way LoginPage / EnrollPage do, and navigate.
//
// Visual contract mirrors EnrollPage / SignupPage / LoginPage
// (`surface--login` + `login__card`); semantic classes only, no
// inline CSS.

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactElement,
} from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { KeyRound } from "lucide-react";
import { ApiError } from "@/lib/api";
import {
  PasskeyCancelledError,
  PasskeyTimeoutError,
  PasskeyTransientError,
  PasskeyUnsupportedError,
} from "@/auth/passkey";
import { runSignupEnrollCeremony } from "@/auth/passkey-register";
import { pickRoleLanding, useAuth } from "@/auth";
import type { SignupEnrollHandoff } from "./SignupVerifyPage";

type EnrollState =
  | { kind: "idle" }
  | { kind: "creating" } // `/signup/passkey/start` in flight or browser ceremony running
  | { kind: "logging_in" } // ceremony done, running the post-finish passkey login
  | { kind: "error"; message: string; tone: "info" | "danger" }
  | { kind: "done" };

export default function SignupEnrollPage(): ReactElement {
  const location = useLocation();
  const navigate = useNavigate();
  const { isAuthenticated, user, loginWithPasskey } = useAuth();

  const handoff = readHandoff(location.state);

  const browserTimezone = useMemo(() => {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch {
      return "UTC";
    }
  }, []);

  const [displayName, setDisplayName] = useState("");
  const [enroll, setEnroll] = useState<EnrollState>({ kind: "idle" });
  // Concurrency guard — prevents a synchronous double-submit (Enter
  // held, Playwright burst) from enqueueing two ceremonies before
  // `disabled={pending}` lands.
  const inflightRef = useRef(false);

  const onSubmit = useCallback(
    async (e: FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      if (!handoff) return;
      if (inflightRef.current) return;
      const trimmedName = displayName.trim();
      if (!trimmedName) return;

      inflightRef.current = true;
      setEnroll({ kind: "creating" });
      try {
        await runSignupEnrollCeremony(handoff.signupSessionId, trimmedName, browserTimezone);
        // Signup finish does NOT stamp a cookie — run the regular
        // passkey login to actually authenticate. The user just
        // verified on the same device, so the platform authenticator
        // is warm and the second prompt is usually instant.
        // `loginWithPasskey` calls `/auth/me` internally and populates
        // the auth store, so by the time it resolves the redirect
        // effect can safely read `user`.
        setEnroll({ kind: "logging_in" });
        await loginWithPasskey();
        setEnroll({ kind: "done" });
      } catch (err) {
        setEnroll({ kind: "error", ...enrollMessageFor(err) });
      } finally {
        inflightRef.current = false;
      }
    },
    [handoff, displayName, browserTimezone, loginWithPasskey],
  );

  // Post-success redirect. Wait for `isAuthenticated` to flip true
  // (the login ceremony populates the auth store) before navigating.
  useEffect(() => {
    if (enroll.kind !== "done") return;
    if (!isAuthenticated) return;
    navigate(pickRoleLanding(user), { replace: true });
  }, [enroll.kind, isAuthenticated, user, navigate]);

  // No handoff state at all — user deep-linked to /signup/enroll
  // without going through verify. Push them back to start.
  if (!handoff) {
    return (
      <div className="surface surface--login">
        <main className="login">
          <div className="login__card">
            <div className="login__brand">
              <span className="desk__logo" aria-hidden="true">◈</span>
              <span className="desk__wordmark">crew.day</span>
            </div>
            <div role="alert" data-testid="signup-enroll-no-handoff">
              <h1 className="login__headline">Signup link required</h1>
              <p className="login__sub">
                Your signup session has expired or you reached this page directly. Start over
                to receive a fresh link.
              </p>
              <Link to="/signup" className="btn btn--moss btn--lg login__primary">
                <span>Start over</span>
              </Link>
            </div>
            <Link to="/login" className="login__recover">← Back to sign in</Link>
          </div>
        </main>
      </div>
    );
  }

  // Already-signed-in user re-following a stale link: bounce to landing.
  if (isAuthenticated && enroll.kind === "idle") {
    return <Navigate to={pickRoleLanding(user)} replace />;
  }

  const pending = enroll.kind === "creating" || enroll.kind === "logging_in";

  return (
    <div className="surface surface--login">
      <main className="login">
        <div className="login__card">
          <div className="login__brand">
            <span className="desk__logo" aria-hidden="true">◈</span>
            <span className="desk__wordmark">crew.day</span>
          </div>

          <h1 className="login__headline">Create your workspace</h1>
          <p className="login__sub">
            You're claiming <strong>{handoff.desiredSlug}</strong>. Confirm your display name,
            then register a passkey on this device — no password, ever.
          </p>

          {enroll.kind === "error" && (
            <p
              className={
                "login__notice"
                + (enroll.tone === "danger" ? " login__notice--danger" : "")
              }
              role="alert"
              data-testid="signup-enroll-error"
            >
              {enroll.message}
            </p>
          )}

          <form className="form" onSubmit={onSubmit}>
            <label className="field">
              <span>Your display name</span>
              <input
                type="text"
                placeholder="Camille Aubry"
                autoComplete="name"
                required
                value={displayName}
                onChange={(ev) => setDisplayName(ev.target.value)}
                disabled={pending}
                data-testid="signup-enroll-name"
              />
            </label>

            <p className="login__hint" data-testid="signup-enroll-timezone">
              We'll set your workspace timezone to <strong>{browserTimezone}</strong>. You can
              change it later in settings.
            </p>

            <button
              type="submit"
              className="btn btn--moss btn--lg login__primary"
              disabled={pending}
              aria-busy={pending}
              data-testid="signup-enroll-submit"
            >
              <KeyRound size={18} strokeWidth={1.8} aria-hidden="true" />
              <span>
                {enroll.kind === "creating"
                  ? "Contacting your authenticator…"
                  : enroll.kind === "logging_in"
                    ? "Signing you in…"
                    : "Create my workspace"}
              </span>
            </button>
          </form>

          <Link to="/login" className="login__recover">← Back to sign in</Link>
        </div>
      </main>
    </div>
  );
}

// ── Internals ─────────────────────────────────────────────────────

/**
 * Pull the verify-step handoff out of `location.state`. Returns null
 * when the user reached `/signup/enroll` without going through verify
 * (deep link, page reload, history navigation that lost state).
 */
function readHandoff(state: unknown): SignupEnrollHandoff | null {
  if (!state || typeof state !== "object") return null;
  const s = state as Partial<SignupEnrollHandoff>;
  if (typeof s.signupSessionId !== "string" || !s.signupSessionId) return null;
  if (typeof s.desiredSlug !== "string" || !s.desiredSlug) return null;
  return { signupSessionId: s.signupSessionId, desiredSlug: s.desiredSlug };
}

interface EnrollMessage {
  message: string;
  tone: "info" | "danger";
}

function enrollMessageFor(err: unknown): EnrollMessage {
  if (err instanceof PasskeyCancelledError) {
    return {
      message: "Passkey prompt closed. Click “Create my workspace” to try again.",
      tone: "info",
    };
  }
  if (err instanceof PasskeyTimeoutError) {
    return {
      message:
        "Your authenticator didn't respond in time. Click “Create my workspace” to try again.",
      tone: "info",
    };
  }
  if (err instanceof PasskeyTransientError) {
    return {
      message:
        "Couldn't reach your authenticator. Wait a moment and try again — your signup link stays valid for 10 minutes.",
      tone: "danger",
    };
  }
  if (err instanceof PasskeyUnsupportedError) {
    if (err.kind === "invalid_state") {
      return {
        message:
          "This device already has a passkey registered. Try another device — your signup link stays valid for 10 minutes.",
        tone: "danger",
      };
    }
    if (err.kind === "constraint") {
      return {
        message:
          "Your authenticator can't satisfy the passkey requirements for this workspace. Try another device — your signup link stays valid for 10 minutes.",
        tone: "danger",
      };
    }
    if (err.kind === "security") {
      return {
        message:
          "This page can't run a passkey ceremony from an insecure context. Open crew.day over HTTPS and try again.",
        tone: "danger",
      };
    }
    return {
      message:
        "This browser or device can't register a passkey here. Try another device — your signup link stays valid for 10 minutes.",
      tone: "danger",
    };
  }
  if (err instanceof ApiError) {
    if (err.status === 404) {
      return {
        message:
          "Your signup session has expired. Start over from the signup page to receive a fresh link.",
        tone: "danger",
      };
    }
    if (err.status === 409) {
      return {
        message:
          "That workspace handle was claimed by someone else while you were enrolling. Start over and pick another.",
        tone: "danger",
      };
    }
    if (err.status === 429) {
      return {
        message: "Too many attempts. Wait a minute and try again.",
        tone: "danger",
      };
    }
  }
  return {
    message: "We couldn't finish creating your workspace. Try again in a moment.",
    tone: "danger",
  };
}
