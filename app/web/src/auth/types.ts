// crewday — auth module types.
//
// Wire shape mirrors `GET /api/v1/auth/me` (§12) and the passkey
// ceremony endpoints (§03 "WebAuthn specifics", §"Login"). The
// production server is in flight; these shapes are the contract the
// SPA depends on and will move into `@/types/auth.ts` once §12 lands
// the response schema there. Keeping them local for now means the
// auth module can compile and unit-test without blocking on the
// upstream PR.

import type { AvailableWorkspace } from "@/types/auth";

/**
 * Minimal "who is logged in" envelope returned by
 * `GET /api/v1/auth/me`. The bare-host variant of `/me` is identity-
 * only (no workspace context) — we can't return the full `Me`
 * because the caller may not have picked a workspace yet.
 *
 * Property notes:
 * - `user_id`: the canonical ULID of the authenticated user. Stable
 *   across email changes and grant churn.
 * - `display_name` / `email`: surfaced in the user menu so the chrome
 *   doesn't need a second fetch before paint.
 * - `available_workspaces`: drives `<WorkspaceGate>` and the
 *   `/select-workspace` page (§14) without a follow-up to
 *   `/me/workspaces`. May be empty for a brand-new account whose
 *   first grant invitation is still pending — that case lands on the
 *   "no workspaces yet" empty state.
 * - `current_workspace_id`: the slug the **server** thinks is active
 *   (read from the `crewday_workspace` cookie). When present and the
 *   client hasn't picked one yet, `<WorkspaceGate>` adopts it
 *   silently.
 * - `is_deployment_admin`: mirrors the same flag on the
 *   workspace-scoped `Me` envelope (§02, §05). Surfaced here so a
 *   deployment admin who has zero workspace grants can still
 *   discover the `/admin/dashboard` deep-link from the
 *   `<WorkspaceGate>` "no workspaces yet" empty state — they would
 *   otherwise never reach the workspace-scoped `/api/v1/me` that
 *   carries the same flag.
 */
export interface AuthMe {
  user_id: string;
  display_name: string;
  email: string;
  available_workspaces: AvailableWorkspace[];
  current_workspace_id: string | null;
  is_deployment_admin: boolean;
}

/**
 * Successful response from `POST /api/v1/auth/passkey/login/finish`.
 * The session cookie is delivered via a `Set-Cookie` header, not in
 * the body — the SPA never sees the cookie value (HTTP-only). All
 * the body needs to carry is the user id so the client can decide
 * whether to call `/auth/me` or trust a previously-cached envelope.
 */
export interface PasskeyLoginFinish {
  user_id: string;
}

/**
 * Response from `POST /api/v1/auth/passkey/login/start`. `options` is
 * a `PublicKeyCredentialRequestOptionsJSON` payload (per the WebAuthn
 * Level 3 IDL): every `BufferSource` field is base64url-encoded as a
 * string; the browser turns it back into bytes via
 * `PublicKeyCredential.parseRequestOptionsFromJSON()` (where
 * available) or our manual decoder.
 */
export interface PasskeyLoginStart {
  challenge_id: string;
  options: PublicKeyCredentialRequestOptionsJSON;
}

/**
 * JSON-serialisable subset of `PublicKeyCredentialRequestOptions`
 * that survives a JSON round-trip. The server emits this shape
 * verbatim (see `LoginStartResponse` in `app/api/v1/auth/passkey.py`).
 *
 * Marked as a permissive `Record` envelope because the spec evolves
 * (extensions, hints, future fields) and we don't want to break the
 * SPA every time a new optional key arrives. The fields the browser
 * consumes (`challenge`, `allowCredentials[].id`) are decoded via
 * `decodeRequestOptions()` below.
 */
export type PasskeyLoginCredential = {
  id: string;
  rawId: string;
  type: "public-key";
  response: {
    authenticatorData: string;
    clientDataJSON: string;
    signature: string;
    userHandle: string | null;
  };
  clientExtensionResults?: Record<string, unknown>;
  authenticatorAttachment?: "platform" | "cross-platform" | null;
};

export interface PublicKeyCredentialDescriptorJSON {
  id: string;
  type: "public-key";
  transports?: ReadonlyArray<"usb" | "nfc" | "ble" | "internal" | "hybrid" | "smart-card">;
}

export interface PublicKeyCredentialRequestOptionsJSON {
  challenge: string;
  rpId?: string;
  timeout?: number;
  userVerification?: "required" | "preferred" | "discouraged";
  allowCredentials?: ReadonlyArray<PublicKeyCredentialDescriptorJSON>;
  // Extension fields the spec keeps adding — pass through.
  [extensionKey: string]: unknown;
}
