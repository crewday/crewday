// crewday — production `/signup` surface.
//
// Self-serve signup is a first-class flow on every deployment
// (§03 "Self-serve signup"; §00 G12). The visitor enters their
// email and a desired workspace slug; on submit we POST
// `/signup/start`, which gates on:
//
//   - `capabilities.settings.signup_enabled` — disabled
//     deployments 404 the entire `/signup/*` surface, so we
//     translate a 404 from `/signup/start` into the "signups are
//     closed on this deployment" view (the prompt's
//     capability-off contract — no probe endpoint exists).
//   - Slug validity / reservation — `409` with one of
//     `slug_taken | slug_reserved | slug_homoglyph_collision |
//     slug_in_grace_period`. The first variant carries a
//     `suggested_alternative`; we surface it inline under the
//     slug field so the user can take it in one click.
//   - Abuse mitigations — `422` `captcha_required` /
//     `disposable_email`, `429` rate-limit. We surface a friendly
//     error and let the form re-arm.
//
// On 202 we swap to a generic "check your email" confirmation
// view. The backend's enumeration guard means the response is
// identical whether or not the email exists, but slug-related
// errors are NOT enumeration-protected (the workspaces table is
// public-list-able through other channels), so we keep slug
// errors inline.
//
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactElement,
  type ReactNode,
  type RefObject,
} from "react";
import { Link } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { ApiError, fetchJson } from "@/lib/api";

interface SignupStartBody {
  email: string;
  desired_slug: string;
  captcha_token?: string;
}

interface SignupStartResponse {
  status: string;
}

type SlugErrorKind =
  | "slug_taken"
  | "slug_reserved"
  | "slug_homoglyph_collision"
  | "slug_in_grace_period";

interface SlugError {
  kind: SlugErrorKind;
  suggestion?: string;
  collidingSlug?: string;
}

type FormState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "sent" }
  | { kind: "closed" }
  | { kind: "slug_error"; error: SlugError }
  | { kind: "error"; message: string };

interface TurnstileApi {
  render: (container: HTMLElement, options: TurnstileRenderOptions) => string;
  reset: (widgetId?: string) => void;
  remove?: (widgetId: string) => void;
}

interface TurnstileRenderOptions {
  sitekey: string;
  callback: (token: string) => void;
  "expired-callback": () => void;
  "error-callback": () => void;
}

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

const TURNSTILE_SCRIPT_ID = "crewday-turnstile-script";
const TURNSTILE_SCRIPT_SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

