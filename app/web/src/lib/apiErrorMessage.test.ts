import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";
import {
  apiErrorMessage,
  fieldErrorId,
  fieldErrorsByLoc,
  labeledFieldMessages,
} from "@/lib/apiErrorMessage";

function apiError(body: unknown, message = "boom", status = 422): ApiError {
  return new ApiError(message, status, body);
}

describe("apiErrorMessage", () => {
  it("prefers user_message over detail/title/message for an ApiError", () => {
    const err = apiError({ user_message: "U", detail: "D", title: "T" });
    expect(apiErrorMessage(err, "fallback")).toBe("U");
  });

  it("walks detail then title when user_message is absent", () => {
    expect(apiErrorMessage(apiError({ detail: "D", title: "T" }), "fb")).toBe("D");
    expect(apiErrorMessage(apiError({ title: "T" }), "fb")).toBe("T");
  });

  it("falls back to the ApiError message when the problem body carries no text", () => {
    expect(apiErrorMessage(apiError(null, "raw message"), "fb")).toBe("raw message");
  });

  it("uses a non-empty Error message for plain errors", () => {
    expect(apiErrorMessage(new Error("plain"), "fb")).toBe("plain");
  });

  it("uses the fallback for empty errors and non-errors", () => {
    expect(apiErrorMessage(new Error(""), "fb")).toBe("fb");
    expect(apiErrorMessage("nope", "fb")).toBe("fb");
  });
});

describe("fieldErrorId", () => {
  it("builds a stable, selector-safe id", () => {
    expect(fieldErrorId("asset-type", "abc123", "default_lifespan_years")).toBe(
      "asset-type-abc123-default-lifespan-years-error",
    );
  });

  it("sanitizes disallowed characters in the row id", () => {
    expect(fieldErrorId("work-role", "id/with.dots", "name")).toBe(
      "work-role-id-with-dots-name-error",
    );
  });
});

describe("labeledFieldMessages", () => {
  const err = apiError({
    errors: [
      { loc: ["body", "email"], msg: "  invalid  " },
      { loc: ["body", "name"], msg: "   " },
      { loc: ["body", "grants"], msg: "required" },
    ],
  });

  it("trims and drops empty messages", () => {
    expect(labeledFieldMessages(err)).toEqual(["invalid", "required"]);
  });

  it("prefixes with a label when the mapper returns one", () => {
    const labelFor = (loc: readonly (string | number)[] | undefined) =>
      loc?.at(-1) === "email" ? "Email" : null;
    expect(labeledFieldMessages(err, labelFor)).toEqual(["Email: invalid", "required"]);
  });
});

describe("fieldErrorsByLoc", () => {
  const fromLoc = (loc: readonly (string | number)[] | undefined): "name" | "key" | null => {
    const field = loc?.at(-1);
    return field === "name" || field === "key" ? field : null;
  };

  it("maps recognized locs to trimmed messages", () => {
    const err = apiError({
      errors: [
        { loc: ["body", "name"], msg: "  too short  " },
        { loc: ["body", "key"], msg: "taken" },
        { loc: ["body", "other"], msg: "ignored" },
        { loc: ["body", "key"], msg: "   " },
      ],
    });
    expect(fieldErrorsByLoc(err, fromLoc)).toEqual({ name: "too short", key: "taken" });
  });

  it("returns an empty object for non-ApiError input", () => {
    expect(fieldErrorsByLoc(new Error("x"), fromLoc)).toEqual({});
  });
});
