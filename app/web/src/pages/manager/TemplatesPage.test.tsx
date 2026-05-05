import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import {
  installFetchRouteHandlers,
  type FakeResponse,
  type FetchCall,
} from "@/test/helpers";

import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests, qk } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import type { TaskTemplate } from "@/types/task";
import type { WorkRole } from "@/types/employee";

import TemplatesPage from "./TemplatesPage";

function makeTemplate(overrides: Partial<TaskTemplate> = {}): TaskTemplate {
  return {
    id: "tpl_1",
    workspace_id: "ws_1",
    name: "Daily clean",
    description_md: "",
    role_id: null,
    duration_minutes: 30,
    property_scope: "any",
    listed_property_ids: [],
    area_scope: "any",
    listed_area_ids: [],
    checklist_template_json: [
      { key: "first", text: "First step", required: false },
      { key: "second", text: "Second step", required: false },
      { key: "third", text: "Third step", required: false },
    ],
    photo_evidence: "disabled",
    linked_instruction_ids: [],
    priority: "normal",
    auto_shift_from_occurrence: false,
    inventory_consumption_json: {},
    inventory_effects: [],
    llm_hints_md: null,
    created_at: "2026-04-01T00:00:00Z",
    deleted_at: null,
    ...overrides,
  };
}

const ROLES: WorkRole[] = [];

interface FetchHarness {
  calls: FetchCall[];
  patchQueue: FakeResponse[];
  listQueue: FakeResponse[];
  restore: () => void;
}

function installFetch(opts: {
  initialTemplate?: TaskTemplate;
  patchResponses?: FakeResponse[];
} = {}): FetchHarness {
  // code-health: ignore[nloc] Route fixtures stay local; shared fetch mechanics live in test/helpers.
  const initial = opts.initialTemplate ?? makeTemplate();
  const listQueue: FakeResponse[] = [
    { body: { data: [initial], next_cursor: null, has_more: false } },
  ];
  const patchQueue: FakeResponse[] = [...(opts.patchResponses ?? [])];
  const env = installFetchRouteHandlers([
    {
      path: "/w/acme/api/v1/task_templates",
      respond: () => {
      const next = listQueue.shift();
        return next ?? { body: { data: [initial], next_cursor: null, has_more: false } };
      },
    },
    {
      path: "/w/acme/api/v1/work_roles",
      respond: { body: { data: ROLES, next_cursor: null, has_more: false } },
    },
    {
      path: `/w/acme/api/v1/task_templates/${initial.id}`,
      method: "PATCH",
      respond: (request) => {
        const next = patchQueue.shift();
        return next ?? { body: { ...initial, ...(request.body as Record<string, unknown>) } };
      },
    },
  ]);
  return {
    calls: env.calls,
    patchQueue,
    listQueue,
    restore: env.restore,
  };
}

function makeClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function Harness({ client }: { client: QueryClient }): ReactElement {
  return (
    <QueryClientProvider client={client}>
      <WorkspaceProvider>
        <MemoryRouter initialEntries={["/templates"]}>
          <TemplatesPage />
        </MemoryRouter>
      </WorkspaceProvider>
    </QueryClientProvider>
  );
}

function patchCalls(calls: FetchCall[]): FetchCall[] {
  return calls.filter((c) => c.init.method === "PATCH");
}

function patchedChecklistKeys(call: FetchCall): string[] {
  const body = JSON.parse(String(call.init.body)) as {
    checklist_template_json: { key: string }[];
  };
  return body.checklist_template_json.map((c) => c.key);
}

async function fireDrop(from: HTMLElement, to: HTMLElement): Promise<void> {
  const dataTransfer = {
    data: {} as Record<string, string>,
    effectAllowed: "move",
    dropEffect: "move",
    setData(format: string, value: string) {
      this.data[format] = value;
    },
    getData(format: string) {
      return this.data[format] ?? "";
    },
  };
  await act(async () => {
    fireEvent.dragStart(from, { dataTransfer });
  });
  await act(async () => {
    fireEvent.dragOver(to, { dataTransfer });
  });
  await act(async () => {
    fireEvent.drop(to, { dataTransfer });
  });
  await act(async () => {
    fireEvent.dragEnd(from, { dataTransfer });
  });
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<TemplatesPage> checklist reorder", () => {
  it("reorders the React Query cache optimistically on drop", async () => {
    const harness = installFetch();
    const client = makeClient();
    try {
      render(<Harness client={client} />);
      await screen.findByText("First step");

      const items = screen.getAllByRole("listitem");
      await fireDrop(items[0]!, items[2]!);

      const cached = client.getQueryData<{ data: TaskTemplate[] }>(
        qk.taskTemplates(),
      );
      expect(cached?.data[0]?.checklist_template_json.map((c) => c.key)).toEqual([
        "second",
        "third",
        "first",
      ]);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
    } finally {
      harness.restore();
    }
  });

  it("debounces PATCH to a single request across a multi-drop burst", async () => {
    const harness = installFetch();
    const client = makeClient();
    try {
      render(<Harness client={client} />);
      await screen.findByText("First step");

      let items = screen.getAllByRole("listitem");
      await fireDrop(items[0]!, items[2]!);
      items = screen.getAllByRole("listitem");
      await fireDrop(items[0]!, items[2]!);

      expect(patchCalls(harness.calls)).toHaveLength(0);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
      await waitFor(() => {
        expect(patchCalls(harness.calls)).toHaveLength(1);
      });

      const sent = patchedChecklistKeys(patchCalls(harness.calls)[0]!);
      expect(sent).toEqual(["third", "first", "second"]);
    } finally {
      harness.restore();
    }
  });

  it("rolls the order back on a 4xx response", async () => {
    const harness = installFetch({
      patchResponses: [{ status: 422, body: { detail: "nope" } }],
    });
    const client = makeClient();
    try {
      render(<Harness client={client} />);
      await screen.findByText("First step");

      const items = screen.getAllByRole("listitem");
      await fireDrop(items[0]!, items[2]!);

      expect(
        client
          .getQueryData<{ data: TaskTemplate[] }>(qk.taskTemplates())
          ?.data[0]?.checklist_template_json.map((c) => c.key),
      ).toEqual(["second", "third", "first"]);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });

      await waitFor(() => {
        const cached = client.getQueryData<{ data: TaskTemplate[] }>(
          qk.taskTemplates(),
        );
        expect(cached?.data[0]?.checklist_template_json.map((c) => c.key)).toEqual([
          "first",
          "second",
          "third",
        ]);
      });
    } finally {
      harness.restore();
    }
  });

  it("supports keyboard reorder via the move-up/down buttons", async () => {
    const harness = installFetch();
    const client = makeClient();
    try {
      render(<Harness client={client} />);
      await screen.findByText("First step");

      const moveDown = screen.getByRole("button", {
        name: 'Move "First step" down',
      });
      fireEvent.click(moveDown);

      expect(
        client
          .getQueryData<{ data: TaskTemplate[] }>(qk.taskTemplates())
          ?.data[0]?.checklist_template_json.map((c) => c.key),
      ).toEqual(["second", "first", "third"]);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
      await waitFor(() => {
        expect(patchCalls(harness.calls)).toHaveLength(1);
      });
      expect(patchedChecklistKeys(patchCalls(harness.calls)[0]!)).toEqual([
        "second",
        "first",
        "third",
      ]);
    } finally {
      harness.restore();
    }
  });
});