export default function SignupPage(): ReactElement {
  const [email, setEmail] = useState("");
  const [slug, setSlug] = useState("");
  const [form, setForm] = useState<FormState>({ kind: "idle" });
  const [captchaToken, setCaptchaToken] = useState<string | null>(null);
  const [captchaResetSignal, setCaptchaResetSignal] = useState(0);
  const captchaSiteKey = turnstileSiteKey();
  // Concurrency guard. Same shape as RecoverPage / LoginPage —
  // `disabled={pending}` only kicks in after React commits, so a
  // synchronous burst (Enter held down, Playwright double-submit) can
  // enqueue two POSTs against the per-IP throttle budget before the
  // attribute applies.
  const inflightRef = useRef(false);
  // Focus pivot for the "sent" confirmation. When the form is
  // replaced we move focus off the unmounted submit button onto the
  // confirmation heading — assistive tech announces the new view
  // and keyboard users keep an anchor.
  const sentHeadingRef = useRef<HTMLHeadingElement | null>(null);

  const mutation = useMutation<SignupStartResponse, Error, SignupStartBody>({
    mutationFn: (body) =>
      fetchJson<SignupStartResponse>("/api/v1/signup/start", {
        method: "POST",
        body,
      }),
    onMutate: () => {
      setForm({ kind: "pending" });
    },
    onSuccess: () => {
      setForm({ kind: "sent" });
      inflightRef.current = false;
    },
    onError: (err) => {
      if (captchaSiteKey && isCaptchaError(err)) {
        setCaptchaToken(null);
        setCaptchaResetSignal((value) => value + 1);
      }
      setForm(stateForError(err, Boolean(captchaSiteKey)));
      inflightRef.current = false;
    },
  });

  const onSubmit = useCallback(
    (e: FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      if (inflightRef.current) return;
      if (mutation.isPending) return;
      const trimmedEmail = email.trim();
      const trimmedSlug = slug.trim().toLowerCase();
      if (!trimmedEmail || !trimmedSlug) return;
      inflightRef.current = true;
      mutation.mutate({
        email: trimmedEmail,
        desired_slug: trimmedSlug,
        ...(captchaToken ? { captcha_token: captchaToken } : {}),
      });
    },
    [email, slug, captchaSiteKey, captchaToken, mutation],
  );

  const onCaptchaToken = useCallback((token: string) => {
    setCaptchaToken(token);
    setForm((current) => (current.kind === "error" ? { kind: "idle" } : current));
  }, []);

  const onCaptchaStale = useCallback(() => {
    setCaptchaToken(null);
  }, []);

  const acceptSuggestion = useCallback(
    (suggestion: string) => {
      setSlug(suggestion);
      setForm({ kind: "idle" });
    },
    [],
  );

  useEffect(() => {
    if (form.kind === "sent") sentHeadingRef.current?.focus();
  }, [form.kind]);

  const pending = form.kind === "pending";

  if (form.kind === "closed") {
    return (
      <SignupShell>
        <SignupClosedView />
      </SignupShell>
    );
  }

  if (form.kind === "sent") {
    return (
      <SignupShell>
        <SignupSentConfirmation headingRef={sentHeadingRef} />
      </SignupShell>
    );
  }

  return (
    <SignupShell>
      <h1 className="login__headline">Start your workspace</h1>
      <p className="login__sub">
        Pick a workspace handle and we'll send a one-time link to your inbox. After you click
        it, you register a passkey on this device — no password, ever.
      </p>
      {form.kind === "error" && (
        <p
          className="login__notice login__notice--danger"
          role="alert"
          data-testid="signup-error"
        >
          {form.message}
        </p>
      )}
      <form className="form" onSubmit={onSubmit}>
        <label className="field">
          <span>Your email</span>
          <input
            type="email"
            placeholder="you@example.com"
            autoComplete="email"
            required
            value={email}
            onChange={(ev) => setEmail(ev.target.value)}
            data-testid="signup-email"
          />
        </label>

        <label className="field">
          <span>Workspace handle</span>
          <input
            type="text"
            placeholder="villa-sud"
            autoComplete="off"
            spellCheck={false}
            inputMode="url"
            pattern="[a-z0-9][a-z0-9-]{1,38}[a-z0-9]"
            required
            value={slug}
            onChange={(ev) => setSlug(ev.target.value)}
            data-testid="signup-slug"
            aria-describedby="signup-slug-hint"
          />
          <span id="signup-slug-hint" className="login__hint">
            Lowercase letters, digits, and hyphens. Lives at <code>/w/&lt;handle&gt;/</code>.
          </span>
        </label>

        {form.kind === "slug_error" && (
          <SlugErrorNotice error={form.error} onAccept={acceptSuggestion} />
        )}

        {captchaSiteKey && (
          <TurnstileWidget
            siteKey={captchaSiteKey}
            resetSignal={captchaResetSignal}
            onToken={onCaptchaToken}
            onStale={onCaptchaStale}
          />
        )}

        <button
          type="submit"
          className="btn btn--moss btn--lg"
          disabled={pending}
          aria-busy={pending}
          data-testid="signup-submit"
        >
          {pending ? "Sending verification link…" : "Send verification link"}
        </button>
      </form>
      <p className="login__footnote muted">
        Already have a workspace? <Link to="/login">Sign in</Link>.
      </p>
    </SignupShell>
  );
}

// ── Subcomponents ─────────────────────────────────────────────────

