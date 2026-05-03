// crewday — WebAuthn passkey ceremony helpers (register flow).
//
// The recovery-enrol and signup flows both drive a
// `navigator.credentials.create()` call against a server-minted
// `PublicKeyCredentialCreationOptions` envelope. This module owns
// the parallel of `passkey.ts` (which covers the login / get()
// ceremony): decode the JSON options the server sent, encode the
// attestation back into the JSON shape py_webauthn expects, and
// map platform errors onto the module's typed errors so callers
// can branch without parsing strings.
//
// Wire shape mirrors `app/api/v1/auth/recovery.py` and
// `app/api/v1/auth/signup.py`. Both surfaces emit the same
// `{challenge_id, options}` envelope on /start and accept a
// `{session_id, challenge_id, credential}` shape on /finish — the
// only differences are the URL prefix and the session-id field
// name (`recovery_session_id` vs `signup_session_id`). A single
// `runRegisterCeremony` driver handles both; surface-specific
// thin wrappers (`runRecoveryEnrollCeremony`,
// `runSignupEnrollCeremony`) keep the call sites readable.
//
// Recovery /finish stamps a session cookie. Signup /finish does
// not (§03 "Self-serve signup": "no Set-Cookie" — the SPA runs
// the regular passkey login ceremony after the workspace lands).

import { fetchJson } from "@/lib/api";
import {
  mapNavigatorError,
  PasskeyUnsupportedError,
} from "./passkey";

/**
 * JSON-shaped `PublicKeyCredentialCreationOptions`, matching the
 * WebAuthn Level 3 IDL's serialised form. The server emits this
 * dict verbatim from py_webauthn's `options_to_json`; every
 * `BufferSource` field is base64url-encoded as a string. We keep
 * the type permissive (unknown-index) because py_webauthn evolves
 * alongside the spec — new optional keys shouldn't break compile.
 */
export interface PublicKeyCredentialCreationOptionsJSON {
  challenge: string;
  rp: { id?: string; name: string };
  user: { id: string; name: string; displayName: string };
  pubKeyCredParams: { type: "public-key"; alg: number }[];
  timeout?: number;
  excludeCredentials?: {
    type: "public-key";
    id: string;
    transports?: ReadonlyArray<AuthenticatorTransport>;
  }[];
  authenticatorSelection?: {
    authenticatorAttachment?: "platform" | "cross-platform";
    residentKey?: "discouraged" | "preferred" | "required";
    requireResidentKey?: boolean;
    userVerification?: "required" | "preferred" | "discouraged";
  };
  attestation?: "none" | "indirect" | "direct" | "enterprise";
  [extensionKey: string]: unknown;
}

/**
 * JSON-serialisable subset of a :class:`PublicKeyCredential`
 * attestation — the payload posted to `/recover/passkey/finish`
 * (and, later, to the signup finish route). Mirrors the shape
 * py_webauthn's `verify_registration_response` consumes.
 */
export interface PasskeyRegisterCredential {
  id: string;
  rawId: string;
  type: "public-key";
  response: {
    clientDataJSON: string;
    attestationObject: string;
    transports?: AuthenticatorTransport[];
  };
  clientExtensionResults?: Record<string, unknown>;
  authenticatorAttachment?: "platform" | "cross-platform" | null;
}

export interface RecoveryStartResponse {
  challenge_id: string;
  options: PublicKeyCredentialCreationOptionsJSON;
}

export interface RecoveryFinishResponse {
  user_id: string;
  credential_id: string;
  revoked_credential_count: number;
  revoked_session_count: number;
}

export interface RecoveryVerifyResponse {
  recovery_session_id: string;
}

export interface SignupVerifyResponse {
  signup_session_id: string;
  desired_slug: string;
}

export interface SignupStartResponse {
  challenge_id: string;
  options: PublicKeyCredentialCreationOptionsJSON;
}

export interface SignupFinishResponse {
  workspace_slug: string;
  redirect: string;
}

// Invite-acceptance passkey wire shapes — mirrors
// `app/api/v1/auth/invite.py::Invite{PasskeyStart,PasskeyFinish}{Request,Response}`.
// New invitees (no passkey on file) run this ceremony after the
// `/invites/{token}/accept` returns `kind == "new_user"`. The finish
// callback also activates pending grants and emits the
// `user.enrolled` audit in the same UoW (cd-kd26), so its response
// carries the redirect target — same shape as the signup-finish body.
// Backend stamps the session cookie via `Set-Cookie`; the SPA must
// not touch the cookie value (HttpOnly).
export interface InvitePasskeyStartResponse {
  challenge_id: string;
  options: PublicKeyCredentialCreationOptionsJSON;
}

