// crewday — WebAuthn passkey ceremony helpers (login flow).
//
// `/login` is a discoverable-credential / conditional-UI ritual
// (§03 "Login"): the server mints a challenge, the browser shows
// the platform passkey UI, and we POST the assertion back. The
// password manager / authenticator carries the username — the SPA
// never asks for an email.
//
// Wire shapes match `app/api/v1/auth/passkey.py`'s
// `LoginStartResponse` / `LoginFinishRequest` / `LoginFinishResponse`.
// The session cookie (`__Host-crewday_session`) rides back as a
// `Set-Cookie` header on the finish response — we never touch it
// from JavaScript (it's `HttpOnly`).
//
// Callers are expected to handle errors (`PasskeyCancelledError`,
// `PasskeyUnsupportedError`, `PasskeyTimeoutError`,
// `PasskeyTransientError`, generic `ApiError`) and surface them in
// the LoginPage UI; this module raises typed errors so the UI can
// branch on them without parsing strings.

import { fetchJson } from "@/lib/api";
import type {
  PasskeyLoginCredential,
  PasskeyLoginFinish,
  PasskeyLoginStart,
  PublicKeyCredentialRequestOptionsJSON,
} from "./types";

/**
 * Thrown when the user dismisses the passkey UI. Maps to a benign
 * "no harm done" inline message on `/login` (no toast). The browser
 * raises this as `NotAllowedError` from `navigator.credentials.get()`.
 */
export class PasskeyCancelledError extends Error {
  constructor(cause?: unknown) {
    super("Passkey prompt was cancelled.");
    this.name = "PasskeyCancelledError";
    if (cause instanceof Error) this.cause = cause;
  }
}

/**
 * Discriminant for `PasskeyUnsupportedError`. Lets the LoginPage
 * branch on the underlying reason without re-`instanceof`-ing the
 * cause:
 *
 * - `platform_unsupported` — the browser/platform itself can't run
 *   the ceremony (no `navigator.credentials`, `NotSupportedError`,
 *   or an unexpected credential shape).
 * - `security` — `SecurityError` from the user agent (e.g. insecure
 *   context, RP-ID / origin mismatch).
 * - `invalid_state` — `InvalidStateError`, typically a duplicate
 *   passkey for this RP on the current authenticator.
 * - `constraint` — `ConstraintError`, the credential parameters
 *   the server requested can't be satisfied by the authenticator.
 */
export type PasskeyUnsupportedKind =
  | "platform_unsupported"
  | "security"
  | "invalid_state"
  | "constraint";

/**
 * Thrown when the platform reports no WebAuthn support, no passkey
 * exists for this RP, or the call site is in an insecure context
 * (HTTPS is required by the spec). Distinct from `Cancelled` because
 * the UI should surface the recovery / enrol affordance instead of
 * just an inline "try again".
 *
 * The `kind` discriminant lets the LoginPage branch on the underlying
 * reason without poking at `cause`.
 */
export class PasskeyUnsupportedError extends Error {
  readonly kind: PasskeyUnsupportedKind;
  constructor(message: string, kind: PasskeyUnsupportedKind, cause?: unknown) {
    super(message);
    this.name = "PasskeyUnsupportedError";
    this.kind = kind;
    if (cause instanceof Error) this.cause = cause;
  }
}

/**
 * Thrown when the WebAuthn ceremony hits a `TimeoutError` — the
 * authenticator didn't respond inside the requested `timeout`
 * window. The LoginPage should hint at retrying ("try again — your
 * authenticator didn't respond in time").
 */
export class PasskeyTimeoutError extends Error {
  constructor(cause?: unknown) {
    super("Passkey ceremony timed out before the authenticator responded.");
    this.name = "PasskeyTimeoutError";
    if (cause instanceof Error) this.cause = cause;
  }
}

/**
 * Thrown for transient platform-level failures: `UnknownError`
 * (the authenticator stack hit an internal error) and
 * `NetworkError` (the user agent failed to talk to a roaming
 * authenticator over its transport). The LoginPage should hint at
 * retry-with-backoff — these are usually self-healing.
 */
export class PasskeyTransientError extends Error {
  constructor(message: string, cause?: unknown) {
    super(message);
    this.name = "PasskeyTransientError";
    if (cause instanceof Error) this.cause = cause;
  }
}

/**
 * Begin a passkey login. Returns the server's challenge envelope so
 * the caller can immediately drive `navigator.credentials.get()`.
 *
 * The server's per-IP rate limit (§15 "Rate limiting and abuse
 * controls": 10/min) translates a flood into `429 rate_limited` —
 * surfaced as the regular `ApiError` (status `429`) so the LoginPage
 * can show its own "slow down" notice.
 */
export async function beginPasskeyLogin(): Promise<PasskeyLoginStart> {
  return fetchJson<PasskeyLoginStart>("/api/v1/auth/passkey/login/start", {
    method: "POST",
    body: {},
  });
}

