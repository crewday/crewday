// Unit tests for the mocks SSE dispatcher.
//
// Scope: a workspace-scope `llm.assignment.changed` frame must drop
// the admin LLM graph cache (§11 LLM router cache invalidation, see
// `app/domain/llm/router.py`). The dispatcher is a single `switch`
// per kind; we exercise the relevant case directly through the
// exported `dispatch` helper rather than spinning up an
// `EventSource` polyfill.

import { describe, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import { dispatch } from "./sse";
import { qk } from "./queryKeys";

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
}

describe("mocks SSE dispatcher — llm.assignment.changed", () => {
  it("invalidates the admin LLM graph key", () => {
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    dispatch(qc, {
      type: "llm.assignment.changed",
      data: JSON.stringify({ workspace_id: "ws_test" }),
    });
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(expect.arrayContaining([qk.adminLlmGraph()]));
  });
});

describe("mocks SSE dispatcher — task_template lifecycle (cd-wyq5)", () => {
  it("upserted invalidates the template catalog only", () => {
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    dispatch(qc, {
      type: "task_template.upserted",
      data: JSON.stringify({ template_id: "tpl_1" }),
    });
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(expect.arrayContaining([qk.taskTemplates()]));
    expect(called).not.toEqual(expect.arrayContaining([qk.schedules()]));
  });

  it("deleted invalidates templates AND schedules", () => {
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    dispatch(qc, {
      type: "task_template.deleted",
      data: JSON.stringify({ template_id: "tpl_1" }),
    });
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(
      expect.arrayContaining([qk.taskTemplates(), qk.schedules()]),
    );
  });
});

describe("mocks SSE dispatcher — API token lifecycle", () => {
  it("created invalidates the token list", () => {
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    dispatch(qc, {
      type: "api_token.created",
      data: JSON.stringify({ id: "tok_1", kind: "scoped" }),
    });
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(expect.arrayContaining([qk.apiTokens()]));
  });

  it("revoked and rotated invalidate the token list and per-token audit", () => {
    for (const type of ["api_token.revoked", "api_token.rotated"] as const) {
      const qc = makeClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      dispatch(qc, {
        type,
        data: JSON.stringify({ id: "tok_1" }),
      });
      const called = spy.mock.calls.map((c) => c[0]?.queryKey);
      expect(called).toEqual(
        expect.arrayContaining([qk.apiTokens(), qk.apiTokenAudit("tok_1")]),
      );
    }
  });

  it("revoked and rotated skip audit invalidation without a token id", () => {
    for (const type of ["api_token.revoked", "api_token.rotated"] as const) {
      const qc = makeClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      dispatch(qc, {
        type,
        data: JSON.stringify({}),
      });
      const called = spy.mock.calls.map((c) => c[0]?.queryKey);
      expect(called).toEqual(expect.arrayContaining([qk.apiTokens()]));
      expect(called).not.toEqual(
        expect.arrayContaining([qk.apiTokenAudit("tok_1")]),
      );
    }
  });
});