export interface InvitePasskeyFinishResponse {
  user_id: string;
  workspace_id: string;
  redirect: string;
}

/**
 * Consume the recovery magic link. Returns the transient
 * `recovery_session_id` the SPA threads into the two subsequent
 * POSTs. The token is burned single-use by the magic-link service
 * — a second call against the same token 409s.
 */
export async function verifyRecoveryToken(token: string): Promise<RecoveryVerifyResponse> {
  return fetchJson<RecoveryVerifyResponse>(
    `/api/v1/recover/passkey/verify?token=${encodeURIComponent(token)}`,
    { method: "GET" },
  );
}

/**
 * Request the `PublicKeyCredentialCreationOptions` for the
 * recovery enrolment ceremony.
 */
export async function beginRecoveryEnroll(
  recoverySessionId: string,
): Promise<RecoveryStartResponse> {
  return fetchJson<RecoveryStartResponse>("/api/v1/recover/passkey/start", {
    method: "POST",
    body: { recovery_session_id: recoverySessionId },
  });
}

/**
 * Submit the browser's attestation. On success the server stamps
 * a session cookie on the response — the SPA never touches the
 * cookie value (HttpOnly). The body confirms `user_id` and
 * documents the destructive blast radius (how many credentials +
 * sessions were revoked).
 */
export async function finishRecoveryEnroll(
  recoverySessionId: string,
  challengeId: string,
  credential: PasskeyRegisterCredential,
): Promise<RecoveryFinishResponse> {
  return fetchJson<RecoveryFinishResponse>("/api/v1/recover/passkey/finish", {
    method: "POST",
    body: {
      recovery_session_id: recoverySessionId,
      challenge_id: challengeId,
      credential,
    },
  });
}

/**
 * Burn the signup-verify magic link. Returns the transient
 * `signup_session_id` the SPA threads into the two subsequent
 * passkey POSTs (§03 "Self-serve signup" step 2). Unlike recovery,
 * `/signup/verify` is a POST that takes the token in the body —
 * the spec mandates a SPA-first JSON shape (§14) so the SPA can
 * decide how to navigate the post-verify state.
 */
export async function verifySignupToken(token: string): Promise<SignupVerifyResponse> {
  return fetchJson<SignupVerifyResponse>("/api/v1/signup/verify", {
    method: "POST",
    body: { token },
  });
}

/**
 * Request the `PublicKeyCredentialCreationOptions` for the signup
 * passkey ceremony. Backend mints the WebAuthn user entity from
 * the in-flight `signup_attempt` row — `display_name` rides
 * forward to the WebAuthn user.displayName so password managers
 * label the new credential with something meaningful.
 */
export async function beginSignupEnroll(
  signupSessionId: string,
  displayName: string,
): Promise<SignupStartResponse> {
  return fetchJson<SignupStartResponse>("/api/v1/signup/passkey/start", {
    method: "POST",
    body: { signup_session_id: signupSessionId, display_name: displayName },
  });
}

/**
 * Submit the browser's attestation to complete signup. On success
 * the backend creates the workspace + user + first passkey + four
 * permission groups in one transaction; the response is the
 * workspace slug and a redirect target. **No `Set-Cookie`** — the
 * SPA must run the regular passkey login ceremony after this
 * lands (§03 "Self-serve signup" step 4).
 */
export async function finishSignupEnroll(
  signupSessionId: string,
  challengeId: string,
  displayName: string,
  timezone: string,
  credential: PasskeyRegisterCredential,
): Promise<SignupFinishResponse> {
  return fetchJson<SignupFinishResponse>("/api/v1/signup/passkey/finish", {
    method: "POST",
    body: {
      signup_session_id: signupSessionId,
      challenge_id: challengeId,
      display_name: displayName,
      timezone,
      credential,
    },
  });
}

/**
 * Turn the server's JSON creation options into the
 * `PublicKeyCredentialCreationOptions` shape
 * `navigator.credentials.create()` expects.
 *
 * Browsers shipping `PublicKeyCredential.parseCreationOptionsFromJSON`
 * get the native fast path; the manual decoder covers older targets.
 */
