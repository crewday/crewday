import { describe, expect, it } from "vitest";
import {
  PasskeyCancelledError,
  PasskeyTimeoutError,
  PasskeyTransientError,
  PasskeyUnsupportedError,
} from "@/auth/passkey";
import { ApiError } from "@/lib/api";
import {
  isSignupCaptchaError,
  messageForLoginError,
  messageForRecoveryEnrollError,
  messageForRecoveryVerifyError,
  messageForSignupEnrollError,
  messageForSignupSlugError,
  readSignupEnrollHandoff,
  stateForSignupError,
  type SlugErrorKind,
} from "./publicAuthMappers";

function api(status: number, body: unknown = {}): ApiError {
  return new ApiError("fallback api message", status, body);
}

describe("public auth mapper helpers", () => {
  it.each([
    ["slug_taken", "That workspace handle is already in use."],
    ["slug_reserved", "That handle is reserved by crew.day. Try another."],
    [
      "slug_in_grace_period",
      "That handle was recently released and is held for 30 days before reuse. Try another.",
    ],
  ] as const)("maps %s slug errors", (kind, message) => {
    expect(messageForSignupSlugError({ kind })).toBe(message);
  });

  it("includes the colliding slug only when present on homoglyph errors", () => {
    expect(
      messageForSignupSlugError({
        kind: "slug_homoglyph_collision",
        collidingSlug: "vi11a",
      }),
    ).toBe("That handle is too close to an existing workspace (vi11a). Try another.");
    expect(messageForSignupSlugError({ kind: "slug_homoglyph_collision" })).toBe(
      "That handle is too close to an existing workspace. Try another.",
    );
  });

  it("turns signup API failures into form states without leaking server detail", () => {
    expect(stateForSignupError(api(404))).toEqual({ kind: "closed" });
    expect(stateForSignupError(api(409, { detail: { error: "slug_taken" } }))).toEqual({
      kind: "slug_error",
      error: { kind: "slug_taken" },
    });
    expect(
      stateForSignupError(api(409, {
        detail: {
          error: "slug_homoglyph_collision",
          suggested_alternative: "villa-2",
          colliding_slug: "vi11a",
        },
      })),
    ).toEqual({
      kind: "slug_error",
      error: {
        kind: "slug_homoglyph_collision",
        suggestion: "villa-2",
        collidingSlug: "vi11a",
      },
    });
    expect(stateForSignupError(api(422, { detail: { error: "captcha_required" } }))).toEqual({
      kind: "error",
      message:
        "This deployment requires a CAPTCHA, which isn't available in this build. "
        + "Ask your operator to configure the CAPTCHA widget or contact them for an invite.",
    });
    expect(stateForSignupError(api(422, { detail: { error: "captcha_failed" } }), true)).toEqual({
      kind: "error",
      message: "The CAPTCHA check didn't complete. Try it again.",
    });
    expect(stateForSignupError(api(500, { detail: "Boom" }))).toEqual({
      kind: "error",
      message: "We couldn't reach the signup service. Try again in a moment.",
    });
  });

  it.each(["captcha_required", "captcha_failed"] as const)(
    "recognises %s as a signup captcha error",
    (error) => {
      expect(isSignupCaptchaError(api(422, { detail: { error } }))).toBe(true);
    },
  );

  it.each([
    [new PasskeyCancelledError(), "Passkey prompt closed", "info"],
    [new PasskeyTimeoutError(), "didn't respond in time", "info"],
    [new PasskeyTransientError("transport failed"), "Couldn't reach your authenticator", "danger"],
    [
      new PasskeyUnsupportedError("security", "security"),
      "insecure context",
      "danger",
    ],
    [api(429), "Too many sign-in attempts", "danger"],
    [api(401), "isn't registered for this workspace", "danger"],
    [api(500, { title: "Server unhappy" }), "Server unhappy", "danger"],
  ])("maps login error %#", (err, message, tone) => {
    expect(messageForLoginError(err)).toMatchObject({ tone });
    expect(messageForLoginError(err).message).toContain(message);
  });

  it("reads only complete signup enroll handoff state", () => {
    expect(readSignupEnrollHandoff({
      signupSessionId: "ss_1",
      desiredSlug: "villa",
    })).toEqual({ signupSessionId: "ss_1", desiredSlug: "villa" });
    expect(readSignupEnrollHandoff({ signupSessionId: "ss_1" })).toBeNull();
    expect(readSignupEnrollHandoff({ desiredSlug: "villa" })).toBeNull();
    expect(readSignupEnrollHandoff(null)).toBeNull();
  });

  it.each([
    [new PasskeyCancelledError(), "Create my workspace", "info"],
    [new PasskeyTimeoutError(), "Create my workspace", "info"],
    [new PasskeyTransientError("transport failed"), "signup link stays valid", "danger"],
    [
      new PasskeyUnsupportedError("duplicate", "invalid_state"),
      "already has a passkey registered",
      "danger",
    ],
    [api(404), "signup session has expired", "danger"],
    [api(409), "claimed by someone else", "danger"],
    [api(429), "Too many attempts", "danger"],
  ])("maps signup enroll error %#", (err, message, tone) => {
    expect(messageForSignupEnrollError(err)).toMatchObject({ tone });
    expect(messageForSignupEnrollError(err).message).toContain(message);
  });

  it.each([400, 404, 409, 410])("marks recovery verify status %s retryable", (status) => {
    expect(messageForRecoveryVerifyError(api(status))).toEqual({
      message: "This recovery link is expired, already used, or invalid. Request a new one below.",
      canRetry: true,
    });
  });

  it("maps recovery verify and enroll fallbacks", () => {
    expect(messageForRecoveryVerifyError(api(429)).message).toContain("Too many recovery attempts");
    expect(messageForRecoveryVerifyError(new Error("boom")).message).toContain(
      "couldn't verify the recovery link",
    );
    expect(messageForRecoveryEnrollError(api(404)).message).toContain(
      "recovery session has expired",
    );
    expect(
      messageForRecoveryEnrollError(
        new PasskeyUnsupportedError("constraint", "constraint"),
      ).message,
    ).toContain("can't satisfy the passkey requirements");
  });

  it.each([
    "slug_taken",
    "slug_reserved",
    "slug_homoglyph_collision",
    "slug_in_grace_period",
  ] as readonly SlugErrorKind[])("keeps every slug kind accepted by state mapper: %s", (error) => {
    expect(stateForSignupError(api(409, { detail: { error } })).kind).toBe("slug_error");
  });
});