function SignupShell({ children }: { children: ReactNode }): ReactElement {
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

/**
 * Generic "check your email" confirmation. Mirrors the
 * RecoverPage shape — `role="status"` + `aria-live="polite"` so
 * assistive tech announces the swap; the heading is
 * programmatically focusable so the parent effect can move
 * keyboard focus off the unmounted submit button.
 */
function SignupSentConfirmation({
  headingRef,
}: {
  headingRef: RefObject<HTMLHeadingElement | null>;
}): ReactElement {
  return (
    <div data-testid="signup-sent" role="status" aria-live="polite">
      <h1 className="login__headline" ref={headingRef} tabIndex={-1}>
        Check your email
      </h1>
      <p className="login__sub">
        We've sent a one-time link to verify your address. The link expires in 15 minutes
        or after one click — whichever comes first.
      </p>
      <p className="login__footnote muted">
        Nothing in your inbox? Check spam, wait a minute, then start over. Repeated requests
        may be rate-limited.
      </p>
    </div>
  );
}

/**
 * Capability-off view. The `/signup/*` router 404s every route
 * when `settings.signup_enabled = false`; we surface a clear
 * "ask the operator" message rather than the generic "couldn't
 * reach the server" fallback.
 */
function SignupClosedView(): ReactElement {
  return (
    <div role="alert" data-testid="signup-closed">
      <h1 className="login__headline">Signups are closed</h1>
      <p className="login__sub">
        This crew.day deployment isn't taking new workspaces right now. If you're expecting
        access, ask your operator to enable signups — or sign in if you already have a
        workspace.
      </p>
    </div>
  );
}

/**
 * Inline slug error. Surfaces `409` variants from `/signup/start`
 * with one-click suggestion adoption when the server provided a
 * `suggested_alternative` (cd-q16s spec §03 step 1).
 */
function SlugErrorNotice({
  error,
  onAccept,
}: {
  error: SlugError;
  onAccept: (suggestion: string) => void;
}): ReactElement {
  return (
    <p
      className="login__notice login__notice--danger"
      role="alert"
      data-testid="signup-slug-error"
    >
      {messageForSlugError(error)}
      {error.suggestion && (
        <>
          {" "}
          <button
            type="button"
            className="login__recover"
            onClick={() => onAccept(error.suggestion!)}
            data-testid="signup-slug-accept"
          >
            Use <strong>{error.suggestion}</strong> instead?
          </button>
        </>
      )}
    </p>
  );
}

function TurnstileWidget({
  siteKey,
  resetSignal,
  onToken,
  onStale,
}: {
  siteKey: string;
  resetSignal: number;
  onToken: (token: string) => void;
  onStale: () => void;
}): ReactElement {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const widgetIdRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const renderWidget = () => {
      if (cancelled || widgetIdRef.current || !containerRef.current || !window.turnstile) {
        return;
      }
      widgetIdRef.current = window.turnstile.render(containerRef.current, {
        sitekey: siteKey,
        callback: onToken,
        "expired-callback": onStale,
        "error-callback": onStale,
      });
    };

    if (window.turnstile) {
      renderWidget();
    } else {
      const script = ensureTurnstileScript();
      script.addEventListener("load", renderWidget);
      return () => {
        cancelled = true;
        script.removeEventListener("load", renderWidget);
      };
    }

    return () => {
      cancelled = true;
    };
  }, [siteKey, onToken, onStale]);

  useEffect(() => {
    if (resetSignal > 0 && widgetIdRef.current) {
      window.turnstile?.reset(widgetIdRef.current);
    }
  }, [resetSignal]);

  useEffect(
    () => () => {
      if (widgetIdRef.current) {
        window.turnstile?.remove?.(widgetIdRef.current);
      }
    },
    [],
  );

  return (
    <div
      className="signup-captcha"
      ref={containerRef}
      data-testid="signup-turnstile"
    />
  );
}

// ── Internals ─────────────────────────────────────────────────────