export function decodeCreationOptions(
  json: PublicKeyCredentialCreationOptionsJSON,
): PublicKeyCredentialCreationOptions {
  const Native = (
    globalThis as unknown as {
      PublicKeyCredential?: {
        parseCreationOptionsFromJSON?: (
          j: PublicKeyCredentialCreationOptionsJSON,
        ) => PublicKeyCredentialCreationOptions;
      };
    }
  ).PublicKeyCredential;
  if (typeof Native?.parseCreationOptionsFromJSON === "function") {
    return Native.parseCreationOptionsFromJSON(json);
  }

  const decoded: PublicKeyCredentialCreationOptions = {
    challenge: base64UrlToBytes(json.challenge),
    rp: json.rp,
    user: {
      id: base64UrlToBytes(json.user.id),
      name: json.user.name,
      displayName: json.user.displayName,
    },
    pubKeyCredParams: json.pubKeyCredParams.map((p) => ({
      type: p.type,
      alg: p.alg,
    })),
  };
  if (json.timeout !== undefined) decoded.timeout = json.timeout;
  if (json.attestation !== undefined) decoded.attestation = json.attestation;
  if (json.authenticatorSelection !== undefined) {
    decoded.authenticatorSelection = { ...json.authenticatorSelection };
  }
  if (json.excludeCredentials !== undefined) {
    decoded.excludeCredentials = json.excludeCredentials.map((d) => ({
      type: d.type,
      id: base64UrlToBytes(d.id),
      ...(d.transports ? { transports: [...d.transports] as AuthenticatorTransport[] } : {}),
    }));
  }
  return decoded;
}

/**
 * Encode a create-ceremony `PublicKeyCredential` into the JSON
 * payload the server expects at `/recover/passkey/finish`.
 */
export function encodeAttestation(credential: PublicKeyCredential): PasskeyRegisterCredential {
  const response = credential.response as AuthenticatorAttestationResponse;
  const transports
    = typeof response.getTransports === "function"
      ? (response.getTransports() as AuthenticatorTransport[])
      : undefined;
  const out: PasskeyRegisterCredential = {
    id: credential.id,
    rawId: bytesToBase64Url(credential.rawId),
    type: "public-key",
    response: {
      clientDataJSON: bytesToBase64Url(response.clientDataJSON),
      attestationObject: bytesToBase64Url(response.attestationObject),
      ...(transports && transports.length > 0 ? { transports } : {}),
    },
  };
  try {
    const ext = credential.getClientExtensionResults();
    if (ext && Object.keys(ext).length > 0) {
      out.clientExtensionResults = { ...ext } as Record<string, unknown>;
    }
  } catch {
    // Polyfill without the helper — nothing to forward.
  }
  if (credential.authenticatorAttachment !== undefined) {
    out.authenticatorAttachment = credential.authenticatorAttachment as
      | "platform"
      | "cross-platform"
      | null;
  }
  return out;
}

/**
 * Drive a generic register ceremony: call the start endpoint to
 * mint the `PublicKeyCredentialCreationOptions`, run
 * `navigator.credentials.create()`, and hand the encoded
 * attestation to the finish callback. Caller supplies the start /
 * finish callbacks so signup and recovery — which differ only in
 * URL prefix and finish-payload shape — share one driver.
 *
 * Translates platform errors into the module's typed errors
 * (`PasskeyCancelledError`, `PasskeyTimeoutError`,
 * `PasskeyTransientError`, `PasskeyUnsupportedError`); anything
 * else propagates (usually `ApiError` from the start / finish
 * fetches).
 */
async function runRegisterCeremony<TStart extends { challenge_id: string; options: PublicKeyCredentialCreationOptionsJSON }, TFinish>(
  start: () => Promise<TStart>,
  finish: (challengeId: string, credential: PasskeyRegisterCredential) => Promise<TFinish>,
  signal: AbortSignal | undefined,
): Promise<TFinish> {
  if (typeof navigator === "undefined" || !navigator.credentials) {
    throw new PasskeyUnsupportedError(
      "This browser does not support WebAuthn passkeys.",
      "platform_unsupported",
    );
  }

  const begin = await start();
  const publicKey = decodeCreationOptions(begin.options);

  let attestation: Credential | null;
  try {
    attestation = await navigator.credentials.create({
      publicKey,
      signal,
    });
  } catch (err) {
    throw mapNavigatorError(err);
  }

  if (!isPublicKeyCredential(attestation)) {
    throw new PasskeyUnsupportedError(
      "Browser returned an unexpected credential type for passkey registration.",
      "platform_unsupported",
    );
  }

  const encoded = encodeAttestation(attestation);
  return finish(begin.challenge_id, encoded);
}

/**
 * Drive the full recovery-enrol ceremony: start → create() → finish.
 * Callers supply the `recovery_session_id` obtained from the verify
 * step. Translates platform errors into the module's typed errors
 * (`PasskeyCancelledError`, `PasskeyTimeoutError`,
 * `PasskeyTransientError`, `PasskeyUnsupportedError`); anything else
 * propagates (usually `ApiError`).
 */
