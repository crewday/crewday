// crewday — production click-to-accept invitation surface.
//
// Spec §03 "Additional users (invite → click-to-accept)". One URL,
// two rendered states, and the kind comes from the server, not from
// a `?state=` demo flag:
//
//   GET /api/v1/invites/{token} — read-only preview (does NOT burn
//     the magic-link nonce). Renders the inviter, the workspace, the
//     invitee email, and the grants the Accept will activate. The
//     `kind` field branches on passkey-presence:
//       * "new_user"     — no passkey on file, render the enrolment
//                          ladder; clicking "Register this device"
//                          POSTs to `/invites/{token}/accept` then
//                          drives the bare-host invite passkey
//                          ceremony (start + finish).
//       * "existing_user" — at least one passkey on file, render the
//                          Accept card; clicking Accept POSTs to
//                          `/invites/{token}/accept`. A 401 on that
//                          POST means the SPA needs to redirect to
//                          /login (the user is signed out); after
//                          sign-in the magic-link is still alive
//                          (introspect doesn't burn it) and the page
//                          re-renders with the same token.
//
// On `existing_user` accept-success the response carries
// workspace_slug; we navigate to `/w/<slug>/today`. On `new_user` the
// invite-passkey ceremony's finish callback returns `{user_id,
// workspace_id, redirect}` and the server stamps the session cookie
// in the same UoW (cd-kd26) — no follow-up login required.

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactElement,
} from "react";
import { useNavigate, useParams } from "react-router-dom";
import { KeyRound } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { EmptyState, Loading } from "@/components/common";
import {
  PasskeyCancelledError,
  PasskeyTimeoutError,
  PasskeyTransientError,
  PasskeyUnsupportedError,
} from "@/auth/passkey";
import { runInvitePasskeyCeremony } from "@/auth/passkey-register";
import { useAuth } from "@/auth";
import type {
  InviteAcceptResponse,
  InviteIntrospection,
} from "@/types/api";

type AcceptState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "needs_sign_in" }
  | { kind: "error"; message: string; tone: "info" | "danger" }
  | { kind: "done"; redirect: string };

export default function AcceptPage(): ReactElement {
  const { token = "" } = useParams<{ token: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { isAuthenticated } = useAuth();

  const introspectKey = useMemo(() => qk.invite(token), [token]);

  const introspect = useQuery({
    queryKey: introspectKey,
    enabled: token.length > 0,
    // Burn-resistant: the GET is a peek, but a fresh load each mount
    // keeps the rendered preview in sync with the server's view of
    // the invite (e.g. expiry just lapsed).
    staleTime: 0,
    gcTime: 0,
    retry: false,
    queryFn: () =>
      fetchJson<InviteIntrospection>(
        `/api/v1/invites/${encodeURIComponent(token)}`,
      ),
  });

  const acceptMutation = useMutation<InviteAcceptResponse, unknown, void>({
    mutationFn: () =>
      fetchJson<InviteAcceptResponse>(
        `/api/v1/invites/${encodeURIComponent(token)}/accept`,
        { method: "POST" },
      ),
  });

  const [state, setState] = useState<AcceptState>({ kind: "idle" });
  // Concurrency guard — prevents an Enter-held / Playwright-burst
  // double-submit before `disabled={pending}` lands.
  const inflightRef = useRef(false);

  const onAccept = useCallback(async () => {
    if (introspect.data === undefined) return;
    if (inflightRef.current) return;
    // Existing-user branch with no active session: short-circuit to
    // sign-in BEFORE the POST. Posting first would burn the magic-link
    // nonce and leave the page unable to recover after the user signs
    // in (the introspect would 404 on return). After login the user
    // lands back at `/accept/<token>` — the introspect re-runs and
    // the click can complete.
    if (introspect.data.kind === "existing_user" && !isAuthenticated) {
      setState({ kind: "needs_sign_in" });
      return;
    }
    inflightRef.current = true;
    setState({ kind: "pending" });
    try {
      const outcome = await acceptMutation.mutateAsync();
      if (outcome.kind === "new_user") {
        // The accept call left the invite ``pending`` with a known
        // user_id; the bare-host passkey ceremony lands the
        // credential and activates the grants atomically server-side.
        const finish = await runInvitePasskeyCeremony(outcome.invite_id);
        // The server stamped the session cookie on /finish; clear
        // any stale auth caches so the next page reads the fresh
        // /auth/me envelope.
        queryClient.removeQueries({ queryKey: qk.authMe() });
        setState({ kind: "done", redirect: finish.redirect });
        return;
      }
      // existing_user — grants activated on the same call. Use the
      // returned slug to land on the workspace today page.
      const slug = outcome.workspace_slug ?? "";
      const redirect = slug ? `/w/${slug}/today` : "/";
      queryClient.removeQueries({ queryKey: qk.authMe() });
      setState({ kind: "done", redirect });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        // Existing-user branch with no active session. The token
        // hasn't been burned yet (the 401 happens before consume's
        // commit point on this branch); after sign-in the SPA can
        // hit /accept/<token> again.
        setState({ kind: "needs_sign_in" });
        return;
      }
      setState({ kind: "error", ...acceptMessageFor(err) });
    } finally {
      inflightRef.current = false;
    }
  }, [introspect.data, isAuthenticated, acceptMutation, queryClient]);

  // Post-success redirect — `useNavigate` cannot run inside the
  // mutation callback because navigation disposes the page mid-
  // ceremony if the browser races our state update.
  useEffect(() => {
    if (state.kind !== "done") return;
    navigate(state.redirect, { replace: true });
  }, [state, navigate]);

  // Sign-in redirect for the existing-user branch. Carry the current
  // path through `?next=` so the user lands back here after auth.
  useEffect(() => {
    if (state.kind !== "needs_sign_in") return;
    const next = `/accept/${encodeURIComponent(token)}`;
    navigate(`/login?next=${encodeURIComponent(next)}`, { replace: true });
  }, [state, token, navigate]);

  if (token.length === 0) {
    return (
      <AcceptShell>
        <h1 className="login__headline">Invite link required</h1>
        <p className="login__sub">
          This URL is missing its invite token. Ask the person who invited you
          to resend the link.
        </p>
      </AcceptShell>
    );
  }

  if (introspect.isPending) {
    return (
      <AcceptShell>
        <Loading />
      </AcceptShell>
    );
  }

  if (introspect.isError || !introspect.data) {
    return (
      <AcceptShell>
        <EmptyState>
          <p>
            This invite link is invalid, already used, or has expired. Ask the
            person who invited you to resend the link.
          </p>
        </EmptyState>
      </AcceptShell>
    );
  }

  const preview = introspect.data;
  const pending = state.kind === "pending";
  const errorNotice = state.kind === "error" ? state : null;

  return (
    <AcceptShell footerToken={token}>
      {preview.kind === "existing_user" ? (
        <ExistingUserView
          preview={preview}
          pending={pending}
          errorNotice={errorNotice}
          onAccept={() => void onAccept()}
        />
      ) : (
        <NewUserView
          preview={preview}
          pending={pending}
          errorNotice={errorNotice}
          onRegister={() => void onAccept()}
        />
      )}
    </AcceptShell>
  );
}