function messageForSlugError(error: SlugError): string {
  if (error.kind === "slug_taken") {
    return "That workspace handle is already in use.";
  }
  if (error.kind === "slug_reserved") {
    return "That handle is reserved by crew.day. Try another.";
  }
  if (error.kind === "slug_homoglyph_collision") {
    return error.collidingSlug
      ? `That handle is too close to an existing workspace (${error.collidingSlug}). Try another.`
      : "That handle is too close to an existing workspace. Try another.";
  }
  // slug_in_grace_period
  return "That handle was recently released and is held for 30 days before reuse. Try another.";
}

interface ErrorDetail {
  error?: string;
  suggested_alternative?: string;
  colliding_slug?: string;
}

function turnstileSiteKey(): string {
  const key = import.meta.env.VITE_TURNSTILE_SITE_KEY;
  return typeof key === "string" ? key.trim() : "";
}

function ensureTurnstileScript(): HTMLScriptElement {
  const existing = document.getElementById(TURNSTILE_SCRIPT_ID);
  if (existing instanceof HTMLScriptElement) return existing;
  const script = document.createElement("script");
  script.id = TURNSTILE_SCRIPT_ID;
  script.src = TURNSTILE_SCRIPT_SRC;
  script.async = true;
  script.defer = true;
  document.head.append(script);
  return script;
}

function readDetail(err: ApiError): ErrorDetail | null {
  // Detail can ride either as the RFC 7807 body or under `body.detail`
  // (FastAPI wraps `HTTPException(detail={...})` that way). The signup
  // router uses HTTPException with a dict detail, so read both shapes.
  const body = err.body;
  if (body && typeof body === "object" && !Array.isArray(body)) {
    const detail = (body as { detail?: unknown }).detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      return detail as ErrorDetail;
    }
    return body as ErrorDetail;
  }
  return null;
}

function isCaptchaError(err: unknown): boolean {
  if (!(err instanceof ApiError) || err.status !== 422) return false;
  const detail = readDetail(err);
  return detail?.error === "captcha_required" || detail?.error === "captcha_failed";
}

function stateForError(err: unknown, captchaEnabled = false): FormState {
  if (err instanceof ApiError) {
    if (err.status === 404) {
      // §03: disabled deployments 404 the entire `/signup/*` surface.
      return { kind: "closed" };
    }
    if (err.status === 409) {
      const detail = readDetail(err);
      const kind = detail?.error;
      if (
        kind === "slug_taken"
        || kind === "slug_reserved"
        || kind === "slug_homoglyph_collision"
        || kind === "slug_in_grace_period"
      ) {
        const slugError: SlugError = { kind };
        if (detail?.suggested_alternative) slugError.suggestion = detail.suggested_alternative;
        if (detail?.colliding_slug) slugError.collidingSlug = detail.colliding_slug;
        return { kind: "slug_error", error: slugError };
      }
      // Unknown 409 — fall through to generic.
    }
    if (err.status === 422) {
      const detail = readDetail(err);
      if (detail?.error === "captcha_required" || detail?.error === "captcha_failed") {
        return {
          kind: "error",
          message: captchaEnabled
            ? "The CAPTCHA check didn't complete. Try it again."
            : "This deployment requires a CAPTCHA, which isn't available in this build. "
              + "Ask your operator to configure the CAPTCHA widget or contact them for an invite.",
        };
      }
      if (detail?.error === "disposable_email") {
        return {
          kind: "error",
          message: "We don't accept signups from throwaway email providers. Use a real email.",
        };
      }
      if (detail?.error === "invalid_slug") {
        return {
          kind: "error",
          message:
            "That workspace handle isn't valid. Use 3–40 lowercase letters, digits, or hyphens "
            + "(no leading or trailing hyphen).",
        };
      }
      return {
        kind: "error",
        message: "We couldn't accept that. Check the form and try again.",
      };
    }
    if (err.status === 429) {
      return {
        kind: "error",
        message: "Too many signup attempts from this network. Wait a minute, then try again.",
      };
    }
  }
  return {
    kind: "error",
    message: "We couldn't reach the signup service. Try again in a moment.",
  };
}
