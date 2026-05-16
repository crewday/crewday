import type { ProblemDetail, ProblemFieldError } from "@/lib/api";

const DEFAULT_FALLBACK = "Something went wrong.";

export interface DisplayErrorDetail {
  label: string;
  message: string;
  path: string | null;
  type: string | null;
}

export interface DisplayError {
  id: string | null;
  message: string;
  status: number | null;
  type: string | null;
  title: string | null;
  machineCode: string | null;
  instance: string | null;
  fieldErrors: ReadonlyArray<ProblemFieldError>;
  requestId: string | null;
  raw: unknown;
  details: ReadonlyArray<DisplayErrorDetail>;
}

interface ApiErrorLike extends Error {
  status: number;
  body: unknown;
  problem: ProblemDetail | null;
  requestId: string | null;
  type: string | null;
  title: string | null;
  detail: string | null;
  fieldErrors: ReadonlyArray<ProblemFieldError>;
  errorId?: string | null;
  userMessage?: string | null;
  machineCode?: string | null;
  instance?: string | null;
}

export function toDisplayError(error: unknown, fallback: string = DEFAULT_FALLBACK): DisplayError {
  const safeFallback = fallback || DEFAULT_FALLBACK;
  const apiError = asApiErrorLike(error);
  if (!apiError) {
    return {
      id: null,
      message: safeFallback,
      status: null,
      type: null,
      title: error instanceof Error ? nonEmptyString(error.name) : null,
      machineCode: null,
      instance: null,
      fieldErrors: [],
      requestId: null,
      raw: safeGenericRaw(error),
      details: genericDetails(error),
    };
  }

  const errorId = nonEmptyString(apiError.errorId) ?? nonEmptyString(apiError.problem?.error_id);
  const requestId = nonEmptyString(apiError.requestId);
  const title = nonEmptyString(apiError.title) ?? nonEmptyString(apiError.problem?.title);
  const detail = nonEmptyString(apiError.detail) ?? nonEmptyString(apiError.problem?.detail);
  const userMessage = nonEmptyString(apiError.userMessage) ?? nonEmptyString(apiError.problem?.user_message);
  const message = userMessage ?? detail ?? title ?? nonEmptyString(apiError.message) ?? safeFallback;
  const type = nonEmptyString(apiError.type) ?? shortProblemType(apiError.problem);
  const machineCode = nonEmptyString(apiError.machineCode) ?? nonEmptyString(apiError.problem?.error);
  const instance = nonEmptyString(apiError.instance) ?? nonEmptyString(apiError.problem?.instance);
  const fieldErrors = normalizedFieldErrors(apiError.fieldErrors);

  return {
    id: errorId ?? requestId,
    message,
    status: apiError.status,
    type,
    title,
    machineCode,
    instance,
    fieldErrors,
    requestId,
    raw: apiError.problem ?? apiError.body,
    details: apiDetails({
      detail,
      errorId,
      fieldErrors,
      instance,
      machineCode,
      requestId,
      status: apiError.status,
      title,
      type,
      userMessage,
    }),
  };
}

function asApiErrorLike(error: unknown): ApiErrorLike | null {
  if (!(error instanceof Error)) return null;
  const value = asRecord(error);
  if (!value) return null;
  if (error.name !== "ApiError") return null;
  if (typeof value.status !== "number") return null;
  if (!("body" in value) || !("problem" in value)) return null;
  const problem = value.problem === null || isProblemDetail(value.problem) ? value.problem : null;
  const fieldErrors = normalizedFieldErrors(value.fieldErrors);
  return {
    name: error.name,
    message: error.message,
    stack: error.stack,
    status: value.status,
    body: value.body,
    problem,
    requestId: nonEmptyString(value.requestId),
    type: nonEmptyString(value.type),
    title: nonEmptyString(value.title),
    detail: nonEmptyString(value.detail),
    fieldErrors,
    errorId: nonEmptyString(value.errorId),
    userMessage: nonEmptyString(value.userMessage),
    machineCode: nonEmptyString(value.machineCode),
    instance: nonEmptyString(value.instance),
  };
}

function apiDetails(input: {
  detail: string | null;
  errorId: string | null;
  fieldErrors: ReadonlyArray<ProblemFieldError>;
  instance: string | null;
  machineCode: string | null;
  requestId: string | null;
  status: number;
  title: string | null;
  type: string | null;
  userMessage: string | null;
}): ReadonlyArray<DisplayErrorDetail> {
  const details: DisplayErrorDetail[] = [];
  addDetail(details, "Status", String(input.status), null, null);
  addDetail(details, "Type", input.type, null, null);
  addDetail(details, "Title", input.title, null, null);
  addDetail(details, "Message", input.userMessage ?? input.detail, null, null);
  addDetail(details, "Machine code", input.machineCode, null, null);
  addDetail(details, "Instance", input.instance, null, null);
  addDetail(details, "Error ID", input.errorId, null, null);
  addDetail(details, "Request ID", input.requestId, null, null);

  for (const fieldError of input.fieldErrors) {
    addDetail(
      details,
      "Field error",
      nonEmptyString(fieldError.msg) ?? "Invalid field",
      formatLocation(fieldError.loc),
      nonEmptyString(fieldError.type),
    );
  }

  return details;
}

function genericDetails(error: unknown): ReadonlyArray<DisplayErrorDetail> {
  if (!(error instanceof Error)) return [];
  const details: DisplayErrorDetail[] = [];
  addDetail(details, "Error", nonEmptyString(error.name), null, null);
  return details;
}

function addDetail(
  details: DisplayErrorDetail[],
  label: string,
  message: string | null,
  path: string | null,
  type: string | null,
): void {
  if (!message) return;
  details.push({ label, message, path, type });
}

function normalizedFieldErrors(value: unknown): ReadonlyArray<ProblemFieldError> {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const record = asRecord(item);
    if (!record) return [];
    const loc = Array.isArray(record.loc)
      ? record.loc.filter((part: unknown): part is string | number => typeof part === "string" || typeof part === "number")
      : undefined;
    return [{
      loc,
      msg: nonEmptyString(record.msg) ?? undefined,
      type: nonEmptyString(record.type) ?? undefined,
    }];
  });
}

function formatLocation(loc: readonly (string | number)[] | undefined): string | null {
  if (!loc?.length) return null;
  return loc.map(String).join(".");
}

function safeGenericRaw(error: unknown): unknown {
  if (error === null) return null;
  if (typeof error === "string" || typeof error === "number" || typeof error === "boolean") return error;
  if (error instanceof Error) return { name: error.name, message: error.message };
  return null;
}

function shortProblemType(problem: ProblemDetail | null): string | null {
  const raw = nonEmptyString(problem?.type);
  if (!raw) return null;
  const match = raw.match(/\/errors\/([^/]+)$/);
  return match?.[1] ?? raw;
}

function isProblemDetail(value: unknown): value is ProblemDetail {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : null;
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}
