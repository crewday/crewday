import {
  PasskeyCancelledError,
  PasskeyTimeoutError,
  PasskeyTransientError,
  PasskeyUnsupportedError,
  type PasskeyUnsupportedKind,
} from "@/auth/passkey";
import { ApiError } from "@/lib/api";
import type { SignupEnrollHandoff } from "./SignupVerifyPage";

export type NoticeTone = "info" | "danger";

export interface ResolvedMessage {
  message: string;
  tone: NoticeTone;
}

export type SlugErrorKind =
  | "slug_taken"
  | "slug_reserved"
  | "slug_homoglyph_collision"
  | "slug_in_grace_period";

export interface SlugError {
  kind: SlugErrorKind;
  suggestion?: string;
  collidingSlug?: string;
}

export type SignupFormState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "sent" }
  | { kind: "closed" }
  | { kind: "slug_error"; error: SlugError }
  | { kind: "error"; message: string };

export interface VerifyMessage {
  message: string;
  canRetry: boolean;
}

interface ErrorDetail {
  error?: string;
  suggested_alternative?: string;
  colliding_slug?: string;
}

const SLUG_ERROR_MESSAGES: Record<Exclude<SlugErrorKind, "slug_homoglyph_collision">, string> = {
  slug_taken: "That workspace handle is already in use.",
  slug_reserved: "That handle is reserved by crew.day. Try another.",
  slug_in_grace_period:
    "That handle was recently released and is held for 30 days before reuse. Try another.",
};

const LOGIN_UNSUPPORTED_MESSAGES: Record<PasskeyUnsupportedKind, string> = {
  invalid_state:
    "This passkey is already registered or in an unexpected state. Try another device, or use the recovery link below.",
  constraint:
    "Your authenticator can't satisfy the passkey requirements for this workspace. Try another device, or use the recovery link below.",
  security:
    "This page can't run a passkey ceremony from an insecure context. Open crew.day over HTTPS and try again.",
  platform_unsupported:
    "This browser or device can't use a passkey here. Try another device, or use the recovery link below.",
};

const SIGNUP_ENROLL_UNSUPPORTED_MESSAGES: Record<PasskeyUnsupportedKind, string> = {
  invalid_state:
    "This device already has a passkey registered. Try another device — your signup link stays valid for 10 minutes.",
  constraint:
    "Your authenticator can't satisfy the passkey requirements for this workspace. Try another device — your signup link stays valid for 10 minutes.",
  security:
    "This page can't run a passkey ceremony from an insecure context. Open crew.day over HTTPS and try again.",
  platform_unsupported:
    "This browser or device can't register a passkey here. Try another device — your signup link stays valid for 10 minutes.",
};

const RECOVERY_ENROLL_UNSUPPORTED_MESSAGES: Record<PasskeyUnsupportedKind, string> = {
  invalid_state:
    "This device already has a passkey for your account. Try another device — the link stays valid for 10 minutes.",
  constraint:
    "Your authenticator can't satisfy the passkey requirements for this workspace. Try another device — the link stays valid for 10 minutes.",
  security:
    "This page can't run a passkey ceremony from an insecure context. Open crew.day over HTTPS and try again.",
  platform_unsupported:
    "This browser or device can't register a passkey here. Try another device — the link stays valid for 10 minutes.",
};

const RECOVERY_VERIFY_STATUS = new Set([400, 404, 409, 410]);

const apiLoginMessage = (err: ApiError): ResolvedMessage => {
  if (err.status === 429) return danger("Too many sign-in attempts. Wait a minute and try again.");
  if (err.status === 401 || err.status === 403) {
    return danger(
      "That passkey isn't registered for this workspace. Use recovery below to enrol a fresh device.",
    );
  }
  const surface = err.detail ?? err.title ?? err.message;
  return danger(surface && surface.trim() ? surface : "We couldn't sign you in. Try again in a moment.");
};

const slugStateFor = (err: ApiError): SignupFormState | null => {
  const detail = readDetail(err);
  if (!isSlugErrorKind(detail?.error)) return null;
  return {
    kind: "slug_error",
    error: {
      kind: detail.error,
      ...(detail.suggested_alternative ? { suggestion: detail.suggested_alternative } : {}),
      ...(detail.colliding_slug ? { collidingSlug: detail.colliding_slug } : {}),
    },
  };
};

const validationStateFor = (err: ApiError, captchaEnabled: boolean): SignupFormState => {
  const detail = readDetail(err);
  if (detail?.error === "captcha_required" || detail?.error === "captcha_failed") {
    return signupError(
      captchaEnabled
        ? "The CAPTCHA check didn't complete. Try it again."
        : "This deployment requires a CAPTCHA, which isn't available in this build. "
          + "Ask your operator to configure the CAPTCHA widget or contact them for an invite.",
    );
  }
  if (detail?.error === "disposable_email") {
    return signupError("We don't accept signups from throwaway email providers. Use a real email.");
  }
  if (detail?.error === "invalid_slug") {
    return signupError(
      "That workspace handle isn't valid. Use 3–40 lowercase letters, digits, or hyphens "
        + "(no leading or trailing hyphen).",
    );
  }
  return signupGenericError();
};

const readDetail = (err: ApiError): ErrorDetail | null => {
  const body = err.body;
  if (!isRecord(body)) return null;
  const detail = body.detail;
  return isRecord(detail) ? detail : body;
};

