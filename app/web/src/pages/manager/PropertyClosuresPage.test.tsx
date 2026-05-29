import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import PropertyClosuresPage from "./PropertyClosuresPage";
import { installFetchRouteHandlers } from "@/test/helpers";

function installFetch({
  emptyClosures = false,
  failCreate = false,
  failClosures = false,
  failUpdate = false,
  manualReason = "Renovation",
  missingProperty = false,
}: {
  emptyClosures?: boolean;
  failCreate?: boolean;
  failClosures?: boolean;
  failUpdate?: boolean;
  manualReason?: string;
  missingProperty?: boolean;
} = {}) {
  // code-health: ignore[nloc] Route fixtures stay local; shared fetch mechanics live in test/helpers.
  const env = installFetchRouteHandlers([
    {
      path: "/w/acme/api/v1/property_closures",
      method: "POST",
      respond: failCreate
        ? {
          status: 422,
          body: {
            type: "validation",
            title: "Validation failed",
            user_message: "Date range overlaps another closure.",
          },
        }
        : {
          status: 201,
          body: {
            id: "closure_new",
            property_id: "prop_1",
            unit_id: null,
            starts_at: "2026-04-16T00:00:00Z",
            ends_at: "2026-04-18T00:00:00Z",
            reason: "Owner repainting west wing",
            source_ical_feed_id: null,
            created_by_user_id: "user_1",
            created_at: "2026-04-16T12:00:00Z",
            deleted_at: null,
          },
        },
    },
    {
      path: "/w/acme/api/v1/property_closures/closure_1",
      method: "PATCH",
      respond: failUpdate
        ? {
          status: 422,
          body: {
            type: "validation",
            title: "Validation failed",
            user_message: "Date range overlaps another closure.",
          },
        }
        : {
          body: {
            id: "closure_1",
            property_id: "prop_1",
            unit_id: null,
            starts_at: "2026-04-11T00:00:00Z",
            ends_at: "2026-04-12T00:00:00Z",
            reason: "Owner maintenance",
            source_ical_feed_id: null,
            created_by_user_id: "user_1",
            created_at: "2026-04-10T12:00:00Z",
            deleted_at: null,
          },
        },
    },
    {
      path: "/w/acme/api/v1/property_closures/closure_1",
      method: "DELETE",
      respond: { status: 204 },
    },
    {
      path: "/w/acme/api/v1/properties",
      respond: {
        body: missingProperty ? [] : [{
          id: "prop_1",
          name: "Villa Rosa",
          city: "Porto",
          timezone: "Europe/Lisbon",
          color: "moss",
          kind: "str",
          areas: ["Kitchen"],
          evidence_policy: "inherit",
          country: "PT",
          locale: "pt-PT",
          settings_override: {},
          client_org_id: null,
          owner_user_id: null,
        }],
      },
    },
    {
      path: "/w/acme/api/v1/properties/prop_1",
      respond: {
        body: missingProperty ? { id: "prop_1" } : {
          id: "prop_1",
          name: "Villa Rosa",
          kind: "str",
          address_json: { city: "Porto" },
          country: "PT",
          locale: "pt-PT",
          timezone: "Europe/Lisbon",
          client_org_id: null,
          owner_user_id: null,
        },
      },
    },
    {
      path: "/w/acme/api/v1/property_closures?property_id=prop_1&limit=100",
      respond: () => failClosures
        ? { status: 500, body: { type: "server_error", title: "Server error" } }
        : {
          body: {
            data: emptyClosures ? [] : [{
              id: "closure_1",
              property_id: "prop_1",
              starts_at: "2026-04-10T00:00:00Z",
              ends_at: "2026-04-13T00:00:00Z",
              reason: manualReason,
              source_ical_feed_id: null,
            },
            {
              id: "closure_2",
              property_id: "prop_1",
              starts_at: "2026-04-20T00:00:00Z",
              ends_at: "2026-04-22T00:00:00Z",
              reason: "Owner repainting west wing",
              source_ical_feed_id: "feed_1",
            }],
            next_cursor: null,
            has_more: false,
          },
        },
    },
    {
      path: "/w/acme/api/v1/stays/reservations?property_id=prop_1&limit=100",
      respond: {
        body: {
          data: [{
            id: "res_1",
            property_id: "prop_1",
            check_in: "2026-04-15T15:00:00Z",
            check_out: "2026-04-17T10:00:00Z",
            guest_name: "Ada Guest",
            guest_count: 2,
            status: "scheduled",
            source: "api",
          }],
          next_cursor: null,
          has_more: false,
        },
      },
    },
    {
      path: "/w/acme/api/v1/me",
      respond: {
        body: {
          role: "manager",
          theme: "system",
          agent_sidebar_collapsed: false,
          employee: {
            id: "emp_1",
            user_id: "user_1",
            first_name: "Mina",
            last_name: "Manager",
            email: "mina@example.test",
            phone: null,
            avatar_url: null,
          },
          manager_name: "Mina",
          today: "2026-04-16",
          now: "2026-04-16T12:00:00Z",
          user_id: "user_1",
          agent_approval_mode: "confirm",
          current_workspace_id: "ws_1",
          available_workspaces: [],
          client_binding_org_ids: [],
          is_deployment_admin: false,
          is_deployment_owner: false,
        },
      },
    },
  ]);
  return {
    get calls() {
      return env.requests.map((request) => request.url);
    },
    get requests() {
      return env.requests.map(({ url, init }) => ({ url, init }));
    },
    restore: env.restore,
  };
}

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/w/acme/property/prop_1/closures"]}>
        <WorkspaceProvider>
          <Routes>
            <Route path="/w/:slug/property/:pid/closures" element={<PropertyClosuresPage />} />
          </Routes>
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  });
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<PropertyClosuresPage>", () => {
  it("renders the promoted mock from production property closure endpoints", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByRole("heading", { name: "Villa Rosa — closures" })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Back to property/ })).toHaveAttribute("href", "/w/acme/property/prop_1");
      expect(screen.getByRole("button", { name: "+ Add closure" })).toBeInTheDocument();
      expect(screen.getByRole("table", { name: "Property closures" })).toBeInTheDocument();
      expect(screen.getByLabelText("Renovation closure from 10 Apr to 12 Apr")).toBeInTheDocument();
      expect(screen.getAllByText("Renovation").length).toBeGreaterThan(0);
      expect(screen.getByText("Airbnb / VRBO iCal")).toBeInTheDocument();
      expect(screen.getByText("Imported iCal unavailable date. Edit or remove it in Airbnb / VRBO.")).toBeInTheDocument();
      const importedRow = screen.getByLabelText("Owner repainting west wing closure from 20 Apr to 21 Apr");
      expect(within(importedRow).getByRole("button", { name: "Edit" })).toBeDisabled();
      expect(within(importedRow).getByRole("button", { name: "Delete" })).toBeDisabled();
      expect(screen.getByText("Calendar view")).toBeInTheDocument();
      const calendar = screen.getByRole("grid", { name: "April 2026 property calendar" });
      expect(Array.from(calendar.children).slice(0, 10).map((el) => el.textContent)).toEqual([
        "Mon",
        "Tue",
        "Wed",
        "Thu",
        "Fri",
        "Sat",
        "Sun",
        "",
        "",
        "1",
      ]);
      expect(calendar.querySelectorAll(".mini-cal__blank")).toHaveLength(2);
      expect(within(calendar).getByRole("gridcell", { name: "2026-04-16" })).toHaveClass("mini-cal__day--today");
      expect(fake.calls).toContain("/w/acme/api/v1/property_closures?property_id=prop_1&limit=100");
      expect(fake.calls).toContain("/w/acme/api/v1/stays/reservations?property_id=prop_1&limit=100");
    } finally {
      fake.restore();
    }
  });

  it("renders the mock failure copy when the closures query fails", async () => {
    const fake = installFetch({ failClosures: true });
    try {
      render(<Harness />);

      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "Villa Rosa — closures" })).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("renders failure copy when the property payload is missing", async () => {
    const fake = installFetch({ missingProperty: true });
    try {
      render(<Harness />);

      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "Villa Rosa — closures" })).toBeNull();
      expect(fake.calls).toContain("/w/acme/api/v1/property_closures?property_id=prop_1&limit=100");
    } finally {
      fake.restore();
    }
  });

  it("posts a new manual closure through the production endpoint", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "+ Add closure" }));
      expect(screen.queryByRole("dialog")).toBeNull();
      const createRow = await screen.findByLabelText("New closure");
      expect(within(createRow).getByRole("button", { name: "Save" })).toBeEnabled();
      fireEvent.change(within(createRow).getByLabelText("Start date"), { target: { value: "2026-04-16" } });
      fireEvent.change(within(createRow).getByLabelText("End date"), { target: { value: "2026-04-18" } });
      fireEvent.change(within(createRow).getByLabelText("Reason"), { target: { value: "  Owner repainting west wing  " } });
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/property_closures" && request.init?.method === "POST")).toBe(true);
      });
      const request = fake.requests.find((entry) => entry.url === "/w/acme/api/v1/property_closures" && entry.init?.method === "POST");
      expect(JSON.parse(String(request?.init?.body))).toEqual({
        property_id: "prop_1",
        unit_id: null,
        starts_at: "2026-04-16T00:00:00Z",
        ends_at: "2026-04-19T00:00:00.000Z",
        reason: "Owner repainting west wing",
        source_ical_feed_id: null,
      });
    } finally {
      fake.restore();
    }
  });

  it("keeps create-row API errors and drafts inside the inline table", async () => {
    const fake = installFetch({ failCreate: true });
    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "+ Add closure" }));
      const createRow = await screen.findByLabelText("New closure");
      fireEvent.change(within(createRow).getByLabelText("Start date"), { target: { value: "2026-04-16" } });
      fireEvent.change(within(createRow).getByLabelText("End date"), { target: { value: "2026-04-18" } });
      fireEvent.change(within(createRow).getByLabelText("Reason"), { target: { value: "Owner repainting west wing" } });
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));

      expect(await within(createRow).findByText("Date range overlaps another closure.")).toBeInTheDocument();
      expect(within(createRow).getByLabelText("Start date")).toHaveValue("2026-04-16");
      expect(within(createRow).getByLabelText("End date")).toHaveValue("2026-04-18");
      expect(within(createRow).getByLabelText("Reason")).toHaveValue("Owner repainting west wing");
    } finally {
      fake.restore();
    }
  });

  it("patches and deletes an existing manual closure", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      const manualRow = await screen.findByLabelText("Renovation closure from 10 Apr to 12 Apr");
      fireEvent.click(within(manualRow).getByRole("button", { name: "Edit" }));
      fireEvent.change(within(manualRow).getByLabelText("Start date"), { target: { value: "2026-04-11" } });
      fireEvent.change(within(manualRow).getByLabelText("Reason"), { target: { value: "Owner maintenance" } });
      fireEvent.click(within(manualRow).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/property_closures/closure_1" && request.init?.method === "PATCH")).toBe(true);
      });
      const patch = fake.requests.find((entry) => entry.url === "/w/acme/api/v1/property_closures/closure_1" && entry.init?.method === "PATCH");
      expect(JSON.parse(String(patch?.init?.body))).toEqual({
        unit_id: null,
        starts_at: "2026-04-11T00:00:00Z",
        ends_at: "2026-04-13T00:00:00.000Z",
        reason: "Owner maintenance",
        source_ical_feed_id: null,
      });

      const refreshedManualRow = await screen.findByLabelText("Renovation closure from 10 Apr to 12 Apr");
      fireEvent.click(within(refreshedManualRow).getByRole("button", { name: "Delete" }));
      fireEvent.click(screen.getByRole("button", { name: "Delete row" }));

      await waitFor(() => {
        expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/property_closures/closure_1" && request.init?.method === "DELETE")).toBe(true);
      });
    } finally {
      fake.restore();
    }
  });

  it("keeps manual closures editable when their reason matches the imported default text", async () => {
    const fake = installFetch({ manualReason: "iCal unavailable" });
    try {
      render(<Harness />);

      const manualRow = await screen.findByLabelText("iCal unavailable closure from 10 Apr to 12 Apr");
      expect(within(manualRow).getByText("manual")).toBeInTheDocument();
      expect(within(manualRow).getByRole("button", { name: "Edit" })).toBeEnabled();
      expect(within(manualRow).getByRole("button", { name: "Delete" })).toBeEnabled();

      fireEvent.click(within(manualRow).getByRole("button", { name: "Edit" }));
      fireEvent.change(within(manualRow).getByLabelText("Reason"), { target: { value: "Owner maintenance" } });
      fireEvent.click(within(manualRow).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/property_closures/closure_1" && request.init?.method === "PATCH")).toBe(true);
      });
      const patch = fake.requests.find((entry) => entry.url === "/w/acme/api/v1/property_closures/closure_1" && entry.init?.method === "PATCH");
      expect(JSON.parse(String(patch?.init?.body))).toMatchObject({
        reason: "Owner maintenance",
        source_ical_feed_id: null,
      });
    } finally {
      fake.restore();
    }
  });

  it("keeps manual-row API errors and drafts inside the inline table", async () => {
    const fake = installFetch({ failUpdate: true });
    try {
      render(<Harness />);

      const manualRow = await screen.findByLabelText("Renovation closure from 10 Apr to 12 Apr");
      fireEvent.click(within(manualRow).getByRole("button", { name: "Edit" }));
      fireEvent.change(within(manualRow).getByLabelText("Start date"), { target: { value: "2026-04-11" } });
      fireEvent.change(within(manualRow).getByLabelText("Reason"), { target: { value: "Owner maintenance" } });
      fireEvent.click(within(manualRow).getByRole("button", { name: "Save" }));

      expect(await within(manualRow).findByText("Date range overlaps another closure.")).toBeInTheDocument();
      expect(within(manualRow).getByLabelText("Start date")).toHaveValue("2026-04-11");
      expect(within(manualRow).getByLabelText("Reason")).toHaveValue("Owner maintenance");
    } finally {
      fake.restore();
    }
  });

  it("cancels manual closure edits without patching", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      const manualRow = await screen.findByLabelText("Renovation closure from 10 Apr to 12 Apr");
      fireEvent.click(within(manualRow).getByRole("button", { name: "Edit" }));
      fireEvent.change(within(manualRow).getByLabelText("Reason"), { target: { value: "Owner maintenance" } });
      fireEvent.click(within(manualRow).getByRole("button", { name: "Cancel" }));

      expect(screen.getByLabelText("Renovation closure from 10 Apr to 12 Apr")).toBeInTheDocument();
      expect(screen.queryByLabelText("Owner maintenance closure from 10 Apr to 12 Apr")).toBeNull();
      expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/property_closures/closure_1" && request.init?.method === "PATCH")).toBe(false);
    } finally {
      fake.restore();
    }
  });

  it("shows empty and validation states inside the inline table", async () => {
    const fake = installFetch({ emptyClosures: true });
    try {
      render(<Harness />);

      const createRow = await screen.findByLabelText("New closure");
      expect(within(createRow).getByRole("heading", { name: "No closures scheduled" })).toBeInTheDocument();

      fireEvent.change(within(createRow).getByLabelText("End date"), { target: { value: "2026-04-15" } });
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));

      expect(within(createRow).getByText("End date must be on or after the start date.")).toBeInTheDocument();
      expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/property_closures" && request.init?.method === "POST")).toBe(false);
    } finally {
      fake.restore();
    }
  });
});