// ── Subcomponents ─────────────────────────────────────────────────

function AcceptShell({
  children,
  footerToken,
}: {
  children: ReactElement | ReactElement[];
  footerToken?: string;
}): ReactElement {
  return (
    <div className="surface surface--login">
      <main className="login">
        <div className="login__card login__card--wide">
          <div className="login__brand">
            <span className="desk__logo" aria-hidden="true">◈</span>
            <span className="desk__wordmark">crew.day</span>
          </div>
          {children}
          {footerToken !== undefined && (
            <p className="login__footnote muted">
              Invite link: <code className="inline-code">/accept/{footerToken}</code>
              {" "}— valid once, expires in 24 hours.
            </p>
          )}
        </div>
      </main>
    </div>
  );
}

function NewUserView({
  preview,
  pending,
  errorNotice,
  onRegister,
}: {
  preview: InviteIntrospection;
  pending: boolean;
  errorNotice: { message: string; tone: "info" | "danger" } | null;
  onRegister: () => void;
}): ReactElement {
  return (
    <>
      <h1 className="login__headline">
        Welcome to the household, {firstName(preview.email_lower)}
      </h1>
      <p className="login__sub">
        {preview.inviter_display_name} has added you to{" "}
        <strong>{preview.workspace_name}</strong>
        {preview.grants.length > 0 ? (
          <>
            {" "}as <strong>{formatRoles(preview.grants)}</strong>
          </>
        ) : null}
        .
      </p>

      {errorNotice && (
        <p
          className={
            "login__notice"
            + (errorNotice.tone === "danger" ? " login__notice--danger" : "")
          }
          role="alert"
          data-testid="accept-error"
        >
          {errorNotice.message}
        </p>
      )}

      <ol className="enroll-steps">
        <li className="enroll-step enroll-step--done">
          <span className="enroll-step__num">1</span>
          <div>
            <strong>Confirm it's you</strong>
            <p>
              This link was sent to{" "}
              <code className="inline-code">{preview.email_lower}</code>. If
              that isn't you, close this page.
            </p>
          </div>
        </li>
        <li className="enroll-step enroll-step--active">
          <span className="enroll-step__num">2</span>
          <div>
            <strong>Register a passkey</strong>
            <p>
              Your phone, Face ID, fingerprint — whatever your device already
              uses to unlock itself. No password to remember.
            </p>
            <button
              type="button"
              className="btn btn--moss btn--lg"
              onClick={onRegister}
              disabled={pending}
              aria-busy={pending}
              data-testid="accept-register"
            >
              <KeyRound size={18} strokeWidth={1.8} aria-hidden="true" />{" "}
              {pending ? "Contacting your authenticator…" : "Register this device"}
            </button>
          </div>
        </li>
        <li className="enroll-step">
          <span className="enroll-step__num">3</span>
          <div>
            <strong>Install the app shortcut</strong>
            <p>
              After signing in, tap "Add to home screen". The app works offline
              for today's tasks.
            </p>
          </div>
        </li>
      </ol>
    </>
  );
}