const isSlugErrorKind = (value: unknown): value is SlugErrorKind => {
  return (
    value === "slug_taken"
    || value === "slug_reserved"
    || value === "slug_homoglyph_collision"
    || value === "slug_in_grace_period"
  );
};

const signupGenericError = (): SignupFormState => {
  return signupError("We couldn't accept that. Check the form and try again.");
};

const signupError = (message: string): SignupFormState => {
  return { kind: "error", message };
};

const info = (message: string): ResolvedMessage => {
  return { message, tone: "info" };
};

const danger = (message: string): ResolvedMessage => {
  return { message, tone: "danger" };
};

const isRecord = (value: unknown): value is Record<string, unknown> => {
  return typeof value === "object" && value !== null && !Array.isArray(value);
};

const nonEmptyString = (value: unknown): string | null => {
  return typeof value === "string" && value ? value : null;
};

const passkeyBaseMessage = (
  err: unknown,
  action: string,
  transientMessage = "Couldn't reach your authenticator. Wait a moment and try again.",
): ResolvedMessage | null => {
  if (err instanceof PasskeyCancelledError) {
    return info(`Passkey prompt closed. Click “${action}” to try again.`);
  }
  if (err instanceof PasskeyTimeoutError) {
    return info(`Your authenticator didn't respond in time. Click “${action}” to try again.`);
  }
  if (err instanceof PasskeyTransientError) {
    return danger(transientMessage);
  }
  return null;
};

export function messageForSignupSlugError(error: SlugError): string {
  if (error.kind !== "slug_homoglyph_collision") return SLUG_ERROR_MESSAGES[error.kind];
  return error.collidingSlug
    ? `That handle is too close to an existing workspace (${error.collidingSlug}). Try another.`
    : "That handle is too close to an existing workspace. Try another.";
}

export function isSignupCaptchaError(err: unknown): boolean {
  if (!(err instanceof ApiError) || err.status !== 422) return false;
  const detail = readDetail(err);
  return detail?.error === "captcha_required" || detail?.error === "captcha_failed";
}

export function stateForSignupError(err: unknown, captchaEnabled = false): SignupFormState {
  if (!(err instanceof ApiError)) {
    return signupError("We couldn't reach the signup service. Try again in a moment.");
  }
  if (err.status === 404) return { kind: "closed" };
  if (err.status === 409) return slugStateFor(err) ?? signupGenericError();
  if (err.status === 422) return validationStateFor(err, captchaEnabled);
  if (err.status === 429) {
    return signupError("Too many signup attempts from this network. Wait a minute, then try again.");
  }
  return signupError("We couldn't reach the signup service. Try again in a moment.");
}

export function messageForLoginError(err: unknown): ResolvedMessage {
  const passkeyMessage = passkeyBaseMessage(err, "Use passkey");
  if (passkeyMessage) return passkeyMessage;
  if (err instanceof PasskeyUnsupportedError) {
    return danger(LOGIN_UNSUPPORTED_MESSAGES[err.kind]);
  }
  if (err instanceof ApiError) return apiLoginMessage(err);
  return danger("We couldn't sign you in. Try again in a moment.");
}

export function readSignupEnrollHandoff(state: unknown): SignupEnrollHandoff | null {
  if (!isRecord(state)) return null;
  const signupSessionId = nonEmptyString(state.signupSessionId);
  const desiredSlug = nonEmptyString(state.desiredSlug);
  return signupSessionId && desiredSlug ? { signupSessionId, desiredSlug } : null;
}

export function messageForSignupEnrollError(err: unknown): ResolvedMessage {
  const passkeyMessage = passkeyBaseMessage(
    err,
    "Create my workspace",
    "Couldn't reach your authenticator. Wait a moment and try again — your signup link stays valid for 10 minutes.",
  );
  if (passkeyMessage) return passkeyMessage;
  if (err instanceof PasskeyUnsupportedError) {
    return danger(SIGNUP_ENROLL_UNSUPPORTED_MESSAGES[err.kind]);
  }
  if (err instanceof ApiError) {
    if (err.status === 404) {
      return danger(
        "Your signup session has expired. Start over from the signup page to receive a fresh link.",
      );
    }
    if (err.status === 409) {
      return danger(
        "That workspace handle was claimed by someone else while you were enrolling. Start over and pick another.",
      );
    }
    if (err.status === 429) return danger("Too many attempts. Wait a minute and try again.");
  }
  return danger("We couldn't finish creating your workspace. Try again in a moment.");
}

export function messageForRecoveryEnrollError(err: unknown): ResolvedMessage {
  const passkeyMessage = passkeyBaseMessage(
    err,
    "Register passkey",
    "Couldn't reach your authenticator. Wait a moment and try again — the link stays valid for 10 minutes.",
  );
  if (passkeyMessage) return passkeyMessage;
  if (err instanceof PasskeyUnsupportedError) {
    return danger(RECOVERY_ENROLL_UNSUPPORTED_MESSAGES[err.kind]);
  }
  if (err instanceof ApiError) {
    if (err.status === 429) return danger("Too many register attempts. Wait a minute and try again.");
    if (err.status === 404) {
      return danger("Your recovery session has expired. Request a fresh link from the sign-in page.");
    }
  }
  return danger("We couldn't finish registering your passkey. Try again in a moment.");
}

export function messageForRecoveryVerifyError(err: unknown): VerifyMessage {
  if (err instanceof ApiError) {
    if (RECOVERY_VERIFY_STATUS.has(err.status)) {
      return {
        message: "This recovery link is expired, already used, or invalid. Request a new one below.",
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
