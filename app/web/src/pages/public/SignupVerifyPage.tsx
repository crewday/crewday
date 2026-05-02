// crewday — production `/signup/verify` surface.
//
// The user lands here after clicking the magic link emailed by
// `/signup/start`. Two URL shapes hit this page:
//
//   1. `/signup/verify?token=…` — canonical SPA path. The link
//      template can emit this directly when the deployment knows
//      the SPA owns the verify step.
//   2. `/auth/magic/:token` — generic magic-link URL the mailer
//      defaults to. The signup mailer (`app/mail/templates/magic_link`)
//      uses this shape today, so we accept it here and treat the
//      path param as the token.
//
// On mount we POST `/api/v1/signup/verify` with `{token}` (a POST,
// not a GET — the spec mandates a SPA-first JSON shape; §03 step 2).
// On success the SPA navigates to `/signup/enroll`, threading the
// `signup_session_id` + `desired_slug` through React Router state
// (matching how EnrollPage hands off `recovery_session_id`).
//
// Failure modes are deliberately collapsed: 400 / 404 / 409 / 410
// all mean "this link is no good — request a new one". 429 is
// rate-limit. A missing token (user opened the URL by hand) is a
// no-retry copy. The retry CTA always points back to `/signup`.

import { useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import { Link, Navigate, useLocation, useNavigate, useParams } from "react-router-dom";
import { ApiError } from "@/lib/api";
import { verifySignupToken } from "@/auth/passkey-register";
import { pickRoleLanding, useAuth } from "@/auth";

type VerifyState =
  | { kind: "idle" }
  | { kind: "verifying" }
  | { kind: "error"; message: string; canRetry: boolean };

export interface SignupEnrollHandoff {
  signupSessionId: string;
  desiredSlug: string;
}

export default function SignupVerifyPage(): ReactElement {
  const location = useLocation();
  const params = useParams<{ token?: string }>();
  const navigate = useNavigate();
  const { isAuthenticated, user } = useAuth();

  // Token rides as either a query param (`/signup/verify?token=…`) or a
  // path param (`/auth/magic/:token`). Path param wins when both shapes
  // are mounted on the same route tree because the generic mailer
  // emits the path-param URL.
  const token = useMemo(() => {
    if (params.token && params.token.trim()) return params.token.trim();
    const search = new URLSearchParams(location.search);
    return search.get("token")?.trim() ?? "";
  }, [params.token, location.search]);

  const [verify, setVerify] = useState<VerifyState>({ kind: "idle" });
  // Burn the token at most once per mount. StrictMode double-mounts
  // the component in dev; without the guard the second mount would
  // 409 because the magic-link service is single-use.
  const verifiedRef = useRef(false);

  useEffect(() => {
    if (verifiedRef.current) return;
    verifiedRef.current = true;

    if (!token) {
      setVerify({
        kind: "error",
        message: "Signup link is missing its token. Start over from the signup page.",
        canRetry: false,
      });
      return;
    }

    setVerify({ kind: "verifying" });
    void (async () => {
      try {
        const result = await verifySignupToken(token);
        const handoff: SignupEnrollHandoff = {
          signupSessionId: result.signup_session_id,
          desiredSlug: result.desired_slug,
        };
        // `replace` so the back button doesn't bounce the user to a
        // burned token. Token rides forward only as router state, not
        // in the URL — keeps it out of bookmarks / referer headers.
        navigate("/signup/enroll", { replace: true, state: handoff });
      } catch (err) {
        setVerify({ kind: "error", ...verifyMessageFor(err) });
      }
    })();
  }, [token, navigate]);

  // Already-signed-in user following a stale link: bounce to their
  // landing rather than letting them burn a fresh token. (Recovery
  // does the same thing — see EnrollPage.)
  if (isAuthenticated) {
    return <Navigate to={pickRoleLanding(user)} replace />;
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
            <SignupVerifyError message={verify.message} canRetry={verify.canRetry} />
          ) : (
            <VerifyingView />
          )}

          <Link to="/login" className="login__recover">← Back to sign in</Link>
        </div>
      </main>
    </div>
  );
}

// ── Subcomponents ─────────────────────────────────────────────────

function VerifyingView(): ReactElement {
  return (
    <div role="status" aria-live="polite" data-testid="signup-verify-pending">
      <h1 className="login__headline">Confirming your link…</h1>
      <p className="login__sub">One moment — we're verifying your signup token.</p>
    </div>
  );
}

function SignupVerifyError({
  message,
  canRetry,
}: {
  message: string;
  canRetry: boolean;
}): ReactElement {
  return (
    <div role="alert" data-testid="signup-verify-error">
      <h1 className="login__headline">We couldn't use this link</h1>
      <p className="login__sub">{message}</p>
      {canRetry && (
        <Link to="/signup" className="btn btn--moss btn--lg login__primary">
          <span>Start over</span>
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
    if (
      err.status === 410
      || err.status === 400
      || err.status === 409
      || err.status === 404
    ) {
      return {
        message:
          "This signup link is expired, already used, or invalid. Start over from the signup page below.",
        canRetry: true,
      };
    }
    if (err.status === 429) {
      return {
        message: "Too many attempts from this network. Wait a minute, then try again.",
        canRetry: true,
      };
    }
  }
  return {
    message: "We couldn't verify your signup link. Try again in a moment.",
    canRetry: true,
  };
}
