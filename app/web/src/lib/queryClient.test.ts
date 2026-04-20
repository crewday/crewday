import { describe, expect, it } from "vitest";
import { makeQueryClient } from "@/lib/queryClient";
import { ApiError } from "@/lib/api";

describe("makeQueryClient defaults", () => {
  it("sets the spec-defined query defaults (§14 Data layer)", () => {
    const qc = makeQueryClient();
    const q = qc.getDefaultOptions().queries;
    expect(q?.staleTime).toBe(30_000);
    expect(q?.gcTime).toBe(5 * 60_000);
    expect(q?.refetchOnWindowFocus).toBe(false);
  });

  it("retries transient failures up to twice", () => {
    const qc = makeQueryClient();
    const retry = qc.getDefaultOptions().queries?.retry as (count: number, error: unknown) => boolean;
    // Network errors (no status) retry twice then stop.
    expect(retry(0, new Error("network"))).toBe(true);
    expect(retry(1, new Error("network"))).toBe(true);
    expect(retry(2, new Error("network"))).toBe(false);
  });

  it("does not retry 4xx responses — those are client-side bugs", () => {
    const qc = makeQueryClient();
    const retry = qc.getDefaultOptions().queries?.retry as (count: number, error: unknown) => boolean;
    const notFound = new ApiError("Not found", 404, { detail: "Not found" });
    const badRequest = new ApiError("Bad request", 400, { detail: "Bad request" });
    expect(retry(0, notFound)).toBe(false);
    expect(retry(0, badRequest)).toBe(false);
  });

  it("retries 5xx responses", () => {
    const qc = makeQueryClient();
    const retry = qc.getDefaultOptions().queries?.retry as (count: number, error: unknown) => boolean;
    const boom = new ApiError("Oops", 503, { detail: "down" });
    expect(retry(0, boom)).toBe(true);
    expect(retry(2, boom)).toBe(false);
  });

  it("recognises the `status` field on non-ApiError errors too", () => {
    const qc = makeQueryClient();
    const retry = qc.getDefaultOptions().queries?.retry as (count: number, error: unknown) => boolean;
    // Some transports attach `.status` without subclassing ApiError;
    // the retry policy should still skip 4xx.
    expect(retry(0, { status: 401 })).toBe(false);
    expect(retry(0, { status: 500 })).toBe(true);
  });

  it("applies exponential backoff capped at 30s", () => {
    const qc = makeQueryClient();
    const delay = qc.getDefaultOptions().queries?.retryDelay as (attempt: number) => number;
    expect(delay(0)).toBe(500); // 500ms
    expect(delay(1)).toBe(1_000); // 1s
    expect(delay(2)).toBe(2_000); // 2s
    // Cap kicks in before the series would overflow.
    expect(delay(10)).toBe(30_000);
  });

  it("produces strictly non-decreasing backoff up to the cap (no zero-delay retry storm)", () => {
    const qc = makeQueryClient();
    const delay = qc.getDefaultOptions().queries?.retryDelay as (attempt: number) => number;
    let last = -1;
    for (let i = 0; i <= 12; i++) {
      const d = delay(i);
      expect(d).toBeGreaterThanOrEqual(last);
      expect(d).toBeGreaterThan(0);
      expect(d).toBeLessThanOrEqual(30_000);
      last = d;
    }
  });

  it("treats 499 as client-side (no retry) and 500 as server-side (retry)", () => {
    // Guards the boundary of the 4xx skip range. Fence-post regressions
    // here would either hammer a real 4xx or silently swallow a 5xx.
    const qc = makeQueryClient();
    const retry = qc.getDefaultOptions().queries?.retry as (count: number, error: unknown) => boolean;
    expect(retry(0, new ApiError("conflict", 499, {}))).toBe(false);
    expect(retry(0, new ApiError("boom", 500, {}))).toBe(true);
  });

  it("handles a null / undefined error by falling through to retry (no status = network blip)", () => {
    const qc = makeQueryClient();
    const retry = qc.getDefaultOptions().queries?.retry as (count: number, error: unknown) => boolean;
    // AbortController rejections, CORS preflight failures, and certain
    // browser extensions surface as `null`/`undefined` to TanStack. They
    // should follow the network-blip path, not the 4xx skip.
    expect(retry(0, null)).toBe(true);
    expect(retry(1, undefined)).toBe(true);
    expect(retry(2, null)).toBe(false);
  });

  it("ignores a non-numeric `status` field rather than crashing the retry decision", () => {
    // Defensive: if a transport attaches `status: "500"` (string) the
    // classifier must fall through to the retry-as-blip path, not throw.
    const qc = makeQueryClient();
    const retry = qc.getDefaultOptions().queries?.retry as (count: number, error: unknown) => boolean;
    expect(retry(0, { status: "500" })).toBe(true);
  });

  it("disables retry on mutations — optimistic rollback must fire exactly once", () => {
    const qc = makeQueryClient();
    expect(qc.getDefaultOptions().mutations?.retry).toBe(0);
  });
});
