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
