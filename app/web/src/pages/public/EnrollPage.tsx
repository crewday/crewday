// crewday — production `/recover/enroll` surface.
//
// The user arrives here from a recovery magic link
// (`{base_url}/recover/enroll?token=...`, minted by
// `app/auth/recovery.py`). The page:
//
//   1. reads `token` from the URL;
//   2. calls `GET /api/v1/recover/passkey/verify?token=...` to burn
//      the magic link and receive the transient `recovery_session_id`;
//   3. mounts a "register your passkey" button that drives the
//      `POST /recover/passkey/start` → `navigator.credentials.create()`
//      → `POST /recover/passkey/finish` ceremony;
//   4. on success, pulls a fresh `/auth/me` envelope (via
//      `useAuth().refresh()`), clears any cached query state, and
//      navigates to the user's role landing — same map LoginPage uses.
//
// Visual contract mirrors the `login__card` shell that LoginPage and
// RecoverPage use; semantic classes only, no new inline CSS.
//
// Error mapping is deliberately narrower than recovery/request: a
// missing / invalid / expired token is a hard fail (the link can't
// be replayed), so we surface a "request a new link" affordance that
// punts the user back to `/recover`.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Navigate, Link, useLocation, useNavigate } from "react-router-dom";
import { KeyRound } from "lucide-react";
import { ApiError } from "@/lib/api";
import {
  PasskeyCancelledError,
  PasskeyTimeoutError,
  PasskeyTransientError,
  PasskeyUnsupportedError,
} from "@/auth/passkey";
import {
  runRecoveryEnrollCeremony,
  verifyRecoveryToken,
} from "@/auth/passkey-register";
import { useAuth } from "@/auth";
import type { AuthMe } from "@/auth";

// Same landing map LoginPage uses — one source of truth would be nicer,
// but extracting it means a two-file diff every time a new grant-role
// ships. Kept in-file per the current convention.
const ROLE_LANDING: Record<string, string> = {
  worker: "/today",
  client: "/portfolio",
  manager: "/dashboard",
  admin: "/dashboard",
  guest: "/",
};

function pickLanding(user: AuthMe | null): string {
  const first = user?.available_workspaces?.[0];
  const role = first?.grant_role;
  if (role && ROLE_LANDING[role]) return ROLE_LANDING[role];
  return "/";
}

type VerifyState =
  | { kind: "idle" }
  | { kind: "verifying" }
  | { kind: "ready"; sessionId: string }
  | { kind: "error"; message: string; canRetry: boolean };

type EnrollState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "error"; message: string; tone: "info" | "danger" }
  | { kind: "done" };

export default function EnrollPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const { isAuthenticated, user, refresh } = useAuth();

  const token = useMemo(() => {
    const params = new URLSearchParams(location.search);
    return params.get("token")?.trim() ?? "";
  }, [location.search]);

  const [verify, setVerify] = useState<VerifyState>({ kind: "idle" });
  const [enroll, setEnroll] = useState<EnrollState>({ kind: "idle" });
  // Concurrency guard — same pattern as LoginPage. `disabled={pending}`
  // only blocks the *next* click after React commits; a rapid burst can
  // enqueue two ceremonies before the attribute applies.
  const inflightRef = useRef(false);
  const verifiedRef = useRef(false);

  // Verify the token exactly once on mount. StrictMode double-mounts the
  // component in dev; the ref keeps us from burning the token twice.
  useEffect(() => {
    if (verifiedRef.current) return;
    verifiedRef.current = true;

    if (!token) {
      setVerify({
        kind: "error",
        message: "Recovery link is missing its token. Request a new one.",
        canRetry: false,
      });
      return;
    }

    setVerify({ kind: "verifying" });
    void (async () => {
      try {
        const result = await verifyRecoveryToken(token);
        setVerify({ kind: "ready", sessionId: result.recovery_session_id });
      } catch (err) {
        setVerify({ kind: "error", ...verifyMessageFor(err) });
      }
    })();
  }, [token]);

  const onRegister = useCallback(async () => {
    if (verify.kind !== "ready") return;
    if (inflightRef.current) return;
    inflightRef.current = true;
    setEnroll({ kind: "pending" });
    try {
      await runRecoveryEnrollCeremony(verify.sessionId);
      await refresh();
      setEnroll({ kind: "done" });
    } catch (err) {
      setEnroll({ kind: "error", ...enrollMessageFor(err) });
    } finally {
      inflightRef.current = false;
    }
  }, [verify, refresh]);

  // Post-enrol redirect. We wait for `isAuthenticated` to flip true
  // (the `refresh()` above populates the store) so `<Navigate>` has
  // a valid `user` to branch on.
  useEffect(() => {
    if (enroll.kind !== "done") return;
    if (!isAuthenticated) return;
    navigate(pickLanding(user), { replace: true });
  }, [enroll.kind, isAuthenticated, user, navigate]);

  // Already-signed-in user following a stale link: bounce them to
  // their landing so they don't burn a fresh magic link for an account
  // they're already authenticated against.
  if (isAuthenticated && enroll.kind === "idle") {
    return <Navigate to={pickLanding(user)} replace />;
  }

  return (
    <div className="surface surface--login">
      <main className="login">
        <div className="login__card">
          <div className="login__brand">
            <span className="desk__logo" aria-hidden="true">◈</span>
            <span className="desk__wordmark">crew.day</span>
          </div>

          {verify.kind === "error" ? (
            <EnrollErrorView message={verify.message} canRetry={verify.canRetry} />
          ) : verify.kind !== "ready" ? (
            <VerifyingView />
          ) : (
            <ReadyView
              enroll={enroll}
              onRegister={() => {
                void onRegister();
              }}
            />
          )}

          <Link to="/login" className="login__recover">← Back to sign in</Link>
        </div>
      </main>
    </div>
  );
}