/**
 * Convert the server's JSON request options into the
 * `PublicKeyCredentialRequestOptions` shape `navigator.credentials.get()`
 * expects (every `BufferSource` field is base64url in the JSON).
 *
 * Browsers that ship `PublicKeyCredential.parseRequestOptionsFromJSON`
 * (Chrome 121+, Safari 17+) get the native fast path; older targets
 * fall back to a manual decoder. We can't blanket-require the native
 * helper because the SPA still supports browsers that pre-date it.
 *
 * Exported for `passkey.test.ts` — product code goes through
 * `runPasskeyLoginCeremony()` below.
 */
export function decodeRequestOptions(
  json: PublicKeyCredentialRequestOptionsJSON,
): PublicKeyCredentialRequestOptions {
  const Native = (
    globalThis as unknown as {
      PublicKeyCredential?: {
        parseRequestOptionsFromJSON?: (j: PublicKeyCredentialRequestOptionsJSON) => PublicKeyCredentialRequestOptions;
      };
    }
  ).PublicKeyCredential;
  if (typeof Native?.parseRequestOptionsFromJSON === "function") {
    return Native.parseRequestOptionsFromJSON(json);
  }

  // Manual decode. We reconstruct only the fields the spec defines as
  // `BufferSource`; everything else (rpId, timeout, userVerification,
  // unknown extension keys) passes through untouched.
  const decoded: PublicKeyCredentialRequestOptions = {
    challenge: base64UrlToBytes(json.challenge),
  };
  if (json.rpId !== undefined) decoded.rpId = json.rpId;
  if (json.timeout !== undefined) decoded.timeout = json.timeout;
  if (json.userVerification !== undefined) decoded.userVerification = json.userVerification;
  if (json.allowCredentials !== undefined) {
    decoded.allowCredentials = json.allowCredentials.map((d) => ({
      id: base64UrlToBytes(d.id),
      type: d.type,
      ...(d.transports ? { transports: [...d.transports] as AuthenticatorTransport[] } : {}),
    }));
  }
  return decoded;
}

/**
 * Encode a `PublicKeyCredential` assertion into the JSON-friendly
 * shape the server's `/login/finish` endpoint expects (every
 * `ArrayBuffer` becomes base64url).
 *
 * Mirrors the inverse of `decodeRequestOptions`. We don't lean on
 * `PublicKeyCredential.toJSON()` (Chrome 122+) because Safari still
 * lacks it as of this writing — the manual encoder is reliable
 * across every supported target.
 */