function ExistingUserView({
  preview,
  pending,
  errorNotice,
  onAccept,
}: {
  preview: InviteIntrospection;
  pending: boolean;
  errorNotice: { message: string; tone: "info" | "danger" } | null;
  onAccept: () => void;
}): ReactElement {
  return (
    <>
      <h1 className="login__headline">You've been invited to more surfaces</h1>
      <p className="login__sub">
        {preview.inviter_display_name} is adding you to{" "}
        <strong>{preview.workspace_name}</strong> on your existing crew.day
        account. Nothing changes until you accept below.
      </p>

      <section className="panel panel--inset">
        <header className="panel__head"><h2>What will change</h2></header>
        {preview.grants.length === 0 ? (
          <p className="muted">No grants attached to this invite.</p>
        ) : (
          <ul className="settings-list">
            {preview.grants.map((g, idx) => (
              <li key={`${g.scope_kind}:${g.scope_id}:${idx}`}>
                <strong>{titleCaseRole(g.grant_role)}</strong>
                {" — "}
                <em>{describeScope(g)}</em>
              </li>
            ))}
          </ul>
        )}
        <p className="muted">
          No passkey re-registration. No break-glass regeneration. Your other
          workspaces are untouched.
        </p>
      </section>

      {errorNotice && (
        <p
          className={
            "login__notice"
            + (errorNotice.tone === "danger" ? " login__notice--danger" : "")
          }
          role="alert"
          data-testid="accept-error"
        >
          {errorNotice.message}
        </p>
      )}

      <div className="form__actions">
        <button
          type="button"
          className="btn btn--moss btn--lg"
          onClick={onAccept}
          disabled={pending}
          aria-busy={pending}
          data-testid="accept-existing"
        >
          {pending ? "Accepting…" : "Accept"}
        </button>
        <button type="button" className="btn btn--ghost btn--lg" disabled={pending}>
          Not now
        </button>
      </div>
    </>
  );
}

// ── Internals ─────────────────────────────────────────────────────

function firstName(emailLower: string): string {
  const local = emailLower.split("@")[0] ?? "";
  if (!local) return "you";
  // Best-effort: capitalise the first character; rest left as-is so a
  // dotted local-part renders as `"camille.aubry"` rather than over-
  // formatted nonsense. The introspect doesn't return a display name
  // for the new-user branch (the user row hasn't been seeded with one
  // yet), so falling back to the email's local-part is the most
  // honest signal we have.
  return local.charAt(0).toUpperCase() + local.slice(1);
}

function titleCaseRole(role: string): string {
  if (!role) return role;
  return role.charAt(0).toUpperCase() + role.slice(1);
}

function describeScope(grant: {
  scope_kind: string;
  scope_id: string;
  scope_property_id?: string | null;
}): string {
  if (grant.scope_kind === "property" && grant.scope_property_id) {
    return `property ${grant.scope_property_id}`;
  }
  if (grant.scope_kind === "organization") {
    return `organization ${grant.scope_id}`;
  }
  return "workspace-wide";
}

function formatRoles(
  grants: ReadonlyArray<{ grant_role: string }>,
): string {
  const unique = Array.from(
    new Set(grants.map((g) => titleCaseRole(g.grant_role))),
  );
  if (unique.length === 0) return "Member";
  if (unique.length === 1) return unique[0]!;
  return unique.slice(0, -1).join(", ") + " and " + unique[unique.length - 1]!;
}

interface AcceptMessage {
  message: string;
  tone: "info" | "danger";
}

function acceptMessageFor(err: unknown): AcceptMessage {
  if (err instanceof PasskeyCancelledError) {
    return {
      message: "Passkey prompt closed. Click “Register this device” to try again.",
      tone: "info",
    };
  }
  if (err instanceof PasskeyTimeoutError) {
    return {
      message:
        "Your authenticator didn't respond in time. Click “Register this device” to try again.",
      tone: "info",
    };
  }
  if (err instanceof PasskeyTransientError) {
    return {
      message:
        "Couldn't reach your authenticator. Wait a moment and try again — your invite link stays valid for 24 hours.",
      tone: "danger",
    };
  }
  if (err instanceof PasskeyUnsupportedError) {
    if (err.kind === "invalid_state") {
      return {
        message:
          "This device already has a passkey for your account. Try another device — your invite link stays valid for 24 hours.",
        tone: "danger",
      };
    }
    if (err.kind === "constraint") {
      return {
        message:
          "Your authenticator can't satisfy the passkey requirements for this workspace. Try another device — your invite link stays valid for 24 hours.",
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
        "This browser or device can't register a passkey here. Try another device — your invite link stays valid for 24 hours.",
      tone: "danger",
    };
  }
  if (err instanceof ApiError) {
    if (err.status === 404 || err.status === 410) {
      return {
        message:
          "This invite link is no longer valid. Ask the person who invited you to resend it.",
        tone: "danger",
      };
    }
    if (err.status === 409) {
      return {
        message:
          "This invite was already accepted on another device. You can sign in directly from the home page.",
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
    message: "We couldn't accept this invite. Try again in a moment.",
    tone: "danger",
  };
}
