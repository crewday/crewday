// §14 "Completed by <name>" concurrent-completion info toast — the
// SSE-driven supersession path (§06 last-write-wins). Covers: another
// user superseding this tab's completion fires the toast with the
// other user's resolved name; an own-completion echo does not; a frame
// for an occurrence this tab never completed does not; name resolution
// falls back to fetch-then-cache; and a resolution failure degrades to
// generic copy rather than dropping the signal.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";

vi.mock("@/lib/api", () => ({
  fetchJson: vi.fn(),
}));

import { fetchJson } from "@/lib/api";
import { dispatchSseEvent, type SseEvent } from "@/lib/sse";
import {
  __resetRecentCompletionsForTests,
  recordSelfCompletion,
} from "@/lib/recentCompletions";
import {
  __resetQueryKeyGetterForTests,
  qk,
  registerQueryKeyWorkspaceGetter,
} from "@/lib/queryKeys";
import {
  setGlobalStatusToastHandler,
  type StatusToastEvent,
} from "@/lib/statusToastBus";

const fetchJsonMock = vi.mocked(fetchJson);

const TASK_ID = "task_1";
const ME = "user_alice";

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
}

function completedFrame(completedBy: string): SseEvent {
  return {
    id: "tok-1",
    kind: "task.completed",
    workspace_id: "w_test",
    data: { task_id: TASK_ID, completed_by: completedBy },
  };
}

let toasts: StatusToastEvent[];
let unsubscribe: () => void;

beforeEach(() => {
  __resetQueryKeyGetterForTests();
  registerQueryKeyWorkspaceGetter(() => "acme");
  __resetRecentCompletionsForTests();
  fetchJsonMock.mockReset();
  toasts = [];
  unsubscribe = setGlobalStatusToastHandler((event) => toasts.push(event));
});

afterEach(() => {
  unsubscribe();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("task.completed supersession toast", () => {
  it("toasts 'Completed by <name>' when another user supersedes this tab's completion", async () => {
    const qc = makeClient();
    qc.setQueryData(qk.user("user_bob"), { user: { display_name: "Bob Stone" } });
    recordSelfCompletion(TASK_ID, ME);

    dispatchSseEvent(completedFrame("user_bob"), qc);

    await vi.waitFor(() => {
      expect(toasts).toContainEqual({ message: "Completed by Bob Stone", tone: "info" });
    });
    // Name came from cache — no REST round trip.
    expect(fetchJsonMock).not.toHaveBeenCalled();
  });

  it("does not toast on the current user's own completion echo", async () => {
    const qc = makeClient();
    recordSelfCompletion(TASK_ID, ME);

    dispatchSseEvent(completedFrame(ME), qc);

    await Promise.resolve();
    await Promise.resolve();
    expect(toasts).toHaveLength(0);
  });

  it("does not toast for an occurrence this tab never completed", async () => {
    const qc = makeClient();

    dispatchSseEvent(completedFrame("user_bob"), qc);

    await Promise.resolve();
    await Promise.resolve();
    expect(toasts).toHaveLength(0);
  });

  it("resolves the name via /users/{id} when it is not cached", async () => {
    const qc = makeClient();
    fetchJsonMock.mockResolvedValue({ user: { display_name: "Casey Lane" } });
    recordSelfCompletion(TASK_ID, ME);

    dispatchSseEvent(completedFrame("user_casey"), qc);

    await vi.waitFor(() => {
      expect(toasts).toContainEqual({ message: "Completed by Casey Lane", tone: "info" });
    });
    expect(fetchJsonMock).toHaveBeenCalledWith("/api/v1/users/user_casey");
  });

  it("falls back to generic copy when the name cannot be resolved", async () => {
    const qc = makeClient();
    fetchJsonMock.mockRejectedValue(new Error("network"));
    recordSelfCompletion(TASK_ID, ME);

    dispatchSseEvent(completedFrame("user_ghost"), qc);

    await vi.waitFor(() => {
      expect(toasts).toContainEqual({ message: "Completed by another teammate", tone: "info" });
    });
  });

  it("only toasts once — a duplicate supersession frame is ignored", async () => {
    const qc = makeClient();
    qc.setQueryData(qk.user("user_bob"), { user: { display_name: "Bob Stone" } });
    recordSelfCompletion(TASK_ID, ME);

    dispatchSseEvent(completedFrame("user_bob"), qc);
    dispatchSseEvent(completedFrame("user_bob"), qc);

    await vi.waitFor(() => {
      expect(toasts).toHaveLength(1);
    });
  });
});
