import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";

import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import {
  installFetchRouteHandlers,
  type FetchRouteRequest,
} from "@/test/helpers";
import { chooseSearchableOption } from "@/test/searchableSelect";
import type { Employee, Property, Schedule, TaskTemplate } from "@/types/api";

import SchedulesPage from "./SchedulesPage";

const TEMPLATE: TaskTemplate = {
  id: "tpl_1",
  workspace_id: "ws_1",
  name: "Turnover clean",
  description_md: "",
  role_id: null,
  duration_minutes: 45,
  property_scope: "any",
  listed_property_ids: [],
  area_scope: "any",
  listed_area_ids: [],
  checklist_template_json: [],
  photo_evidence: "disabled",
  linked_instruction_ids: [],
  priority: "normal",
  auto_shift_from_occurrence: false,
  inventory_consumption_json: {},
  inventory_effects: [],
  llm_hints_md: null,
  created_at: "2026-04-01T00:00:00Z",
  deleted_at: null,
};

const PROPERTY: Property = {
  id: "prop_1",
  name: "Casa Verde",
  city: "Lisbon",
  timezone: "Europe/Lisbon",
  color: "moss",
  kind: "vacation",
  areas: [],
  evidence_policy: "inherit",
  country: "PT",
  locale: "pt-PT",
  settings_override: {},
  client_org_id: null,
  owner_user_id: null,
};

const EMPLOYEE: Employee = {
  id: "emp_1",
  name: "Mina Silva",
  roles: [],
  properties: [],
  avatar_initials: "MS",
  avatar_file_id: null,
  avatar_url: null,
  phone: "",
  email: "mina@example.test",
  started_on: "2026-01-01",
  capabilities: {},
  workspaces: [],
  villas: [],
  language: "en",
  weekly_availability: {},
  evidence_policy: "inherit",
  preferred_locale: null,
  settings_override: {},
};

const SCHEDULE: Schedule = {
  id: "sch_1",
  workspace_id: "ws_1",
  name: "Existing turnover",
  template_id: TEMPLATE.id,
  property_id: PROPERTY.id,
  area_id: null,
  default_assignee_id: EMPLOYEE.id,
  backup_assignee_user_ids: [],
  rrule: "RRULE:FREQ=WEEKLY;BYDAY=MO",
  rrule_human: "Every Monday at 09:00",
  dtstart_local: "2026-04-20T09:00:00",
  duration_minutes: 45,
  rdate_local: "",
  exdate_local: "",
  active_from: "2026-04-20",
  active_until: null,
  paused_at: null,
  created_at: "2026-04-01T00:00:00Z",
  deleted_at: null,
  paused: false,
};

function makeClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function Harness({ client }: { client: QueryClient }): ReactElement {
  return (
    <QueryClientProvider client={client}>
      <WorkspaceProvider>
        <MemoryRouter initialEntries={["/schedules"]}>
          <SchedulesPage />
        </MemoryRouter>
      </WorkspaceProvider>
    </QueryClientProvider>
  );
}

