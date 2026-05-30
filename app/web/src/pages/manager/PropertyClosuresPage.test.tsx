import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import managerPanelsCss from "@/styles/manager-panels.css?raw";
import PropertyClosuresPage from "./PropertyClosuresPage";
import { installFetchRouteHandlers } from "@/test/helpers";

interface TestClosurePayload {
  id: string;
  property_id: string;
  starts_at: string;
  ends_at: string;
  reason: string;
  source_ical_feed_id: string | null;
}

function defaultClosureRows(manualReason: string, boundaryClosure: boolean): TestClosurePayload[] {
  return [{
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
  },
  ...(boundaryClosure ? [{
    id: "closure_3",
    property_id: "prop_1",
    starts_at: "2026-04-29T00:00:00Z",
    ends_at: "2026-05-04T00:00:00Z",
    reason: "Owner stay",
    source_ical_feed_id: null,
  }] : [])];
}

function installFetch({
  boundaryClosure = false,
  closureRows,
  emptyClosures = false,
  failCreate = false,
  failClosures = false,
  failUpdate = false,
  manualReason = "Renovation",
  missingProperty = false,
}: {
  boundaryClosure?: boolean;
  closureRows?: TestClosurePayload[];
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
            data: emptyClosures ? [] : closureRows ?? defaultClosureRows(manualReason, boundaryClosure),
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

function cssRule(selector: string, css = managerPanelsCss, occurrence = 0) {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const matches = Array.from(css.matchAll(new RegExp(`^\\s*${escapedSelector}\\s*{([^}]*)}`, "gm")));
  const match = matches[occurrence];
  expect(match, `${selector} rule should exist`).not.toBeNull();
  return match?.[1] ?? "";
}

function expectCssDeclaration(rule: string, property: string, value: string) {
  const escapedProperty = property.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const escapedValue = value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  expect(rule).toMatch(new RegExp(`${escapedProperty}\\s*:\\s*${escapedValue}\\s*;`));
}

function expectDirectSourceChip(cell: Element | null, label: string) {
  expect(cell).toHaveClass("property-closure-source");
  const chip = Array.from(cell?.children ?? []).find((child) => child.classList.contains("property-closure-source-chip"));
  expect(chip).toBeInstanceOf(HTMLElement);
  expect(within(chip as HTMLElement).getByText(label)).toHaveClass("chip");
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

      expect(await screen.findByRole("heading", { name: "Villa Rosa" })).toBeInTheDocument();
      expect(screen.getByText("Porto · Europe/Lisbon")).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: /Back to property/ })).toBeNull();
      expect(screen.queryByText(/events upsert here automatically/)).toBeNull();
      expect(screen.queryByRole("button", { name: "+ Add closure" })).toBeNull();
      const propertySections = screen.getByRole("navigation", { name: "Property sections" });
      expect(within(propertySections).getByRole("link", { name: "Overview" })).toHaveAttribute("href", "/w/acme/property/prop_1");
      expect(within(propertySections).getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/w/acme/property/prop_1#settings");
      const relatedPages = screen.getByRole("navigation", { name: "Related property pages" });
      expect(within(relatedPages).getByRole("link", { name: "Stays" })).toHaveAttribute("href", "/w/acme/property/prop_1/stays");
      expect(within(relatedPages).getByRole("link", { name: "Instructions" })).toHaveAttribute("href", "/w/acme/property/prop_1/instructions");
      expect(within(relatedPages).getByRole("link", { name: "Closures" })).toHaveAttribute("href", "/w/acme/property/prop_1/closures");
      expect(within(relatedPages).getByRole("link", { name: "Closures" })).toHaveAttribute("aria-current", "page");
      expect(within(relatedPages).getByRole("link", { name: "Closures" })).toHaveClass("page-tabs__tab--active");
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
      expect(screen.getByRole("grid", { name: "May 2026 property calendar" })).toBeInTheDocument();
      expect(screen.getByRole("grid", { name: "June 2026 property calendar" })).toBeInTheDocument();
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
      expect(within(calendar).getByRole("gridcell", { name: /^2026-04-16\b/ })).toHaveClass("mini-cal__day--today");
      const importedCell = within(calendar).getByRole("gridcell", {
        name: /2026-04-20.*Owner repainting west wing closure, iCal source/,
      });
      expect(within(importedCell).getByText("Owner repainting west wing")).toBeInTheDocument();
      expect(within(importedCell).getByText("iCal")).toBeInTheDocument();
      expect(within(importedCell).getByTitle("Owner repainting west wing closure, iCal source")).toBeInTheDocument();
      expect(fake.calls).toContain("/w/acme/api/v1/property_closures?property_id=prop_1&limit=100");
      expect(fake.calls).toContain("/w/acme/api/v1/stays/reservations?property_id=prop_1&limit=100");
    } finally {
      fake.restore();
    }
  });

  it("renders closure reason and source across the current and next two calendar months", async () => {
    const fake = installFetch({ boundaryClosure: true });
    try {
      render(<Harness />);

      const april = await screen.findByRole("grid", { name: "April 2026 property calendar" });
      const may = screen.getByRole("grid", { name: "May 2026 property calendar" });
      expect(screen.getByRole("grid", { name: "June 2026 property calendar" })).toBeInTheDocument();

      const aprilStart = within(april).getByRole("gridcell", { name: /^2026-04-29\b/ });
      const mayMiddle = within(may).getByRole("gridcell", { name: /^2026-05-01\b/ });
      const mayEnd = within(may).getByRole("gridcell", { name: /^2026-05-03\b/ });
      const exclusiveEnd = within(may).getByRole("gridcell", { name: "2026-05-04" });

      for (const cell of [aprilStart, mayMiddle, mayEnd]) {
        expect(within(cell).getByText("Owner stay")).toBeInTheDocument();
        expect(within(cell).getByText("manual")).toBeInTheDocument();
        expect(within(cell).getByTitle("Owner stay closure, manual source")).toBeInTheDocument();
      }
      expect(within(exclusiveEnd).queryByText("Owner stay")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("selects a draft closure range from calendar day pointers into the create row", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      const createRow = await screen.findByLabelText("New closure");
      const april = screen.getByRole("grid", { name: "April 2026 property calendar" });
      const dayCell = (iso: string) => within(april).getByRole("gridcell", { name: new RegExp(`^${iso}\\b`) });

      fireEvent.pointerDown(dayCell("2026-04-10"), { button: 0 });
      fireEvent.pointerEnter(dayCell("2026-04-16"), { buttons: 1 });

      expect(within(createRow).getByLabelText("Start date")).toHaveValue("2026-04-10");
      expect(within(createRow).getByLabelText("End date")).toHaveValue("2026-04-16");
      expect(within(dayCell("2026-04-12")).getByLabelText("Draft closure")).toBeInTheDocument();
      expect(within(dayCell("2026-04-10")).getByText("Renovation")).toBeInTheDocument();
      expect(dayCell("2026-04-16").querySelector(".mini-cal__bar")).toBeInTheDocument();
      fireEvent.pointerUp(dayCell("2026-04-16"));

      fireEvent.change(within(createRow).getByLabelText("Reason"), { target: { value: "Floor repair" } });
      expect(within(dayCell("2026-04-12")).getByLabelText("Floor repair draft closure")).toBeInTheDocument();
      expect(within(dayCell("2026-04-12")).getByText("Floor repair")).toBeInTheDocument();

      fireEvent.change(within(createRow).getByLabelText("Reason"), { target: { value: "" } });
      expect(within(dayCell("2026-04-12")).getByLabelText("Draft closure")).toBeInTheDocument();
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));
      expect(within(createRow).getByText("Reason is required.")).toBeInTheDocument();
      expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/property_closures" && request.init?.method === "POST")).toBe(false);

      fireEvent.pointerDown(dayCell("2026-04-10"), { button: 0 });
      fireEvent.pointerEnter(dayCell("2026-04-06"), { buttons: 1 });
      fireEvent.pointerUp(dayCell("2026-04-06"));

      expect(within(createRow).getByLabelText("Start date")).toHaveValue("2026-04-06");
      expect(within(createRow).getByLabelText("End date")).toHaveValue("2026-04-10");
      expect(within(dayCell("2026-04-08")).getByLabelText("Draft closure")).toBeInTheDocument();
      expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/property_closures" && request.init?.method === "POST")).toBe(false);

      fireEvent.change(within(createRow).getByLabelText("Start date"), { target: { value: "2026-04-14" } });
      fireEvent.change(within(createRow).getByLabelText("End date"), { target: { value: "2026-04-15" } });

      expect(within(dayCell("2026-04-14")).getByLabelText("Draft closure")).toBeInTheDocument();
      expect(within(dayCell("2026-04-15")).getByLabelText("Draft closure")).toBeInTheDocument();
      expect(within(dayCell("2026-04-13")).queryByLabelText("Draft closure")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("stops calendar range selection when a stale drag re-enters without the primary button held", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      const createRow = await screen.findByLabelText("New closure");
      const april = screen.getByRole("grid", { name: "April 2026 property calendar" });
      const dayCell = (iso: string) => within(april).getByRole("gridcell", { name: new RegExp(`^${iso}\\b`) });

      fireEvent.pointerDown(dayCell("2026-04-10"), { button: 0 });
      fireEvent.pointerEnter(dayCell("2026-04-16"), { buttons: 0 });

      expect(within(createRow).getByLabelText("Start date")).toHaveValue("2026-04-10");
      expect(within(createRow).getByLabelText("End date")).toHaveValue("2026-04-10");
      expect(within(dayCell("2026-04-16")).queryByLabelText("Draft closure")).toBeNull();

      fireEvent.pointerEnter(dayCell("2026-04-18"), { buttons: 1 });

      expect(within(createRow).getByLabelText("Start date")).toHaveValue("2026-04-10");
      expect(within(createRow).getByLabelText("End date")).toHaveValue("2026-04-10");
      expect(within(dayCell("2026-04-18")).queryByLabelText("Draft closure")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("stacks closure calendar months and lays out closure chip text vertically", async () => {
    const fake = installFetch({
      closureRows: [{
        id: "closure_overlap",
        property_id: "prop_1",
        starts_at: "2026-04-16T00:00:00Z",
        ends_at: "2026-04-17T00:00:00Z",
        reason: "Owner stay",
        source_ical_feed_id: null,
      }],
    });
    try {
      render(<Harness />);

      const calendarGroup = await screen.findByRole("group", { name: "Three-month property calendar" });
      const months = Array.from(calendarGroup.querySelectorAll(".property-calendar__month"));
      expect(months).toHaveLength(3);
      expect(months.map((month) => within(month as HTMLElement).getByRole("heading").textContent)).toEqual([
        "April 2026",
        "May 2026",
        "June 2026",
      ]);

      const april = screen.getByRole("grid", { name: "April 2026 property calendar" });
      const overlapCell = within(april).getByRole("gridcell", {
        name: /2026-04-16.*Owner stay closure, manual source.*Ada Guest stay, manual source/,
      });
      const closureChip = within(overlapCell).getByTitle("Owner stay closure, manual source");
      const stayBar = overlapCell.querySelector(".mini-cal__bar");
      const [dayNumber, chipSibling, staySibling] = Array.from(overlapCell.children);

      expect(dayNumber).toHaveClass("mini-cal__num");
      expect(chipSibling).toBe(closureChip);
      expect(staySibling).toBe(stayBar);
      expect(stayBar).toBeInTheDocument();
      expect(Array.from(closureChip.children).map((child) => child.className)).toEqual([
        "mini-cal__closure-reason",
        "mini-cal__closure-source",
      ]);

      const calendarRule = cssRule(".property-calendar");
      const closureRule = cssRule(".mini-cal__closure");
      const closureTextRule = cssRule(".mini-cal__closure-reason,\n.mini-cal__closure-source");
      const sourceRule = cssRule(".mini-cal__closure-source", managerPanelsCss, 1);

      expectCssDeclaration(calendarRule, "grid-template-columns", "minmax(0, 1fr)");
      expect(managerPanelsCss).not.toMatch(/\.property-calendar\s*{[^}]*repeat\(auto-fit/);
      expectCssDeclaration(closureRule, "display", "flex");
      expectCssDeclaration(closureRule, "flex-direction", "column");
      expectCssDeclaration(closureRule, "max-width", "100%");
      expectCssDeclaration(closureRule, "overflow", "hidden");
      expectCssDeclaration(closureTextRule, "width", "100%");
      expectCssDeclaration(closureTextRule, "max-width", "100%");
      expectCssDeclaration(closureTextRule, "text-overflow", "ellipsis");
      expectCssDeclaration(sourceRule, "color", "var(--ink-3)");
      expect(managerPanelsCss).not.toMatch(/\.mini-cal__closure-source\s*{[^}]*clip:\s*rect/);
    } finally {
      fake.restore();
    }
  });

  it("hides past closure rows until the archive control reveals them", async () => {
    const fake = installFetch({
      closureRows: [
        {
          id: "closure_past",
          property_id: "prop_1",
          starts_at: "2026-03-27T00:00:00Z",
          ends_at: "2026-04-01T00:00:00Z",
          reason: "March plumbing",
          source_ical_feed_id: null,
        },
        {
          id: "closure_current",
          property_id: "prop_1",
          starts_at: "2026-03-29T00:00:00Z",
          ends_at: "2026-04-03T00:00:00Z",
          reason: "Early April maintenance",
          source_ical_feed_id: null,
        },
        {
          id: "closure_imported_past",
          property_id: "prop_1",
          starts_at: "2026-03-24T00:00:00Z",
          ends_at: "2026-04-01T00:00:00Z",
          reason: "Imported owner block",
          source_ical_feed_id: "feed_1",
        },
        {
          id: "closure_future",
          property_id: "prop_1",
          starts_at: "2026-05-07T00:00:00Z",
          ends_at: "2026-05-10T00:00:00Z",
          reason: "May repainting",
          source_ical_feed_id: null,
        },
      ],
    });
    try {
      render(<Harness />);

      expect(await screen.findByRole("button", { name: "Show 2 past closures" })).toBeInTheDocument();
      expect(screen.getByText("2 past closures hidden")).toBeInTheDocument();
      expect(screen.queryByLabelText("March plumbing closure from 27 Mar to 31 Mar")).toBeNull();
      expect(screen.queryByLabelText("Imported owner block closure from 24 Mar to 31 Mar")).toBeNull();
      expect(screen.getByLabelText("Early April maintenance closure from 29 Mar to 02 Apr")).toBeInTheDocument();
      expect(screen.getByLabelText("May repainting closure from 07 May to 09 May")).toBeInTheDocument();
      expect(screen.getByLabelText("New closure")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Show 2 past closures" }));

      const pastRow = screen.getByLabelText("March plumbing closure from 27 Mar to 31 Mar");
      const importedPastRow = screen.getByLabelText("Imported owner block closure from 24 Mar to 31 Mar");
      expect(pastRow).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Hide past closures" })).toHaveAttribute("aria-pressed", "true");
      expect(screen.getByText("2 past closures shown")).toBeInTheDocument();
      expect(within(pastRow).getByRole("button", { name: "Edit" })).toBeEnabled();
      expect(within(pastRow).getByRole("button", { name: "Delete" })).toBeEnabled();
      expect(within(importedPastRow).getByRole("button", { name: "Edit" })).toBeDisabled();
      expect(within(importedPastRow).getByRole("button", { name: "Delete" })).toBeDisabled();
    } finally {
      fake.restore();
    }
  });

  it("renders the mock failure copy when the closures query fails", async () => {
    const fake = installFetch({ failClosures: true });
    try {
      render(<Harness />);

      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "Villa Rosa" })).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("renders failure copy when the property payload is missing", async () => {
    const fake = installFetch({ missingProperty: true });
    try {
      render(<Harness />);

      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "Villa Rosa" })).toBeNull();
      expect(fake.calls).toContain("/w/acme/api/v1/property_closures?property_id=prop_1&limit=100");
    } finally {
      fake.restore();
    }
  });

  it("posts a new manual closure through the production endpoint", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      const createRow = await screen.findByLabelText("New closure");
      const reasonField = within(createRow).getByLabelText("Reason");
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(reasonField).toHaveValue("");
      expect(reasonField).toHaveAttribute("placeholder", "Enter a reason");
      expect(within(createRow).getByRole("button", { name: "Save" })).toBeDisabled();
      fireEvent.change(within(createRow).getByLabelText("Start date"), { target: { value: "2026-04-16" } });
      fireEvent.change(within(createRow).getByLabelText("End date"), { target: { value: "2026-04-18" } });
      fireEvent.change(reasonField, { target: { value: "  Owner repainting west wing  " } });
      expect(within(createRow).getByRole("button", { name: "Save" })).toBeEnabled();
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
      await waitFor(() => {
        expect(within(createRow).getByLabelText("End date")).toHaveValue("2026-04-16");
      });
      expect(reasonField).toHaveValue("");
      expect(reasonField).toHaveAttribute("placeholder", "Enter a reason");
      expect(within(createRow).getByRole("button", { name: "Save" })).toBeDisabled();
    } finally {
      fake.restore();
    }
  });

  it("allocates enough inline table width for closure date controls", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      const createRow = await screen.findByLabelText("New closure");
      const startDate = within(createRow).getByLabelText("Start date");
      const endDate = within(createRow).getByLabelText("End date");
      const inlineForm = screen.getByRole("table", { name: "Property closures" }).closest(".inline-table-form");

      expect(startDate).toHaveClass("inline-table-form__control--date");
      expect(endDate).toHaveClass("inline-table-form__control--date");
      expect(startDate).toHaveValue("2026-04-16");
      expect(endDate).toHaveValue("2026-04-16");
      expect(inlineForm).toHaveAttribute(
        "style",
        expect.stringContaining("--inline-table-columns: 156px 156px"),
      );
    } finally {
      fake.restore();
    }
  });

  it("keeps closure source chips compact in read, edit, and create rows", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      const readRow = await screen.findByLabelText("Renovation closure from 10 Apr to 12 Apr");
      const readSourceCell = readRow.querySelector('[data-inline-table-column="source"]');
      expectDirectSourceChip(readSourceCell, "manual");

      const importedRow = screen.getByLabelText("Owner repainting west wing closure from 20 Apr to 21 Apr");
      const importedSourceCell = importedRow.querySelector('[data-inline-table-column="source"]');
      expectDirectSourceChip(importedSourceCell, "Airbnb / VRBO iCal");

      fireEvent.click(within(readRow).getByRole("button", { name: "Edit" }));
      const editSourceCell = readRow.querySelector('[data-inline-table-column="source"]');
      expectDirectSourceChip(editSourceCell, "manual");

      const createRow = screen.getByLabelText("New closure");
      const createSourceCell = createRow.querySelector('[data-inline-table-column="source"]');
      expectDirectSourceChip(createSourceCell, "manual");

      const chipRule = cssRule(".property-closure-source-chip");
      expectCssDeclaration(chipRule, "display", "inline-flex");
      expectCssDeclaration(chipRule, "width", "fit-content");
      const editRule = cssRule(
        ".inline-table-form__group.is-editing .inline-table-form__td.property-closure-source > .property-closure-source-chip",
      );
      expectCssDeclaration(editRule, "flex", "0 1 auto");
    } finally {
      fake.restore();
    }
  });

  it("keeps create-row API errors and drafts inside the inline table", async () => {
    const fake = installFetch({ failCreate: true });
    try {
      render(<Harness />);

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
      expect(within(manualRow).getByLabelText("Reason")).toHaveValue("Renovation");
      expect(within(manualRow).getByLabelText("Reason")).toHaveAttribute("placeholder", "Enter a reason");
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

  it("cancels create-row drafts back to an empty reason", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      const createRow = await screen.findByLabelText("New closure");
      fireEvent.change(within(createRow).getByLabelText("End date"), { target: { value: "2026-04-18" } });
      fireEvent.change(within(createRow).getByLabelText("Reason"), { target: { value: "Owner maintenance" } });
      fireEvent.click(within(createRow).getByRole("button", { name: "Cancel" }));

      expect(within(createRow).getByLabelText("Start date")).toHaveValue("2026-04-16");
      expect(within(createRow).getByLabelText("End date")).toHaveValue("2026-04-16");
      expect(within(createRow).getByLabelText("Reason")).toHaveValue("");
      expect(within(createRow).getByLabelText("Reason")).toHaveAttribute("placeholder", "Enter a reason");
      expect(within(createRow).getByRole("button", { name: "Save" })).toBeDisabled();
      expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/property_closures" && request.init?.method === "POST")).toBe(false);
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

  it("requires a reason before creating a manual closure", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      const createRow = await screen.findByLabelText("New closure");
      fireEvent.change(within(createRow).getByLabelText("End date"), { target: { value: "2026-04-17" } });
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));

      expect(within(createRow).getByText("Reason is required.")).toBeInTheDocument();
      expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/property_closures" && request.init?.method === "POST")).toBe(false);
    } finally {
      fake.restore();
    }
  });
});
