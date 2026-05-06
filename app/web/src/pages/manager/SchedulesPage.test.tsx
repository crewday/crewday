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
  name: "Existing turnover",
  template_id: TEMPLATE.id,
  property_id: PROPERTY.id,
  rrule_human: "Every Monday at 09:00",
  default_assignee_id: EMPLOYEE.id,
  backup_assignee_user_ids: [],
  duration_minutes: 45,
  active_from: "2026-04-20",
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
} = {}) {
  const schedules = opts.schedules ?? [SCHEDULE];
  const templates = opts.templates ?? [TEMPLATE];
  const postRequests = opts.postRequests ?? [];
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

describe("<SchedulesPage> New schedule action", () => {
  it("opens the schedule creation workflow", async () => {
    const harness = installSchedulesFetch();
    try {
      render(<Harness client={makeClient()} />);
      await waitFor(() => {
        expect(screen.getAllByText("Existing turnover").length).toBeGreaterThan(0);
      });

      fireEvent.click(screen.getByRole("button", { name: "+ New schedule" }));

      const dialog = screen.getByRole("dialog", { name: "New schedule" });
      expect(within(dialog).getByLabelText("Template")).toHaveValue(TEMPLATE.id);
      expect(within(dialog).getByLabelText("Property")).toHaveValue("");
      expect(within(dialog).getByRole("button", { name: "Create schedule" })).toBeEnabled();
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

      fireEvent.click(screen.getByRole("button", { name: "+ New schedule" }));

      const dialog = screen.getByRole("dialog", { name: "New schedule" });
      expect(within(dialog).getByRole("status")).toHaveTextContent(
        "Create a task template before adding schedules.",
      );
      expect(within(dialog).getByRole("button", { name: "Create schedule" })).toBeDisabled();
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

      const dialog = screen.getByRole("dialog", { name: "New schedule" });
      fireEvent.change(within(dialog).getByLabelText("Name"), {
        target: { value: "Friday turnover" },
      });
      fireEvent.change(within(dialog).getByLabelText("Property"), {
        target: { value: PROPERTY.id },
      });
      fireEvent.change(within(dialog).getByLabelText("Default assignee"), {
        target: { value: EMPLOYEE.id },
      });
      fireEvent.change(within(dialog).getByLabelText("Starts on"), {
        target: { value: "2026-05-08" },
      });
      fireEvent.change(within(dialog).getByLabelText("Start time"), {
        target: { value: "10:30" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Create schedule" }));

      await waitFor(() => {
        expect(postRequests).toHaveLength(1);
      });
      expect(postRequests[0]?.body).toEqual({
        name: "Friday turnover",
        template_id: TEMPLATE.id,
        property_id: PROPERTY.id,
        default_assignee: EMPLOYEE.id,
        rrule: "RRULE:FREQ=WEEKLY;BYDAY=FR",
        dtstart_local: "2026-05-08T10:30:00",
        active_from: "2026-05-08",
      });
      await waitFor(() => {
        expect(screen.queryByRole("dialog", { name: "New schedule" })).not.toBeInTheDocument();
      });
    } finally {
      harness.restore();
    }
  });

  it("shows a validation message instead of silently returning on invalid submit state", async () => {
    const postRequests: FetchRouteRequest[] = [];
    const harness = installSchedulesFetch({ postRequests });
    try {
      render(<Harness client={makeClient()} />);
      await waitFor(() => {
        expect(screen.getAllByText("Existing turnover").length).toBeGreaterThan(0);
      });
      fireEvent.click(screen.getByRole("button", { name: "+ New schedule" }));

      const dialog = screen.getByRole("dialog", { name: "New schedule" });
      const startsOn = within(dialog).getByLabelText("Starts on");
      const form = startsOn.closest("form");
      if (!form) throw new Error("Schedule form was not rendered");
      fireEvent.change(startsOn, { target: { value: "" } });
      fireEvent.submit(form);

      expect(within(dialog).getByRole("alert")).toHaveTextContent(
        "Name is required.",
      );
      expect(postRequests).toHaveLength(0);
    } finally {
      harness.restore();
    }
  });
});
