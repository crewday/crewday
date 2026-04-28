import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import type { ReactElement } from "react";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import TaskDetailPage from "./TaskDetailPage";

interface FakeResponse {
  status?: number;
  body: unknown;
}

interface FetchCall {
  url: string;
  init: RequestInit;
}

function installFetch(scripted: Record<string, FakeResponse[]>): {
  calls: FetchCall[];
  restore: () => void;
} {
  const calls: FetchCall[] = [];
  const original = globalThis.fetch;
  const queues: Record<string, FakeResponse[]> = {};
  for (const [suffix, responses] of Object.entries(scripted)) {
    queues[suffix] = [...responses];
  }
  const suffixes = Object.keys(queues).sort((a, b) => b.length - a.length);
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    const suffix = suffixes.find((candidate) => resolved.endsWith(candidate));
    if (!suffix) throw new Error(`Unscripted fetch: ${resolved}`);
    const next = queues[suffix]!.shift();
    if (!next) throw new Error(`No more responses for: ${resolved}`);
    const status = next.status ?? 200;
    const ok = status >= 200 && status < 300;
    return {
      ok,
      status,
      statusText: ok ? "OK" : "Error",
      text: async () => JSON.stringify(next.body),
    } as unknown as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function baseTask(overrides: Record<string, unknown> = {}): unknown {
  return {
    id: "t1",
    workspace_id: "ws1",
    title: "Reset guest room",
    property_id: null,
    area_id: "Bedroom",
    priority: "high",
    state: "pending",
    scheduled_for_utc: "2026-04-28T09:30:00Z",
    duration_minutes: 45,
    photo_evidence: "required",
    linked_instruction_ids: [],
    inventory_consumption_json: {},
    is_personal: true,
    created_at: "2026-04-28T08:00:00Z",
    ...overrides,
  };
}

function emptyEvidence(): unknown {
  return { data: [], next_cursor: null, has_more: false };
}

function emptyComments(): unknown {
  return { data: [], next_cursor: null, has_more: false };
}

function Harness({ initial = "/task/t1" }: { initial?: string }): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route path="/task/:tid" element={<><TaskDetailPage /><LocationProbe /></>} />
          <Route path="/today" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function LocationProbe(): ReactElement {
  const loc = useLocation();
  return <span data-testid="location">{loc.pathname}</span>;
}

function fileInput(): HTMLInputElement {
  const label = screen.getByText("Take photo").closest("label");
  if (!label) throw new Error("evidence picker not found");
  const input = label.querySelector("input[type=file]") as HTMLInputElement | null;
  if (!input) throw new Error("file input not found");
  return input;
}

function selectFile(file: File): void {
  const input = fileInput();
  Object.defineProperty(input, "files", {
    value: [file],
    configurable: true,
  });
  fireEvent.change(input);
}

beforeEach(() => {
  __resetApiProvidersForTests();
  registerWorkspaceSlugGetter(() => "acme");
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:preview"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  vi.restoreAllMocks();
});

describe("TaskDetailPage", () => {
  it("renders the mock detail structure from the current task, property, evidence, and comments APIs", async () => {
    const env = installFetch({
      "/api/v1/tasks/t1": [
        {
          body: baseTask({
            property_id: "p1",
            inventory_consumption_json: { linen: 2 },
          }),
        },
      ],
      "/api/v1/properties": [
        {
          body: [
            {
              id: "p1",
              name: "Villa Sud",
              city: "Nice",
              timezone: "Europe/Paris",
              color: "moss",
              kind: "str",
              areas: ["Bedroom"],
              evidence_policy: "inherit",
              country: "FR",
              locale: "fr",
              settings_override: {},
              client_org_id: null,
              owner_user_id: null,
            },
          ],
        },
      ],
      "/api/v1/tasks/t1/evidence": [{ body: emptyEvidence() }],
      "/api/v1/tasks/t1/comments": [
        {
          body: {
            data: [
              {
                id: "c1",
                occurrence_id: "t1",
                kind: "user",
                author_user_id: "u1",
                body_md: "Fresh towels are low.",
                created_at: "2026-04-28T09:40:00Z",
                deleted_at: null,
              },
            ],
            next_cursor: null,
            has_more: false,
          },
        },
      ],
    });

    try {
      render(<Harness />);

      expect(await screen.findByText("Reset guest room")).toBeInTheDocument();
      expect(await screen.findByText("Villa Sud")).toBeInTheDocument();
      expect(screen.getByText("High")).toBeInTheDocument();
      expect(screen.getByText("linen")).toBeInTheDocument();
      expect(screen.getByText("Fresh towels are low.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /add photo to complete/i })).toBeDisabled();
      expect(env.calls.some((call) => call.url.endsWith("/w/acme/api/v1/tasks/t1"))).toBe(true);
    } finally {
      env.restore();
    }
  });

  it("uploads photo evidence with an optimistic preview and completes with the returned evidence id", async () => {
    const env = installFetch({
      "/api/v1/tasks/t1": [
        { body: baseTask() },
        { body: baseTask({ state: "done" }) },
      ],
      "/api/v1/tasks/t1/evidence": [
        { body: emptyEvidence() },
        {
          body: {
            id: "ev1",
            workspace_id: "ws1",
            occurrence_id: "t1",
            kind: "photo",
            blob_hash: "hash1",
            note_md: null,
            created_at: "2026-04-28T09:45:00Z",
            created_by_user_id: "u1",
          },
        },
        {
          body: {
            data: [
              {
                id: "ev1",
                kind: "photo",
                blob_hash: "hash1",
                note_md: null,
                created_at: "2026-04-28T09:45:00Z",
              },
            ],
            next_cursor: null,
            has_more: false,
          },
        },
      ],
      "/api/v1/tasks/t1/comments": [{ body: emptyComments() }],
      "/api/v1/tasks/t1/complete": [
        {
          body: {
            task_id: "t1",
            state: "done",
            completed_at: "2026-04-28T09:50:00Z",
            completed_by_user_id: "u1",
            reason: null,
          },
        },
      ],
    });

    try {
      render(<Harness />);
      await screen.findByText("Reset guest room");

      selectFile(new File(["photo-bytes"], "room.jpg", { type: "image/jpeg" }));
      expect(await screen.findByText("Photo ready")).toBeInTheDocument();

      const uploadCall = env.calls.find(
        (call) => call.url.endsWith("/api/v1/tasks/t1/evidence") && call.init.method === "POST",
      );
      expect(uploadCall?.init.body).toBeInstanceOf(FormData);
      const uploadForm = uploadCall!.init.body as FormData;
      expect(uploadForm.get("kind")).toBe("photo");
      expect((uploadForm.get("file") as File).name).toBe("room.jpg");

      const completeButton = screen.getByRole("button", { name: /complete with photo/i });
      expect(completeButton).not.toBeDisabled();
      fireEvent.click(completeButton);

      await waitFor(() => {
        const completeCall = env.calls.find((call) =>
          call.url.endsWith("/api/v1/tasks/t1/complete"),
        );
        expect(completeCall).toBeDefined();
        expect(completeCall!.init.body).toBe(JSON.stringify({ photo_evidence_ids: ["ev1"] }));
      });
    } finally {
      env.restore();
    }
  });

  it("does not treat a failed optimistic photo upload as completion evidence", async () => {
    const env = installFetch({
      "/api/v1/tasks/t1": [{ body: baseTask() }],
      "/api/v1/tasks/t1/evidence": [
        { body: emptyEvidence() },
        {
          status: 415,
          body: {
            type: "https://crewday.dev/errors/evidence_content_type_rejected",
            title: "Unsupported media",
            detail: "Only photos are accepted.",
          },
        },
      ],
      "/api/v1/tasks/t1/comments": [{ body: emptyComments() }],
    });

    try {
      render(<Harness />);
      await screen.findByText("Reset guest room");

      selectFile(new File(["not-a-photo"], "room.txt", { type: "text/plain" }));

      expect(await screen.findByText("Upload failed")).toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent("Only photos are accepted.");
      expect(screen.getByRole("button", { name: /add photo to complete/i })).toBeDisabled();
      expect(env.calls.some((call) => call.url.endsWith("/api/v1/tasks/t1/complete"))).toBe(false);
    } finally {
      env.restore();
    }
  });

  it("renders returned checklist rows read-only until the backend exposes a checklist mutation route", async () => {
    const env = installFetch({
      "/api/v1/tasks/t1": [
        {
          body: baseTask({
            photo_evidence: "disabled",
            checklist: [
              {
                id: "ci1",
                text: "Check under the bed",
                required: true,
                checked: false,
              },
            ],
          }),
        },
      ],
      "/api/v1/tasks/t1/evidence": [{ body: emptyEvidence() }],
      "/api/v1/tasks/t1/comments": [{ body: emptyComments() }],
    });

    try {
      render(<Harness />);

      const item = await screen.findByText("Check under the bed");
      fireEvent.click(item);

      expect(screen.getByRole("button", { name: /mark done/i })).not.toBeDisabled();
      expect(env.calls.some((call) => call.url.includes("/checklist/"))).toBe(false);
    } finally {
      env.restore();
    }
  });

  it("posts chat messages through the task comments endpoint", async () => {
    const env = installFetch({
      "/api/v1/tasks/t1": [{ body: baseTask({ photo_evidence: "disabled" }) }],
      "/api/v1/tasks/t1/evidence": [{ body: emptyEvidence() }],
      "/api/v1/tasks/t1/comments": [
        { body: emptyComments() },
        {
          body: {
            id: "c2",
            occurrence_id: "t1",
            kind: "user",
            author_user_id: "u1",
            body_md: "Window latch is loose.",
            created_at: "2026-04-28T10:00:00Z",
            deleted_at: null,
          },
        },
        {
          body: {
            data: [
              {
                id: "c2",
                occurrence_id: "t1",
                kind: "user",
                author_user_id: "u1",
                body_md: "Window latch is loose.",
                created_at: "2026-04-28T10:00:00Z",
                deleted_at: null,
              },
            ],
            next_cursor: null,
            has_more: false,
          },
        },
      ],
    });

    try {
      render(<Harness />);
      await screen.findByText("Reset guest room");

      fireEvent.change(screen.getByLabelText("Message the assistant about this task"), {
        target: { value: "Window latch is loose." },
      });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));

      await screen.findByText("Window latch is loose.");
      const postCall = env.calls.find(
        (call) => call.url.endsWith("/api/v1/tasks/t1/comments") && call.init.method === "POST",
      );
      expect(postCall?.init.body).toBe(
        JSON.stringify({ body_md: "Window latch is loose.", attachments: [] }),
      );
    } finally {
      env.restore();
    }
  });

  it("renders an inline error state without navigating during render", async () => {
    const env = installFetch({
      "/api/v1/tasks/t1": [
        {
          status: 404,
          body: {
            type: "https://crewday.dev/errors/task_not_found",
            title: "Not found",
            detail: "Task not found",
          },
        },
      ],
      "/api/v1/tasks/t1/evidence": [{ body: emptyEvidence() }],
      "/api/v1/tasks/t1/comments": [{ body: emptyComments() }],
    });

    try {
      render(<Harness />);

      expect(await screen.findByText("Task unavailable.")).toBeInTheDocument();
      expect(screen.getByTestId("location")).toHaveTextContent("/task/t1");
    } finally {
      env.restore();
    }
  });
});