// ── Subcomponents ─────────────────────────────────────────────────

function VerifyingView() {
  return (
    <div role="status" aria-live="polite">
      <h1 className="login__headline">Checking your link…</h1>
      <p className="login__sub">One moment — we're confirming the recovery token.</p>
    </div>
  );
}

function ReadyView({
  enroll,
  onRegister,
}: {
  enroll: EnrollState;
  onRegister: () => void;
}) {
  const pending = enroll.kind === "pending";
  return (
    <>
      <h1 className="login__headline">Register a new passkey</h1>
      <p className="login__sub">
        Click below to enrol a passkey on this device. Doing so revokes every other passkey
        on your account and signs you out of every other active session.
      </p>
      {enroll.kind === "error" && (
        <p
          className={
            "login__notice"
            + (enroll.tone === "danger" ? " login__notice--danger" : "")
          }
          role="alert"
          data-testid="enroll-error"
        >
          {enroll.message}
        </p>
      )}
      <button
        className="btn btn--moss btn--lg login__primary"
        type="button"
        onClick={onRegister}
        disabled={pending}
        aria-busy={pending}
        data-testid="enroll-register"
      >
        <KeyRound size={18} strokeWidth={1.8} aria-hidden="true" />
        <span>{pending ? "Contacting your authenticator…" : "Register passkey"}</span>
      </button>
    </>
  );
}

function EnrollErrorView({
  message,
  canRetry,
}: {
  message: string;
  canRetry: boolean;
}) {
  return (
    <div role="alert">
      <h1 className="login__headline">We couldn't use this link</h1>
      <p className="login__sub">{message}</p>
      {canRetry && (
        <Link to="/recover" className="btn btn--moss btn--lg login__primary">
          <span>Request a new link</span>
        </Link>
      )}
    </div>
  );
}

// ── Internals ─────────────────────────────────────────────────────

interface VerifyMessage {
  message: string;
  canRetry: boolean;
}

function verifyMessageFor(err: unknown): VerifyMessage {
  if (err instanceof ApiError) {
    if (err.status === 410 || err.status === 400 || err.status === 409 || err.status === 404) {
      return {
        message:
          "This recovery link is expired, already used, or invalid. Request a new one below.",
        canRetry: true,
      };
    }
    if (err.status === 429) {
      return {
        message: "Too many recovery attempts from this network. Wait a minute, then try again.",
        canRetry: true,
      };
    }
  }
  return {
    message: "We couldn't verify the recovery link. Try again in a moment.",
    canRetry: true,
  };
}

interface EnrollMessage {
  message: string;
  tone: "info" | "danger";
}

function enrollMessageFor(err: unknown): EnrollMessage {
  if (err instanceof PasskeyCancelledError) {
    return {
      message: "Passkey prompt closed. Click “Register passkey” to try again.",
      tone: "info",
    };
  }
  if (err instanceof PasskeyTimeoutError) {
    return {
      message:
        "Your authenticator didn't respond in time. Click “Register passkey” to try again.",
      tone: "info",
    };
  }
  if (err instanceof PasskeyTransientError) {
    return {
      message:
        "Couldn't reach your authenticator. Wait a moment and try again — the link stays valid for 10 minutes.",
      tone: "danger",
    };
  }
  if (err instanceof PasskeyUnsupportedError) {
    if (err.kind === "invalid_state") {
      return {
        message:
          "This device already has a passkey for your account. Try another device — the link stays valid for 10 minutes.",
        tone: "danger",
      };
    }
    if (err.kind === "constraint") {
      return {
        message:
          "Your authenticator can't satisfy the passkey requirements for this workspace. Try another device — the link stays valid for 10 minutes.",
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
        "This browser or device can't register a passkey here. Try another device — the link stays valid for 10 minutes.",
      tone: "danger",
    };
  }
  if (err instanceof ApiError) {
    if (err.status === 429) {
      return {
        message: "Too many register attempts. Wait a minute and try again.",
        tone: "danger",
      };
    }
    if (err.status === 404) {
      return {
        message:
          "Your recovery session has expired. Request a fresh link from the sign-in page.",
        tone: "danger",
      };
    }
  }
  return {
    message: "We couldn't finish registering your passkey. Try again in a moment.",
    tone: "danger",
  };
}
