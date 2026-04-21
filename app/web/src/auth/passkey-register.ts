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
// Wire shape mirrors `app/api/v1/auth/recovery.py`'s
// `RecoveryPasskeyStartResponse` (challenge_id + options) and
// `RecoveryFinishBody` (recovery_session_id + challenge_id +
// credential). The session cookie is stamped on the finish
// response (`Set-Cookie: __Host-crewday_session=...`).

import { fetchJson } from "@/lib/api";
import {
  PasskeyCancelledError,
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
 * Drive the full recovery-enrol ceremony: start → create() → finish.
 * Callers supply the `recovery_session_id` obtained from the verify
 * step. Translates platform errors into the module's typed errors
 * (`PasskeyCancelledError`, `PasskeyUnsupportedError`); anything
 * else propagates (usually `ApiError`).
 */
export async function runRecoveryEnrollCeremony(
  recoverySessionId: string,
  options: { signal?: AbortSignal } = {},
): Promise<RecoveryFinishResponse> {
  if (typeof navigator === "undefined" || !navigator.credentials) {
    throw new PasskeyUnsupportedError(
      "This browser does not support WebAuthn passkeys.",
    );
  }

  const begin = await beginRecoveryEnroll(recoverySessionId);
  const publicKey = decodeCreationOptions(begin.options);

  let attestation: Credential | null;
  try {
    attestation = await navigator.credentials.create({
      publicKey,
      signal: options.signal,
    });
  } catch (err) {
    throw mapNavigatorError(err);
  }

  if (!isPublicKeyCredential(attestation)) {
    throw new PasskeyUnsupportedError(
      "Browser returned an unexpected credential type for passkey registration.",
    );
  }

  const encoded = encodeAttestation(attestation);
  return finishRecoveryEnroll(recoverySessionId, begin.challenge_id, encoded);
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

function mapNavigatorError(err: unknown): Error {
  if (err instanceof DOMException) {
    if (err.name === "NotAllowedError" || err.name === "AbortError") {
      return new PasskeyCancelledError(err);
    }
    if (
      err.name === "SecurityError"
      || err.name === "NotSupportedError"
      || err.name === "InvalidStateError"
    ) {
      return new PasskeyUnsupportedError(err.message || err.name, err);
    }
  }
  return err instanceof Error ? err : new Error(String(err));
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