function installSchedulesFetch(opts: {
  schedules?: Schedule[];
  templates?: TaskTemplate[];
  postRequests?: FetchRouteRequest[];
  patchRequests?: FetchRouteRequest[];
  pauseRequests?: FetchRouteRequest[];
  resumeRequests?: FetchRouteRequest[];
} = {}) {
  const schedules = opts.schedules ?? [SCHEDULE];
  const templates = opts.templates ?? [TEMPLATE];
  const postRequests = opts.postRequests ?? [];
  const patchRequests = opts.patchRequests ?? [];
  const pauseRequests = opts.pauseRequests ?? [];
  const resumeRequests = opts.resumeRequests ?? [];
  return installFetchRouteHandlers([
    {
      path: "/w/acme/api/v1/tasks/schedules",
      respond: {
        body: {
          data: schedules,
          next_cursor: null,
          has_more: false,
          templates_by_id: { [TEMPLATE.id]: TEMPLATE },
        },
      },
    },
    {
      path: "/w/acme/api/v1/tasks/task_templates",
      respond: { body: { data: templates, next_cursor: null, has_more: false } },
    },
    {
      path: "/w/acme/api/v1/properties",
      respond: { body: [PROPERTY] },
    },
    {
      path: "/w/acme/api/v1/employees",
      respond: { body: [EMPLOYEE] },
    },
    {
      path: "/w/acme/api/v1/tasks/schedules",
      method: "POST",
      respond: (request) => {
        postRequests.push(request);
        return {
          status: 201,
          body: {
            ...SCHEDULE,
            id: "sch_new",
            name: (request.body as { name?: string }).name ?? "New schedule",
          },
        };
      },
    },
    {
      path: "/w/acme/api/v1/tasks/schedules/sch_1",
      method: "PATCH",
      respond: (request) => {
        patchRequests.push(request);
        return {
          body: {
            ...SCHEDULE,
            ...(request.body as Record<string, unknown>),
          },
        };
      },
    },
    {
      path: "/w/acme/api/v1/tasks/schedules/sch_1/pause",
      method: "POST",
      respond: (request) => {
        pauseRequests.push(request);
        return { body: { ...SCHEDULE, paused: true, paused_at: "2026-05-08T10:30:00Z" } };
      },
    },
    {
      path: "/w/acme/api/v1/tasks/schedules/sch_1/resume",
      method: "POST",
      respond: (request) => {
        resumeRequests.push(request);
        return { body: { ...SCHEDULE, paused: false, paused_at: null } };
      },
    },
  ]);
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<SchedulesPage> inline schedules table", () => {
  it("renders a trailing inline create row without opening a modal", async () => {
    const harness = installSchedulesFetch();
    try {
      render(<Harness client={makeClient()} />);
      await waitFor(() => {
        expect(screen.getAllByText("Existing turnover").length).toBeGreaterThan(0);
      });

      fireEvent.click(screen.getByRole("button", { name: "+ New schedule" }));

      const createRow = screen.getByLabelText("New schedule");
      expect(within(createRow).getByLabelText("Name")).toBeInTheDocument();
      expect(within(createRow).getByRole("combobox", { name: /^Template\b/ })).toHaveValue("");
      expect(within(createRow).getByRole("combobox", { name: /^Property\b/ })).toHaveValue("Any property");
      expect(within(createRow).getByRole("combobox", { name: /^Default assignee\b/ })).toHaveValue("Unassigned");
      expect(screen.queryByRole("dialog", { name: "New schedule" })).not.toBeInTheDocument();
    } finally {
      harness.restore();
    }
  });

  it("shows an explicit prerequisite message when no task templates exist", async () => {
    const harness = installSchedulesFetch({ templates: [] });
    try {
      render(<Harness client={makeClient()} />);
      await waitFor(() => {
        expect(screen.getAllByText("Existing turnover").length).toBeGreaterThan(0);
      });

      const createRow = screen.getByLabelText("New schedule");
      expect(createRow).toHaveTextContent("Create a task template before adding schedules.");
      expect(within(createRow).getByRole("button", { name: "Save" })).toBeDisabled();
    } finally {
      harness.restore();
    }
  });

  it("renders schedules with nullable property, duration, and active_from fields", async () => {
    const harness = installSchedulesFetch({
      schedules: [
        {
          ...SCHEDULE,
          id: "sch_nullable",
          name: "Workspace-wide schedule",
          property_id: null,
          duration_minutes: null,
          active_from: null,
        },
      ],
    });
    try {
      render(<Harness client={makeClient()} />);

      await waitFor(() => {
        expect(screen.getAllByText("Workspace-wide schedule").length).toBeGreaterThan(0);
      });
      expect(screen.getByText("since unknown")).toBeInTheDocument();
      expect(screen.getAllByText("45 min").length).toBeGreaterThan(0);
      expect(screen.queryByText("Invalid Date")).not.toBeInTheDocument();
    } finally {
      harness.restore();
    }
  });

  it("submits a valid schedule through POST /schedules", async () => {
    const postRequests: FetchRouteRequest[] = [];
    const harness = installSchedulesFetch({ postRequests });
    try {
      render(<Harness client={makeClient()} />);
      await waitFor(() => {
        expect(screen.getAllByText("Existing turnover").length).toBeGreaterThan(0);
      });
      fireEvent.click(screen.getByRole("button", { name: "+ New schedule" }));

      const createRow = screen.getByLabelText("New schedule");
      fireEvent.change(within(createRow).getByLabelText(/^Name\b/), {
        target: { value: "Friday turnover" },
      });
      await chooseSearchableOption(createRow, /^Template\b/, /Turnover clean/i);
      await chooseSearchableOption(createRow, /^Property\b/, /Casa Verde/i);
      await chooseSearchableOption(createRow, /^Default assignee\b/, /Mina Silva/i);
      fireEvent.change(within(createRow).getByLabelText(/^Starts on\b/), {
        target: { value: "2026-05-08" },
      });
      fireEvent.change(within(createRow).getByLabelText(/^Start time\b/), {
        target: { value: "10:30" },
      });
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(postRequests).toHaveLength(1);
      });
      expect(postRequests[0]?.body).toEqual({
        name: "Friday turnover",
        template_id: TEMPLATE.id,
        property_id: PROPERTY.id,
        default_assignee: EMPLOYEE.id,
        backup_assignee_user_ids: [],
        rrule: "RRULE:FREQ=WEEKLY;BYDAY=FR",
        dtstart_local: "2026-05-08T10:30:00",
        duration_minutes: TEMPLATE.duration_minutes,
        rdate_local: "",
        exdate_local: "",
        active_from: "2026-05-08",
        active_until: null,
      });
      expect(screen.queryByRole("dialog", { name: "New schedule" })).not.toBeInTheDocument();
    } finally {
      harness.restore();
    }
  });

  it("submits optional sentinels as omitted schedule fields", async () => {
    const postRequests: FetchRouteRequest[] = [];
    const harness = installSchedulesFetch({ postRequests });
    try {
      render(<Harness client={makeClient()} />);
      await waitFor(() => {
        expect(screen.getAllByText("Existing turnover").length).toBeGreaterThan(0);
      });
      const createRow = screen.getByLabelText("New schedule");
      expect(within(createRow).getByRole("combobox", { name: /^Property\b/ })).toHaveValue("Any property");
      expect(within(createRow).getByRole("combobox", { name: /^Default assignee\b/ })).toHaveValue("Unassigned");
      fireEvent.change(within(createRow).getByLabelText(/^Name\b/), {
        target: { value: "Workspace-wide schedule" },
      });
      await chooseSearchableOption(createRow, /^Template\b/, /Turnover clean/i);
      fireEvent.change(within(createRow).getByLabelText(/^Starts on\b/), {
        target: { value: "2026-05-08" },
      });
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(postRequests).toHaveLength(1);
      });
      expect(postRequests[0]?.body).toEqual({
        name: "Workspace-wide schedule",
        template_id: TEMPLATE.id,
        backup_assignee_user_ids: [],
        rrule: "RRULE:FREQ=WEEKLY;BYDAY=FR",
        dtstart_local: "2026-05-08T09:00:00",
        duration_minutes: TEMPLATE.duration_minutes,
        rdate_local: "",
        exdate_local: "",
        active_from: "2026-05-08",
        active_until: null,
      });
    } finally {
      harness.restore();
    }
  });

  it("shows required validation in the inline create row", async () => {
    const postRequests: FetchRouteRequest[] = [];
    const harness = installSchedulesFetch({ postRequests });
    try {
      render(<Harness client={makeClient()} />);
      await waitFor(() => {
        expect(screen.getAllByText("Existing turnover").length).toBeGreaterThan(0);
      });
      fireEvent.click(screen.getByRole("button", { name: "+ New schedule" }));

      const createRow = screen.getByLabelText("New schedule");
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));
      expect(createRow).toHaveTextContent("Name is required.");

      fireEvent.change(within(createRow).getByLabelText(/^Name\b/), {
        target: { value: "Friday turnover" },
      });
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));
      expect(createRow).toHaveTextContent("Template is required.");

      await chooseSearchableOption(createRow, /^Template\b/, /Turnover clean/i);
      fireEvent.change(within(createRow).getByLabelText(/^Starts on\b/), {
        target: { value: "" },
      });
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));
      expect(createRow).toHaveTextContent("Start date is required.");

      fireEvent.change(within(createRow).getByLabelText(/^Starts on\b/), {
        target: { value: "2026-05-08" },
      });
      fireEvent.change(within(createRow).getByLabelText(/^Start time\b/), {
        target: { value: "" },
      });
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));
      expect(createRow).toHaveTextContent("Start time is required.");
      expect(postRequests).toHaveLength(0);
    } finally {
      harness.restore();
    }
  });

  it("preserves the existing schedule list and preview while editing inline", async () => {
    const harness = installSchedulesFetch();
    try {
      render(<Harness client={makeClient()} />);
      await waitFor(() => {
        expect(screen.getAllByText("Existing turnover").length).toBeGreaterThan(0);
      });

      fireEvent.click(screen.getByRole("button", { name: "+ New schedule" }));

      expect(screen.getByRole("table", { name: "Schedules" })).toBeInTheDocument();
      expect(screen.getByText("Preview — next 7 days")).toBeInTheDocument();
      expect(screen.getAllByText("Every Monday at 09:00").length).toBeGreaterThan(0);
      expect(screen.getAllByText("Casa Verde").length).toBeGreaterThan(0);
      expect(screen.getAllByText("45m").length).toBeGreaterThan(0);
    } finally {
      harness.restore();
    }
  });

  it("updates an existing schedule through the inline row", async () => {
    const patchRequests: FetchRouteRequest[] = [];
    const scheduleWithHiddenFields: Schedule = {
      ...SCHEDULE,
      area_id: "area_hidden",
      backup_assignee_user_ids: ["emp_backup"],
      rdate_local: "RDATE:20260509T081500",
      exdate_local: "EXDATE:20260513T081500",
      active_until: "2026-08-31",
    };
    const harness = installSchedulesFetch({ schedules: [scheduleWithHiddenFields], patchRequests });
    try {
      render(<Harness client={makeClient()} />);
      await waitFor(() => {
        expect(screen.getAllByText("Existing turnover").length).toBeGreaterThan(0);
      });

      const scheduleRow = screen.getByLabelText("Existing turnover");
      fireEvent.click(within(scheduleRow).getByRole("button", { name: "Edit" }));
      fireEvent.change(within(scheduleRow).getByLabelText(/^Name\b/), {
        target: { value: "Updated turnover" },
      });
      fireEvent.change(within(scheduleRow).getByLabelText(/^Starts on\b/), {
        target: { value: "2026-05-06" },
      });
      fireEvent.change(within(scheduleRow).getByLabelText(/^Start time\b/), {
        target: { value: "08:15" },
      });
      fireEvent.click(within(scheduleRow).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(patchRequests).toHaveLength(1);
      });
      expect(patchRequests[0]?.body).toEqual({
        name: "Updated turnover",
        template_id: TEMPLATE.id,
        property_id: PROPERTY.id,
        area_id: "area_hidden",
        default_assignee: EMPLOYEE.id,
        backup_assignee_user_ids: ["emp_backup"],
        rrule: "RRULE:FREQ=WEEKLY;BYDAY=WE",
        dtstart_local: "2026-05-06T08:15:00",
        duration_minutes: TEMPLATE.duration_minutes,
        rdate_local: "RDATE:20260509T081500",
        exdate_local: "EXDATE:20260513T081500",
        active_from: "2026-05-06",
        active_until: "2026-08-31",
      });
    } finally {
      harness.restore();
    }
  });

  it("preserves hidden recurrence and nullable duration when updating a visible field", async () => {
    const patchRequests: FetchRouteRequest[] = [];
    const scheduleWithAdvancedFields: Schedule = {
      ...SCHEDULE,
      area_id: "area_hidden",
      backup_assignee_user_ids: ["emp_backup"],
      rrule: "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE",
      duration_minutes: null,
      rdate_local: "RDATE:20260509T081500",
      exdate_local: "EXDATE:20260513T081500",
      active_until: "2026-08-31",
    };
    const harness = installSchedulesFetch({ schedules: [scheduleWithAdvancedFields], patchRequests });
    try {
      render(<Harness client={makeClient()} />);
      await waitFor(() => {
        expect(screen.getAllByText("Existing turnover").length).toBeGreaterThan(0);
      });

      const scheduleRow = screen.getByLabelText("Existing turnover");
      fireEvent.click(within(scheduleRow).getByRole("button", { name: "Edit" }));
      fireEvent.change(within(scheduleRow).getByLabelText(/^Name\b/), {
        target: { value: "Advanced turnover" },
      });
      fireEvent.click(within(scheduleRow).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(patchRequests).toHaveLength(1);
      });
      expect(patchRequests[0]?.body).toEqual({
        name: "Advanced turnover",
        template_id: TEMPLATE.id,
        property_id: PROPERTY.id,
        area_id: "area_hidden",
        default_assignee: EMPLOYEE.id,
        backup_assignee_user_ids: ["emp_backup"],
        rrule: "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE",
        dtstart_local: "2026-04-20T09:00:00",
        duration_minutes: null,
        rdate_local: "RDATE:20260509T081500",
        exdate_local: "EXDATE:20260513T081500",
        active_from: "2026-04-20",
        active_until: "2026-08-31",
      });
    } finally {
      harness.restore();
    }
  });

  it("keeps existing schedule editing usable when only sidecar templates are available", async () => {
    const patchRequests: FetchRouteRequest[] = [];
    const harness = installSchedulesFetch({ templates: [], patchRequests });
    try {
      render(<Harness client={makeClient()} />);
      await waitFor(() => {
        expect(screen.getAllByText("Existing turnover").length).toBeGreaterThan(0);
      });

      const createRow = screen.getByLabelText("New schedule");
      expect(createRow).toHaveTextContent("Create a task template before adding schedules.");
      expect(within(createRow).getByRole("button", { name: "Save" })).toBeDisabled();

      const scheduleRow = screen.getByLabelText("Existing turnover");
      fireEvent.click(within(scheduleRow).getByRole("button", { name: "Edit" }));
      expect(within(scheduleRow).getByRole("combobox", { name: /^Template\b/ })).toHaveValue(TEMPLATE.name);
      fireEvent.change(within(scheduleRow).getByLabelText(/^Name\b/), {
        target: { value: "Sidecar turnover" },
      });
      fireEvent.click(within(scheduleRow).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(patchRequests).toHaveLength(1);
      });
      expect(patchRequests[0]?.body).toMatchObject({
        name: "Sidecar turnover",
        template_id: TEMPLATE.id,
      });
    } finally {
      harness.restore();
    }
  });

  it("pauses and resumes existing schedules from the inline row", async () => {
    const pauseRequests: FetchRouteRequest[] = [];
    const resumeRequests: FetchRouteRequest[] = [];
    const harness = installSchedulesFetch({
      schedules: [{ ...SCHEDULE, paused: true, paused_at: "2026-05-08T10:30:00Z" }],
      pauseRequests,
      resumeRequests,
    });
    try {
      render(<Harness client={makeClient()} />);
      await waitFor(() => {
        expect(screen.getAllByText("Existing turnover").length).toBeGreaterThan(0);
      });

      const scheduleRow = screen.getByLabelText("Existing turnover");
      fireEvent.click(within(scheduleRow).getByRole("button", { name: "Resume" }));
      await waitFor(() => {
        expect(resumeRequests).toHaveLength(1);
      });
    } finally {
      harness.restore();
    }
    cleanup();

    const pauseHarness = installSchedulesFetch({ pauseRequests, resumeRequests });
    try {
      render(<Harness client={makeClient()} />);
      await waitFor(() => {
        expect(screen.getAllByText("Existing turnover").length).toBeGreaterThan(0);
      });

      const scheduleRow = screen.getByLabelText("Existing turnover");
      fireEvent.click(within(scheduleRow).getByRole("button", { name: "Pause" }));
      await waitFor(() => {
        expect(pauseRequests).toHaveLength(1);
      });
    } finally {
      pauseHarness.restore();
    }
  });
});
