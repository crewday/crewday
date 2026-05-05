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
  type ReactNode,
} from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { KeyRound } from "lucide-react";
import { runSignupEnrollCeremony } from "@/auth/passkey-register";
import { pickRoleLanding, useAuth } from "@/auth";
import {
  messageForSignupEnrollError,
  readSignupEnrollHandoff,
} from "./publicAuthMappers";

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

  const handoff = readSignupEnrollHandoff(location.state);

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
        setEnroll({ kind: "error", ...messageForSignupEnrollError(err) });
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
    return <NoHandoffView />;
  }

  // Already-signed-in user re-following a stale link: bounce to landing.
  if (isAuthenticated && enroll.kind === "idle") {
    return <Navigate to={pickRoleLanding(user)} replace />;
  }

  const pending = enroll.kind === "creating" || enroll.kind === "logging_in";

  return (
    <SignupEnrollShell>
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

      <SignupEnrollForm
        displayName={displayName}
        onDisplayName={setDisplayName}
        browserTimezone={browserTimezone}
        enroll={enroll}
        pending={pending}
        onSubmit={onSubmit}
      />
    </SignupEnrollShell>
  );
}

function SignupEnrollShell({ children }: { children: ReactNode }) {
  return (
    <div className="surface surface--login">
      <main className="login">
        <div className="login__card">
          <div className="login__brand">
            <span className="desk__logo" aria-hidden="true">◈</span>
            <span className="desk__wordmark">crew.day</span>
          </div>
          {children}
          <Link to="/login" className="login__recover">← Back to sign in</Link>
        </div>
      </main>
    </div>
  );
}

function NoHandoffView(): ReactElement {
  return (
    <SignupEnrollShell>
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
    </SignupEnrollShell>
  );
}

function SignupEnrollForm({
  displayName,
  onDisplayName,
  browserTimezone,
  enroll,
  pending,
  onSubmit,
}: {
  displayName: string;
  onDisplayName: (value: string) => void;
  browserTimezone: string;
  enroll: EnrollState;
  pending: boolean;
  onSubmit: (e: FormEvent<HTMLFormElement>) => void;
}): ReactElement {
  return (
    <form className="form" onSubmit={onSubmit}>
      <label className="field">
        <span>Your display name</span>
        <input
          type="text"
          placeholder="Camille Aubry"
          autoComplete="name"
          required
          value={displayName}
          onChange={(ev) => onDisplayName(ev.target.value)}
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
        <span>{signupEnrollSubmitCopy(enroll)}</span>
      </button>
    </form>
  );
}

function signupEnrollSubmitCopy(enroll: EnrollState): string {
  if (enroll.kind === "creating") return "Contacting your authenticator…";
  if (enroll.kind === "logging_in") return "Signing you in…";
  return "Create my workspace";
}