export async function runRecoveryEnrollCeremony(
  recoverySessionId: string,
  options: { signal?: AbortSignal } = {},
): Promise<RecoveryFinishResponse> {
  return runRegisterCeremony(
    () => beginRecoveryEnroll(recoverySessionId),
    (challengeId, credential) =>
      finishRecoveryEnroll(recoverySessionId, challengeId, credential),
    options.signal,
  );
}

/**
 * Drive the full signup-enrol ceremony: start → create() → finish.
 * Caller supplies `signup_session_id` (from `verifySignupToken`),
 * `display_name`, and `timezone`. The finish call lands the
 * workspace + user + passkey atomically server-side; the response
 * carries the redirect target — but **not** a session cookie. The
 * SPA must run a regular passkey login (`useAuth().loginWithPasskey()`
 * or the underlying `runPasskeyLoginCeremony`) afterwards to
 * actually authenticate. See §03 "Self-serve signup" step 4.
 */
export async function runSignupEnrollCeremony(
  signupSessionId: string,
  displayName: string,
  timezone: string,
  options: { signal?: AbortSignal } = {},
): Promise<SignupFinishResponse> {
  return runRegisterCeremony(
    () => beginSignupEnroll(signupSessionId, displayName),
    (challengeId, credential) =>
      finishSignupEnroll(
        signupSessionId,
        challengeId,
        displayName,
        timezone,
        credential,
      ),
    options.signal,
  );
}

/**
 * Mint the `PublicKeyCredentialCreationOptions` for a new invitee's
 * first passkey. Bare-host endpoint — the `invite_id` is the
 * bearer-of-capability and the server enforces the
 * pending+passkey-absent gate before minting the challenge.
 */
export async function beginInvitePasskey(
  inviteId: string,
): Promise<InvitePasskeyStartResponse> {
  return fetchJson<InvitePasskeyStartResponse>("/api/v1/invite/passkey/start", {
    method: "POST",
    body: { invite_id: inviteId },
  });
}

/**
 * Submit the browser's attestation for an invite passkey ceremony.
 * The server activates the pending grants, emits `user.enrolled`,
 * and stamps the session cookie via `Set-Cookie` in the same UoW
 * (cd-kd26). Response carries the redirect target so the SPA can
 * land the user on `/w/<slug>/today` without a second round trip.
 */
export async function finishInvitePasskey(
  inviteId: string,
  challengeId: string,
  credential: PasskeyRegisterCredential,
): Promise<InvitePasskeyFinishResponse> {
  return fetchJson<InvitePasskeyFinishResponse>("/api/v1/invite/passkey/finish", {
    method: "POST",
    body: {
      invite_id: inviteId,
      challenge_id: challengeId,
      credential,
    },
  });
}

/**
 * Drive the full invite-passkey ceremony: start → create() → finish.
 * Caller supplies the `invite_id` returned by the preceding
 * `POST /invites/{token}/accept` (which left the invite in
 * ``pending`` with the user_id known but no passkey on file).
 *
 * Translates platform errors into the module's typed errors
 * (`PasskeyCancelledError`, `PasskeyTimeoutError`,
 * `PasskeyTransientError`, `PasskeyUnsupportedError`); anything else
 * propagates (usually `ApiError`). On success the server-set session
 * cookie authenticates the user — no follow-up login required.
 */
export async function runInvitePasskeyCeremony(
  inviteId: string,
  options: { signal?: AbortSignal } = {},
): Promise<InvitePasskeyFinishResponse> {
  return runRegisterCeremony(
    () => beginInvitePasskey(inviteId),
    (challengeId, credential) => finishInvitePasskey(inviteId, challengeId, credential),
    options.signal,
  );
}

// ── Internals ─────────────────────────────────────────────────────

function isPublicKeyCredential(value: unknown): value is PublicKeyCredential {
  if (!value || typeof value !== "object") return false;
  const v = value as { type?: unknown; rawId?: unknown; response?: unknown };
  return (
    v.type === "public-key"
    && v.rawId instanceof ArrayBuffer
    && typeof v.response === "object"
  );
}

function base64UrlToBytes(value: string): ArrayBuffer {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/");
  const pad = padded.length % 4 === 0 ? padded : padded + "=".repeat(4 - (padded.length % 4));
  const binary = atob(pad);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

function bytesToBase64Url(buffer: ArrayBuffer | ArrayBufferView): string {
  const view = buffer instanceof ArrayBuffer ? new Uint8Array(buffer) : new Uint8Array(buffer.buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < view.length; i += chunk) {
    binary += String.fromCharCode(...view.subarray(i, i + chunk));
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
