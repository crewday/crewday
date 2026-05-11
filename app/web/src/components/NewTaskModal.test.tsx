import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import {
  installFetchRouteHandlers,
  type FetchRouteRequest,
} from "@/test/helpers";
import { chooseSearchableOption } from "@/test/searchableSelect";
import type { Me, Property } from "@/types/api";
import NewTaskButton from "./NewTaskModal";

const PROPERTY_A: Property = {
  id: "prop_a",
  name: "Villa Sud",
  city: "Nice",
  timezone: "Europe/Paris",
  color: "moss",
  kind: "vacation",
  areas: [],
  evidence_policy: "inherit",
  country: "FR",
  locale: "fr-FR",
  settings_override: {},
  client_org_id: null,
  owner_user_id: null,
};

const PROPERTY_B: Property = {
  ...PROPERTY_A,
  id: "prop_b",
  name: "Casa Verde",
  city: "Lisbon",
  timezone: "Europe/Lisbon",
  country: "PT",
  locale: "pt-PT",
};

const ME: Me = {
  role: "employee",
  theme: "system",
  agent_sidebar_collapsed: false,
  employee: {
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
  },
  manager_name: "Mina Silva",
  today: "2026-05-11",
  now: "2026-05-11T09:00:00Z",
  user_id: "user_1",
  agent_approval_mode: "strict",
  current_workspace_id: "ws_1",
  available_workspaces: [],
  client_binding_org_ids: [],
  is_deployment_admin: false,
  is_deployment_owner: false,
};

function makeClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function Harness({ client }: { client: QueryClient }): ReactElement {
  return (
    <QueryClientProvider client={client}>
      <WorkspaceProvider>
        <NewTaskButton />
      </WorkspaceProvider>
    </QueryClientProvider>
  );
}

function installNewTaskFetch(postRequests: FetchRouteRequest[]) {
  return installFetchRouteHandlers([
    {
      path: "/w/acme/api/v1/properties",
      respond: { body: [PROPERTY_A, PROPERTY_B] },
    },
    {
      path: "/w/acme/api/v1/me",
      respond: { body: ME },
    },
    {
      path: "/w/acme/api/v1/properties/prop_a/areas",
      respond: { body: { data: [{ id: "area_kitchen", name: "Kitchen" }] } },
    },
    {
      path: "/w/acme/api/v1/properties/prop_b/areas",
      respond: { body: { data: [{ id: "area_terrace", name: "Terrace" }] } },
    },
    {
      path: "/w/acme/api/v1/tasks",
      method: "POST",
      respond: (request) => {
        postRequests.push(request);
        return {
          status: 201,
          body: {
            id: "task_1",
            title: request.body && typeof request.body === "object"
              ? (request.body as { title?: string }).title
              : "Task",
            property_id: request.body && typeof request.body === "object"
              ? (request.body as { property_id?: string }).property_id
              : null,
            area_id: request.body && typeof request.body === "object"
              ? (request.body as { area_id?: string }).area_id
              : null,
            is_personal: true,
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
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close() {
    this.open = false;
  };
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<NewTaskButton>", () => {
  it("uses searchable property and area controls while resetting area on property change", async () => {
    const postRequests: FetchRouteRequest[] = [];
    const harness = installNewTaskFetch(postRequests);

    try {
      render(<Harness client={makeClient()} />);
      fireEvent.click(await screen.findByRole("button", { name: "+ New task" }));

      const dialog = screen.getByRole("dialog", { name: "New task" });
      expect(within(dialog).getByRole("combobox", { name: /^Property\b/ })).toHaveValue(
        "No property",
      );

      fireEvent.change(within(dialog).getByLabelText(/^Title\b/), {
        target: { value: "Check welcome basket" },
      });
      await chooseSearchableOption(dialog, /^Property\b/, /Villa Sud/i);
      await chooseSearchableOption(dialog, /^Area\b/, /Kitchen/i);
      await chooseSearchableOption(dialog, /^Property\b/, /Casa Verde/i);

      expect(await within(dialog).findByRole("combobox", { name: /^Area\b/ })).toHaveValue(
        "No area",
      );

      fireEvent.click(within(dialog).getByRole("button", { name: "Add task" }));

      await waitFor(() => {
        expect(postRequests).toHaveLength(1);
      });
      expect(postRequests[0]?.body).toMatchObject({
        title: "Check welcome basket",
        property_id: PROPERTY_B.id,
        assigned_user_id: ME.user_id,
        is_personal: true,
      });
      expect(postRequests[0]?.body).not.toHaveProperty("area_id");
    } finally {
      harness.restore();
    }
  });

  it("submits the no-property sentinel without optional location fields", async () => {
    const postRequests: FetchRouteRequest[] = [];
    const harness = installNewTaskFetch(postRequests);

    try {
      render(<Harness client={makeClient()} />);
      fireEvent.click(await screen.findByRole("button", { name: "+ New task" }));

      const dialog = screen.getByRole("dialog", { name: "New task" });
      expect(within(dialog).getByRole("combobox", { name: /^Property\b/ })).toHaveValue(
        "No property",
      );
      fireEvent.change(within(dialog).getByLabelText(/^Title\b/), {
        target: { value: "Workspace note" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Add task" }));

      await waitFor(() => {
        expect(postRequests).toHaveLength(1);
      });
      expect(postRequests[0]?.body).toMatchObject({
        title: "Workspace note",
        assigned_user_id: ME.user_id,
        is_personal: true,
      });
      expect(postRequests[0]?.body).not.toHaveProperty("property_id");
      expect(postRequests[0]?.body).not.toHaveProperty("area_id");
    } finally {
      harness.restore();
    }
  });
});
