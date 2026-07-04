// Shared helpers for turning an ApiError into user-facing strings and stable
// DOM ids. Kept out of displayError.ts on purpose: that module duck-types
// ApiError to avoid a runtime import cycle with api.ts (which re-exports
// toDisplayError). This module has no such constraint because api.ts never
// imports it back.

import { ApiError, type ProblemFieldError } from "@/lib/api";

type FieldLoc = ProblemFieldError["loc"];

/**
 * Human-facing message for an error, preferring server-provided text. For an
 * ApiError, walks user_message -> detail -> title -> message; otherwise uses a
 * non-empty Error message; else the caller's fallback.
 */
export function apiErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    return error.userMessage ?? error.detail ?? error.title ?? error.message ?? fallback;
  }
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

/** Stable, CSS-selector-safe DOM id for a per-row field error message. */
export function fieldErrorId(prefix: string, rowId: string, field: string): string {
  return prefix + "-" + rowId.replace(/[^a-zA-Z0-9_-]/g, "-") + "-" + field.replaceAll("_", "-") + "-error";
}

/**
 * Trimmed field-error messages from an ApiError, optionally prefixed with a
 * human field label. Empty/whitespace messages are dropped.
 */
export function labeledFieldMessages(
  error: ApiError,
  labelFor?: (loc: FieldLoc) => string | null,
): string[] {
  return error.fieldErrors
    .map((fieldError) => {
      const message = fieldError.msg?.trim();
      if (!message) return null;
      const label = labelFor?.(fieldError.loc) ?? null;
      return label ? `${label}: ${message}` : message;
    })
    .filter((message): message is string => Boolean(message));
}

/**
 * Builds a per-field error map from an ApiError, keyed by a caller-supplied
 * loc -> field mapper. Non-ApiError inputs and unmapped/empty entries are
 * skipped.
 */
export function fieldErrorsByLoc<TField extends string>(
  error: unknown,
  fromLoc: (loc: FieldLoc) => TField | null,
): Partial<Record<TField, string>> {
  if (!(error instanceof ApiError)) return {};
  const errors: Partial<Record<TField, string>> = {};
  for (const fieldError of error.fieldErrors) {
    const field = fromLoc(fieldError.loc);
    const message = fieldError.msg?.trim();
    if (field && message) errors[field] = message;
  }
  return errors;
}
