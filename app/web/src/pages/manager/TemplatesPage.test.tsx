import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import {
  installFetchRouteHandlers,
  type FakeResponse,
  type FetchCall,
} from "@/test/helpers";
import { chooseSearchableOption } from "@/test/searchableSelect";

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

const ROLES: WorkRole[] = [
  {
    id: "role_1",
    workspace_id: "ws_1",
    key: "housekeeping",
    name: "Housekeeping",
    description_md: "",
    default_settings_json: {},
    icon_name: "Sparkles",
    created_at: "2026-04-01T00:00:00Z",
    deleted_at: null,
  },
];
const TASK_TEMPLATES_API_PATH = "/w/acme/api/v1/tasks/task_templates";

interface FetchHarness {
  calls: FetchCall[];
  patchQueue: FakeResponse[];
  createQueue: FakeResponse[];
  listQueue: FakeResponse[];
  restore: () => void;
}

function installFetch(opts: {
  initialTemplate?: TaskTemplate;
  patchResponses?: FakeResponse[];
  createResponses?: FakeResponse[];
} = {}): FetchHarness {
  // code-health: ignore[nloc] Route fixtures stay local; shared fetch mechanics live in test/helpers.
  const initial = opts.initialTemplate ?? makeTemplate();
  let current = initial;
  const listQueue: FakeResponse[] = [
    { body: { data: [initial], next_cursor: null, has_more: false } },
  ];
  const patchQueue: FakeResponse[] = [...(opts.patchResponses ?? [])];
  const createQueue: FakeResponse[] = [...(opts.createResponses ?? [])];
  const env = installFetchRouteHandlers([
    {
      path: TASK_TEMPLATES_API_PATH,
      respond: () => {
        const next = listQueue.shift();
        return next ?? { body: { data: [current], next_cursor: null, has_more: false } };
      },
    },
    {
      path: TASK_TEMPLATES_API_PATH,
      method: "POST",
      respond: (request) => {
        const next = createQueue.shift();
        return next ?? { status: 201, body: makeTemplate({ id: "tpl_created", ...(request.body as Partial<TaskTemplate>) }) };
      },
    },
    {
      path: "/w/acme/api/v1/work_roles",
      respond: { body: { data: ROLES, next_cursor: null, has_more: false } },
    },
    {
      path: `${TASK_TEMPLATES_API_PATH}/${initial.id}`,
      method: "PATCH",
      respond: (request) => {
        const next = patchQueue.shift();
        if (next) return next;
        current = { ...current, ...(request.body as Partial<TaskTemplate>) };
        return { body: current };
      },
    },
  ]);
  return {
    calls: env.calls,
    patchQueue,
    createQueue,
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

function postCalls(calls: FetchCall[]): FetchCall[] {
  return calls.filter((c) => c.init.method === "POST");
}

function patchedChecklistKeys(call: FetchCall): string[] {
  const body = JSON.parse(String(call.init.body)) as {
    checklist_template_json: { key: string }[];
  };
  return body.checklist_template_json.map((c) => c.key);
}

function patchedChecklist(call: FetchCall): TaskTemplate["checklist_template_json"] {
  const body = JSON.parse(String(call.init.body)) as {
    checklist_template_json: TaskTemplate["checklist_template_json"];
  };
  return body.checklist_template_json;
}

function callPath(call: FetchCall): string {
  return new URL(call.url, "http://crewday.test").pathname;
}

function checklistRow(label: string): HTMLElement {
  const row = screen.getByText(label).closest("[data-inline-table-row-group]");
  if (!(row instanceof HTMLElement)) {
    throw new Error(`Checklist row not found for ${label}`);
  }
  return row;
}

function createChecklistRow(): HTMLElement {
  const row = document.querySelector("[data-inline-table-row-group^='inline-create']");
  if (!(row instanceof HTMLElement)) {
    throw new Error("Checklist create row not found");
  }
  return row;
}

function dataTransfer(): DataTransfer {
  const data: Record<string, string> = {};
  return {
    effectAllowed: "move",
    dropEffect: "move",
    files: [] as unknown as FileList,
    items: [] as unknown as DataTransferItemList,
    types: [],
    clearData(format?: string) {
      if (format) {
        delete data[format];
        return;
      }
      for (const key of Object.keys(data)) delete data[key];
    },
    setData(format: string, value: string) {
      data[format] = value;
    },
    getData(format: string) {
      return data[format] ?? "";
    },
    setDragImage: vi.fn(),
  } as unknown as DataTransfer;
}

async function fireDrop(from: HTMLElement, to: HTMLElement): Promise<void> {
  const transfer = dataTransfer();
  await act(async () => {
    fireEvent.dragStart(from, { dataTransfer: transfer });
  });
  await act(async () => {
    fireEvent.dragOver(to, { dataTransfer: transfer });
  });
  await act(async () => {
    fireEvent.drop(to, { dataTransfer: transfer });
  });
  await act(async () => {
    fireEvent.dragEnd(from, { dataTransfer: transfer });
  });
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close() {
    this.open = false;
  };
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<TemplatesPage> checklist reorder", () => {
  it("loads templates from the tasks-mounted API route", async () => {
    const harness = installFetch();
    const client = makeClient();
    try {
      render(<Harness client={client} />);
      await screen.findByText("First step");

      expect(screen.getByRole("table", { name: "Daily clean checklist" })).toBeInTheDocument();
      expect(harness.calls.some((c) => callPath(c) === TASK_TEMPLATES_API_PATH)).toBe(
        true,
      );
    } finally {
      harness.restore();
    }
  });

  it("reorders the React Query cache optimistically on drop", async () => {
    const harness = installFetch();
    const client = makeClient();
    try {
      render(<Harness client={client} />);
      await screen.findByText("First step");

      await fireDrop(checklistRow("First step"), checklistRow("Third step"));

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

  it("shows and clears the checklist drop indicator", async () => {
    const harness = installFetch();
    const client = makeClient();
    try {
      render(<Harness client={client} />);
      await screen.findByText("First step");

      const transfer = dataTransfer();
      const first = checklistRow("First step");
      const second = checklistRow("Second step");
      vi.spyOn(second, "getBoundingClientRect").mockReturnValue({
        x: 0,
        y: 0,
        width: 240,
        height: 40,
        top: 100,
        right: 240,
        bottom: 140,
        left: 0,
        toJSON: () => ({}),
      });

      fireEvent.dragStart(first, { dataTransfer: transfer });
      fireEvent.dragOver(second, { dataTransfer: transfer, clientY: 130 });
      expect(second).toHaveClass("inline-table-form__group--drop-after");

      fireEvent.drop(second, { dataTransfer: transfer, clientY: 130 });
      expect(second).not.toHaveClass("inline-table-form__group--drop-after");

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

      await fireDrop(checklistRow("First step"), checklistRow("Third step"));
      await fireDrop(checklistRow("Second step"), checklistRow("First step"));

      expect(patchCalls(harness.calls)).toHaveLength(0);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
      await waitFor(() => {
        expect(patchCalls(harness.calls)).toHaveLength(1);
      });

      const patch = patchCalls(harness.calls)[0]!;
      expect(callPath(patch)).toBe(`${TASK_TEMPLATES_API_PATH}/tpl_1`);
      const sent = patchedChecklistKeys(patch);
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

      await fireDrop(checklistRow("First step"), checklistRow("Third step"));

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
        name: "Move First step down",
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
      expect(screen.getByRole("status")).toHaveTextContent(
        'Moved "First step" to position 2 of 3.',
      );
    } finally {
      harness.restore();
    }
  });

  it("edits checklist item text, flags, and recurrence inline", async () => {
    const harness = installFetch();
    const client = makeClient();
    try {
      render(<Harness client={client} />);
      await screen.findByText("First step");

      const row = checklistRow("First step");
      fireEvent.click(within(row).getByText("First step"));
      const text = await within(row).findByLabelText("Checklist item text");
      fireEvent.change(text, { target: { value: "Inspect kitchen" } });
      fireEvent.click(within(row).getByRole("checkbox", { name: "Required" }));
      fireEvent.click(within(row).getByRole("checkbox", { name: "Guest-visible" }));
      fireEvent.change(within(row).getByLabelText("Checklist item recurrence"), {
        target: { value: "FREQ=WEEKLY" },
      });
      fireEvent.keyDown(text, { key: "Enter" });

      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
      await waitFor(() => {
        expect(patchCalls(harness.calls)).toHaveLength(1);
      });
      const first = patchedChecklist(patchCalls(harness.calls)[0]!)[0]!;
      expect(first).toMatchObject({
        key: "first",
        text: "Inspect kitchen",
        required: true,
        guest_visible: true,
        rrule: "FREQ=WEEKLY",
      });
    } finally {
      harness.restore();
    }
  });

  it("creates and deletes checklist items through the replacement payload", async () => {
    const harness = installFetch();
    const client = makeClient();
    try {
      render(<Harness client={client} />);
      await screen.findByText("First step");

      const createRow = createChecklistRow();
      const text = within(createRow).getByLabelText("Checklist item text");
      fireEvent.change(text, { target: { value: "Restock coffee" } });
      fireEvent.keyDown(text, { key: "Enter" });

      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
      await waitFor(() => {
        expect(patchCalls(harness.calls)).toHaveLength(1);
      });
      expect(patchedChecklist(patchCalls(harness.calls)[0]!).at(-1)).toMatchObject({
        key: "restock_coffee",
        text: "Restock coffee",
        required: false,
        guest_visible: false,
      });

      const createdRow = checklistRow("Restock coffee");
      fireEvent.click(within(createdRow).getByRole("button", { name: "Delete" }));
      const dialog = await screen.findByRole("alertdialog", { name: "Delete this row?" });
      fireEvent.click(within(dialog).getByRole("button", { name: "Delete row" }));

      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
      await waitFor(() => {
        expect(patchCalls(harness.calls)).toHaveLength(2);
      });
      expect(patchedChecklistKeys(patchCalls(harness.calls)[1]!)).toEqual([
        "first",
        "second",
        "third",
      ]);
    } finally {
      harness.restore();
    }
  });

  it("does not persist another row's unsaved draft in replacement payloads", async () => {
    const harness = installFetch();
    const client = makeClient();
    try {
      render(<Harness client={client} />);
      await screen.findByText("First step");

      const firstRow = checklistRow("First step");
      fireEvent.click(within(firstRow).getByText("First step"));
      const firstText = await within(firstRow).findByLabelText("Checklist item text");
      fireEvent.change(firstText, { target: { value: "Draft first step" } });

      const createRow = createChecklistRow();
      const createText = within(createRow).getByLabelText("Checklist item text");
      fireEvent.change(createText, { target: { value: "Restock coffee" } });
      fireEvent.keyDown(createText, { key: "Enter" });

      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
      await waitFor(() => {
        expect(patchCalls(harness.calls)).toHaveLength(1);
      });
      const sent = patchedChecklist(patchCalls(harness.calls)[0]!);
      expect(sent[0]).toMatchObject({ key: "first", text: "First step" });
      expect(sent.at(-1)).toMatchObject({
        key: "restock_coffee",
        text: "Restock coffee",
      });
      expect(firstText).toHaveValue("Draft first step");
    } finally {
      harness.restore();
    }
  });
});

describe("<TemplatesPage> create flow", () => {
  it("opens the new-template dialog from the header action", async () => {
    const harness = installFetch();
    const client = makeClient();
    try {
      render(<Harness client={client} />);
      await screen.findByText("First step");

      fireEvent.click(screen.getByRole("button", { name: "+ New template" }));

      const dialog = await screen.findByRole("dialog", { name: "New template" });
      expect(within(dialog).getByLabelText(/^Name\b/)).toBeInTheDocument();
      expect(within(dialog).getByLabelText(/^Duration\b/)).toHaveValue(30);
      const role = within(dialog).getByRole("combobox", { name: /^Role\b/ });
      expect(role).toHaveValue("Any role");
      fireEvent.focus(role);
      expect(await within(dialog).findByRole("option", { name: /Housekeeping/i })).toBeInTheDocument();
    } finally {
      harness.restore();
    }
  });

  it("cancels without creating a template", async () => {
    const harness = installFetch();
    const client = makeClient();
    try {
      render(<Harness client={client} />);
      await screen.findByText("First step");

      fireEvent.click(screen.getByRole("button", { name: "+ New template" }));
      const dialog = await screen.findByRole("dialog", { name: "New template" });
      fireEvent.change(within(dialog).getByLabelText(/^Name\b/), {
        target: { value: "Evening reset" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

      await waitFor(() => {
        expect(screen.queryByRole("dialog", { name: "New template" })).not.toBeInTheDocument();
      });
      expect(postCalls(harness.calls)).toHaveLength(0);
    } finally {
      harness.restore();
    }
  });

  it("posts a valid template and invalidates task templates on success", async () => {
    const harness = installFetch();
    const client = makeClient();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    try {
      render(<Harness client={client} />);
      await screen.findByText("First step");

      fireEvent.click(screen.getByRole("button", { name: "+ New template" }));
      const dialog = await screen.findByRole("dialog", { name: "New template" });
      fireEvent.change(within(dialog).getByLabelText(/^Name\b/), {
        target: { value: "Evening reset" },
      });
      fireEvent.change(within(dialog).getByLabelText(/^Description\b/), {
        target: { value: "Close down common areas." },
      });
      await chooseSearchableOption(dialog, /^Role\b/, /Housekeeping/i);
      fireEvent.change(within(dialog).getByLabelText(/^Duration\b/), {
        target: { value: "45" },
      });
      fireEvent.change(within(dialog).getByLabelText(/^Priority\b/), {
        target: { value: "high" },
      });
      fireEvent.change(within(dialog).getByLabelText(/^Photo evidence\b/), {
        target: { value: "optional" },
      });
      const guidance = within(dialog).getByLabelText(/^Agent guidance\b/);
      expect(
        within(dialog).getByText(
          "Agents use this when drafting or discussing tasks from this template.",
        ),
      ).toBeInTheDocument();
      expect(guidance).toHaveAccessibleDescription(
        "Agents use this when drafting or discussing tasks from this template.",
      );
      fireEvent.change(guidance, {
        target: { value: "Nightly closing checklist." },
      });
      fireEvent.click(
        within(dialog).getByRole("checkbox", {
          name: "Start shift automatically from generated tasks",
        }),
      );
      fireEvent.click(within(dialog).getByRole("button", { name: "Create template" }));

      await waitFor(() => {
        expect(postCalls(harness.calls)).toHaveLength(1);
      });
      const post = postCalls(harness.calls)[0]!;
      expect(callPath(post)).toBe(TASK_TEMPLATES_API_PATH);
      expect(JSON.parse(String(post.init.body))).toEqual({
        name: "Evening reset",
        description_md: "Close down common areas.",
        role_id: "role_1",
        duration_minutes: 45,
        property_scope: "any",
        listed_property_ids: [],
        area_scope: "any",
        listed_area_ids: [],
        checklist_template_json: [],
        photo_evidence: "optional",
        linked_instruction_ids: [],
        priority: "high",
        auto_shift_from_occurrence: true,
        inventory_consumption_json: {},
        llm_hints_md: "Nightly closing checklist.",
      });
      await waitFor(() => {
        expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: qk.taskTemplates() });
      });
      await waitFor(() => {
        expect(screen.queryByRole("dialog", { name: "New template" })).not.toBeInTheDocument();
      });
    } finally {
      harness.restore();
    }
  });

  it("posts the any-role sentinel as a null role while native enums keep defaults", async () => {
    const harness = installFetch();
    const client = makeClient();
    try {
      render(<Harness client={client} />);
      await screen.findByText("First step");

      fireEvent.click(screen.getByRole("button", { name: "+ New template" }));
      const dialog = await screen.findByRole("dialog", { name: "New template" });
      expect(within(dialog).getByRole("combobox", { name: /^Role\b/ })).toHaveValue("Any role");
      fireEvent.change(within(dialog).getByLabelText(/^Name\b/), {
        target: { value: "Common area reset" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Create template" }));

      await waitFor(() => {
        expect(postCalls(harness.calls)).toHaveLength(1);
      });
      expect(JSON.parse(String(postCalls(harness.calls)[0]!.init.body))).toMatchObject({
        name: "Common area reset",
        role_id: null,
        priority: "normal",
        photo_evidence: "disabled",
      });
    } finally {
      harness.restore();
    }
  });
});