export function encodeAssertion(credential: PublicKeyCredential): PasskeyLoginCredential {
  const response = credential.response as AuthenticatorAssertionResponse;
  const userHandle: string | null
    = response.userHandle && response.userHandle.byteLength > 0
      ? bytesToBase64Url(response.userHandle)
      : null;
  const out: PasskeyLoginCredential = {
    id: credential.id,
    rawId: bytesToBase64Url(credential.rawId),
    type: "public-key",
    response: {
      authenticatorData: bytesToBase64Url(response.authenticatorData),
      clientDataJSON: bytesToBase64Url(response.clientDataJSON),
      signature: bytesToBase64Url(response.signature),
      userHandle,
    },
  };
  // Optional surface: extension results are forwarded when present so
  // the server can act on (e.g.) `appid` echoes for legacy U2F keys.
  // `getClientExtensionResults` is always defined on a real assertion
  // — we wrap the access in a `try` so the encoder doesn't crash on
  // a polyfill that omits it.
  try {
    const ext = credential.getClientExtensionResults();
    // The DOM lib types `AuthenticationExtensionsClientOutputs` as a
    // closed shape (no index signature), but the wire payload is just
    // a JSON object — copy through `unknown` to satisfy the
    // `Record<string, unknown>` field on `PasskeyLoginCredential`
    // without losing any keys the spec hasn't yet typed.
    if (ext && Object.keys(ext).length > 0) {
      out.clientExtensionResults = { ...ext } as Record<string, unknown>;
    }
  } catch {
    // Polyfill / old browser without the helper. Nothing to forward.
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
 * Send the assertion back to the server. The session cookie is
 * stamped on the response by the server; the body only carries the
 * authenticated `user_id` (§03 "Login").
 */
export async function finishPasskeyLogin(
  challengeId: string,
  credential: PasskeyLoginCredential,
): Promise<PasskeyLoginFinish> {
  return fetchJson<PasskeyLoginFinish>("/api/v1/auth/passkey/login/finish", {
    method: "POST",
    body: { challenge_id: challengeId, credential },
  });
}

/**
 * Drive the full ceremony: begin → `navigator.credentials.get` →
 * finish. Translates the WebAuthn-spec error names into the
 * module's typed errors so the LoginPage can branch cleanly.
 *
 * The `signal` argument lets the LoginPage cancel an in-flight
 * conditional-UI ceremony when the user navigates away — the browser
 * will reject the `get()` promise with `AbortError`, which we
 * surface as `PasskeyCancelledError`.
 */
export async function runPasskeyLoginCeremony(
  options: { signal?: AbortSignal; mediation?: CredentialMediationRequirement } = {},
): Promise<PasskeyLoginFinish> {
  if (typeof navigator === "undefined" || !navigator.credentials) {
    throw new PasskeyUnsupportedError(
      "This browser does not support WebAuthn passkeys.",
      "platform_unsupported",
    );
  }

  const begin = await beginPasskeyLogin();
  const publicKey = decodeRequestOptions(begin.options);

  let assertion: Credential | null;
  try {
    assertion = await navigator.credentials.get({
      publicKey,
      signal: options.signal,
      // Conditional UI is the §03 "Login" recommendation: the browser
      // surfaces the passkey selector silently from the username
      // field. Callers can override (e.g. an explicit "Use passkey"
      // button passes `mediation: "required"`).
      mediation: options.mediation ?? "optional",
    });
  } catch (err) {
    throw mapNavigatorError(err);
  }

  if (!isPublicKeyCredential(assertion)) {
    throw new PasskeyUnsupportedError(
      "Browser returned an unexpected credential type for passkey login.",
      "platform_unsupported",
    );
  }

  const encoded = encodeAssertion(assertion);
  return finishPasskeyLogin(begin.challenge_id, encoded);
}

/**
 * Type-guard that doesn't depend on `PublicKeyCredential` being a
 * resolvable global — jsdom omits it entirely, so a bare
 * `instanceof PublicKeyCredential` would be a `ReferenceError` in
 * tests. The runtime check is structural: every WebAuthn assertion
 * carries the documented `id`, `rawId`, `response`, and
 * `getClientExtensionResults` fields.
 */
function isPublicKeyCredential(value: unknown): value is PublicKeyCredential {
  if (!value || typeof value !== "object") return false;
  const v = value as { type?: unknown; rawId?: unknown; response?: unknown };
  return v.type === "public-key" && v.rawId instanceof ArrayBuffer && typeof v.response === "object";
}

// ── Internals ─────────────────────────────────────────────────────

/**
 * Translate a WebAuthn ceremony failure into one of the module's
 * typed errors. Shared between the login (`get()`) and register
 * (`create()`) ceremonies — both surfaces emit the same DOMException
 * names per the spec, so the mapping table is identical.
 *
 * Exported (under-the-hood, no `index.ts` re-export) so
 * `passkey-register.ts` can reuse the table without copy-pasting it.
 */
export function mapNavigatorError(err: unknown): Error {
  if (err instanceof DOMException) {
    // `NotAllowedError` covers both user-cancel and authenticator
    // refusal — they're indistinguishable per the WebAuthn spec.
    if (err.name === "NotAllowedError" || err.name === "AbortError") {
      return new PasskeyCancelledError(err);
    }
    if (err.name === "TimeoutError") {
      return new PasskeyTimeoutError(err);
    }
    if (err.name === "UnknownError" || err.name === "NetworkError") {
      return new PasskeyTransientError(err.message || err.name, err);
    }
    if (err.name === "SecurityError") {
      return new PasskeyUnsupportedError(err.message || err.name, "security", err);
    }
    if (err.name === "NotSupportedError") {
      return new PasskeyUnsupportedError(err.message || err.name, "platform_unsupported", err);
    }
    if (err.name === "InvalidStateError") {
      return new PasskeyUnsupportedError(err.message || err.name, "invalid_state", err);
    }
    if (err.name === "ConstraintError") {
      return new PasskeyUnsupportedError(err.message || err.name, "constraint", err);
    }
  }
  // Last-resort: surface the raw error so devtools still show the
  // stack. The LoginPage falls back to a generic "could not sign in"
  // message rather than leaking the underlying string.
  return err instanceof Error ? err : new Error(String(err));
}

function base64UrlToBytes(value: string): ArrayBuffer {
  // Tolerate accidental padded-base64 (`+`, `/`, `=`) input from a
  // server that hasn't been updated to emit base64url — same posture
  // the magic-link decoder takes server-side.
  const padded = value.replace(/-/g, "+").replace(/_/g, "/");
  const pad = padded.length % 4 === 0 ? padded : padded + "=".repeat(4 - (padded.length % 4));
  const binary = atob(pad);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

function bytesToBase64Url(buffer: ArrayBuffer | ArrayBufferView): string {
  const view = buffer instanceof ArrayBuffer ? new Uint8Array(buffer) : new Uint8Array(buffer.buffer);
  // Build the binary string in chunks so we don't blow the
  // call stack on `String.fromCharCode(...big_array)`. 0x8000 is
  // the de-facto safe ceiling.
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < view.length; i += chunk) {
    binary += String.fromCharCode(...view.subarray(i, i + chunk));
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
