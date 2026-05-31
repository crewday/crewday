import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { qk, __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { installFetchRouteHandlers } from "@/test/helpers";
import StaysPage from "./StaysPage";

const property = {
  id: "prop_1",
  name: "Villa Rosa",
  city: "Nice",
  timezone: "Europe/Paris",
  color: "moss",
  kind: "str",
  areas: [],
  evidence_policy: "inherit",
  country: "FR",
  locale: "fr-FR",
  settings_override: {},
  client_org_id: null,
  owner_user_id: "usr_1",
};

const secondProperty = {
  ...property,
  id: "prop_2",
  name: "Beach House",
  city: "Cannes",
  color: "sky",
  owner_user_id: "usr_2",
};

const unit = {
  id: "unit_1",
  property_id: "prop_1",
  name: "Garden Suite",
  ordinal: 1,
  default_checkin_time: "16:00",
  default_checkout_time: "10:00",
  max_guests: 4,
  welcome_overrides_json: {},
  settings_override_json: {},
  notes_md: "",
  created_at: "2026-04-01T00:00:00Z",
  updated_at: null,
  deleted_at: null,
};

const secondUnit = {
  ...unit,
  id: "unit_2",
  name: "Roof Studio",
};

const otherPropertyUnit = {
  ...unit,
  id: "unit_3",
  property_id: "prop_2",
  name: "Cabana",
};

const existingReservation = {
  id: "res_existing",
  workspace_id: "ws_1",
  property_id: "prop_1",
  unit_id: "unit_1",
  ical_feed_id: null,
  external_uid: "manual-existing",
  check_in: "2026-04-18T16:00:00Z",
  check_out: "2026-04-20T10:00:00Z",
  guest_name: "Ada Existing",
  guest_count: 2,
  status: "confirmed",
  source: "manual",
  guest_link_id: null,
  created_at: "2026-04-01T00:00:00Z",
};

const otherPropertyReservation = {
  ...existingReservation,
  id: "res_other_property",
  property_id: "prop_2",
  unit_id: "unit_3",
  guest_name: "Other Guest",
};

function isoOffset(days: number): string {
  const date = new Date();
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function nextIso(iso: string): string {
  const date = new Date(iso + "T00:00:00Z");
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString().slice(0, 10);
}

function installFetch(options: {
  properties?: unknown[];
  reservations?: unknown[];
  units?: Record<string, unknown[]> | unknown[];
  feeds?: unknown[];
  leaves?: unknown[];
  leavesResponse?: unknown;
  employees?: unknown[];
  membershipRole?: "owner_workspace" | "managed_workspace" | "observer_workspace";
  shareGuestIdentity?: boolean;
  createStay?: (body: unknown) => { status?: number; body?: unknown };
  updateStay?: (id: string, body: unknown) => { status?: number; body?: unknown };
  createFeed?: (body: unknown) => { status?: number; body?: unknown };
  memberships?: Record<string, {
    membershipRole?: "owner_workspace" | "managed_workspace" | "observer_workspace";
    shareGuestIdentity?: boolean;
  }>;
} = {}) {
  const reservations = [...(options.reservations ?? [])];
  const feeds = [...(options.feeds ?? [])];
  const properties = options.properties ?? [property];
  const unitsByProperty = Array.isArray(options.units)
    ? { prop_1: options.units }
    : options.units ?? { prop_1: [unit] };
  const unitsFor = (propertyId: string) => unitsByProperty[propertyId] ?? [];
  const membershipFor = (propertyId: string) => ({
    membershipRole: options.memberships?.[propertyId]?.membershipRole ?? options.membershipRole ?? "owner_workspace",
    shareGuestIdentity: options.memberships?.[propertyId]?.shareGuestIdentity ?? options.shareGuestIdentity ?? false,
  });
  return installFetchRouteHandlers([
    {
      path: "/api/v1/auth/me",
      respond: {
        body: {
          user_id: "usr_1",
          display_name: "Mina",
          email: "mina@example.com",
          available_workspaces: [],
          current_workspace_id: "ws_1",
          is_deployment_admin: false,
        },
      },
    },
    {
      path: "/api/v1/me/workspaces",
      respond: {
        body: [
          {
            workspace_id: "ws_1",
            slug: "acme",
            name: "Acme",
            current_role: "manager",
            last_seen_at: null,
            settings_override: {},
          },
        ],
      },
    },
    {
      path: "/w/acme/api/v1/stays/reservations?limit=500",
      respond: { body: { data: reservations } },
    },
    {
      path: "/w/acme/api/v1/stays/reservations?property_id=prop_1&limit=500",
      respond: {
        body: {
          data: reservations.filter((row) => row && typeof row === "object" && "property_id" in row && row.property_id === "prop_1"),
        },
      },
    },
    {
      path: "/w/acme/api/v1/stays/reservations?property_id=prop_2&limit=500",
      respond: {
        body: {
          data: reservations.filter((row) => row && typeof row === "object" && "property_id" in row && row.property_id === "prop_2"),
        },
      },
    },
    {
      path: "/w/acme/api/v1/user_leaves?approved=true&limit=500",
      respond: { body: options.leavesResponse ?? { data: options.leaves ?? [] } },
    },
    { path: "/w/acme/api/v1/properties", respond: { body: properties } },
    { path: "/w/acme/api/v1/employees", respond: { body: options.employees ?? [] } },
    { path: "/w/acme/api/v1/stays/ical-feeds", respond: { body: feeds } },
    {
      path: "/w/acme/api/v1/stays/ical-feeds?property_id=prop_1",
      respond: {
        body: feeds.filter((row) => row && typeof row === "object" && "property_id" in row && row.property_id === "prop_1"),
      },
    },
    {
      path: "/w/acme/api/v1/stays/ical-feeds?property_id=prop_2",
      respond: {
        body: feeds.filter((row) => row && typeof row === "object" && "property_id" in row && row.property_id === "prop_2"),
      },
    },
    {
      path: "/w/acme/api/v1/properties/prop_1/units?limit=100",
      respond: { body: { data: unitsFor("prop_1") } },
    },
    {
      path: "/w/acme/api/v1/properties/prop_2/units?limit=100",
      respond: { body: { data: unitsFor("prop_2") } },
    },
    {
      path: "/w/acme/api/v1/properties/prop_1/share",
      respond: {
        body: {
          data: (() => {
            const membership = membershipFor("prop_1");
            return [{
              property_id: "prop_1",
              workspace_id: "ws_1",
              label: "Acme",
              membership_role: membership.membershipRole,
              status: "active",
              share_guest_identity: membership.shareGuestIdentity,
              created_at: "2026-04-01T00:00:00Z",
            }];
          })(),
        },
      },
    },
    {
      path: "/w/acme/api/v1/properties/prop_2/share",
      respond: {
        body: {
          data: (() => {
            const membership = membershipFor("prop_2");
            return [{
              property_id: "prop_2",
              workspace_id: "ws_1",
              label: "Acme",
              membership_role: membership.membershipRole,
              status: "active",
              share_guest_identity: membership.shareGuestIdentity,
              created_at: "2026-04-01T00:00:00Z",
            }];
          })(),
        },
      },
    },
    {
      path: "/w/acme/api/v1/stays",
      method: "POST",
      respond: (request) => {
        const response = options.createStay?.(request.body) ?? {
          status: 201,
          body: {
            id: "res_new",
            workspace_id: "ws_1",
            property_id: "prop_1",
            unit_id: "unit_1",
            ical_feed_id: null,
            external_uid: "manual-new",
            check_in: "2026-04-21T16:00:00Z",
            check_out: "2026-04-23T10:00:00Z",
            guest_name: "Bea Guest",
            guest_count: 3,
            status: "confirmed",
            source: "manual",
            guest_link_id: null,
            created_at: "2026-04-02T00:00:00Z",
          },
        };
        if ((response.status ?? 200) < 400) reservations.unshift(response.body);
        return response;
      },
    },
    {
      path: "/w/acme/api/v1/stays/res_existing",
      method: "PATCH",
      respond: (request) => {
        const id = request.path.split("/").at(-1) ?? "";
        const existingIndex = reservations.findIndex((row) => (
          row && typeof row === "object" && "id" in row && row.id === id
        ));
        const existing = existingIndex >= 0 && reservations[existingIndex] && typeof reservations[existingIndex] === "object"
          ? reservations[existingIndex] as Record<string, unknown>
          : existingReservation;
        const requestBody = request.body && typeof request.body === "object"
          ? request.body as Record<string, unknown>
          : {};
        const response = options.updateStay?.(id, request.body) ?? {
          status: 200,
          body: {
            ...existing,
            id,
            property_id: requestBody.property_id ?? existing.property_id,
            unit_id: requestBody.unit_id ?? existing.unit_id,
            check_in: requestBody.check_in_at ?? existing.check_in,
            check_out: requestBody.check_out_at ?? existing.check_out,
            guest_name: requestBody.guest_name ?? existing.guest_name,
            guest_count: requestBody.guest_count ?? existing.guest_count,
            status: requestBody.status ?? existing.status,
            source: "manual",
          },
        };
        if ((response.status ?? 200) < 400) {
          if (existingIndex >= 0) reservations[existingIndex] = response.body;
          else reservations.unshift(response.body);
        }
        return response;
      },
    },
    {
      path: "/w/acme/api/v1/stays/ical-feeds",
      method: "POST",
      respond: (request) => {
        const provider = typeof request.body === "object" && request.body !== null && "provider_override" in request.body
          ? String(request.body.provider_override)
          : "gcal";
        const response = options.createFeed?.(request.body) ?? {
          status: 201,
          body: {
            id: "feed_1",
            workspace_id: "ws_1",
            property_id: "prop_1",
            unit_id: "unit_1",
            provider,
            provider_override: provider,
            url_preview: provider === "gcal" ? "https://calendar.google.com" : "https://calendar.airbnb.com",
            enabled: true,
            poll_cadence: "*/15 * * * *",
            last_polled_at: "2026-04-02T00:00:00Z",
            last_etag: null,
            last_error: null,
            created_at: "2026-04-02T00:00:00Z",
          },
        };
        if ((response.status ?? 200) < 400) feeds.unshift(response.body);
        return response;
      },
    },
  ]);
}

function Harness({ queryClient, initial = "/w/acme/stays" }: { queryClient?: QueryClient; initial?: string } = {}) {
  const qc = queryClient ?? new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <WorkspaceProvider>
          <Routes>
            <Route path="/w/:slug/stays" element={<StaysPage />} />
            <Route path="/w/:slug/property/:pid/stays" element={<StaysPage />} />
          </Routes>
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function chooseSearchableOption(
  container: HTMLElement,
  label: RegExp,
  query: string,
): Promise<void> {
  const input = within(container).getByRole("combobox", { name: label });
  fireEvent.change(input, { target: { value: query } });
  await screen.findByRole("option", { name: (name) => name.includes(query) });
  fireEvent.keyDown(input, { key: "Enter" });
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
  Element.prototype.scrollIntoView = vi.fn();
  class TestIntersectionObserver {
    observe = vi.fn();
    disconnect = vi.fn();
    unobserve = vi.fn();
  }
  vi.stubGlobal("IntersectionObserver", TestIntersectionObserver);
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
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("<StaysPage>", () => {
  it("keeps top-level stays unfiltered while the property route fetches and renders only that property", async () => {
    const fake = installFetch({
      properties: [property, secondProperty],
      reservations: [existingReservation, otherPropertyReservation],
      units: { prop_1: [unit], prop_2: [otherPropertyUnit] },
      feeds: [
        {
          id: "feed_prop_1",
          property_id: "prop_1",
          unit_id: "unit_1",
          provider: "airbnb",
          provider_override: "airbnb",
          url_preview: "https://calendar.airbnb.com",
          enabled: true,
          poll_cadence: "*/15 * * * *",
          last_polled_at: null,
          last_error: null,
        },
        {
          id: "feed_prop_2",
          property_id: "prop_2",
          unit_id: "unit_3",
          provider: "vrbo",
          provider_override: "vrbo",
          url_preview: "https://ical.vrbo.com",
          enabled: true,
          poll_cadence: "*/15 * * * *",
          last_polled_at: null,
          last_error: null,
        },
      ],
    });
    try {
      render(<Harness />);

      expect(await screen.findByText("Ada Existing")).toBeInTheDocument();
      expect(screen.getByText("Other Guest")).toBeInTheDocument();
      expect(fake.requests.some((request) => request.path === "/w/acme/api/v1/stays/reservations?limit=500")).toBe(true);

      cleanup();
      render(<Harness initial="/w/acme/property/prop_2/stays" />);

      expect(await screen.findByText("Other Guest")).toBeInTheDocument();
      expect(screen.queryByText("Ada Existing")).not.toBeInTheDocument();
      expect(
        screen.getByText("Imported from Airbnb, VRBO, and direct bookings. Property view: stays, turnover bundles, and closures."),
      ).toBeInTheDocument();
      expect(
        screen.queryByText("Imported from Airbnb, VRBO, and direct bookings. Four layers: stays, turnover bundles, closures, employee leave."),
      ).not.toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Stays" })).toHaveAttribute("aria-current", "page");
      expect(fake.requests.some((request) => request.path === "/w/acme/api/v1/stays/reservations?property_id=prop_2&limit=500")).toBe(true);
      expect(fake.requests.some((request) => request.path === "/w/acme/api/v1/stays/ical-feeds?property_id=prop_2")).toBe(true);
      expect(screen.getAllByText("Beach House").length).toBeGreaterThan(0);
      expect(screen.queryByText("Villa Rosa")).not.toBeInTheDocument();
      expect(screen.queryByText("Approved leave")).not.toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("defaults property-route stay and iCal forms to the active property's units", async () => {
    const fake = installFetch({
      properties: [property, secondProperty],
      reservations: [otherPropertyReservation],
      units: { prop_1: [unit], prop_2: [otherPropertyUnit] },
    });
    try {
      render(<Harness initial="/w/acme/property/prop_2/stays" />);

      expect(await screen.findByText("Other Guest")).toBeInTheDocument();
      expect(screen.queryByRole("menuitem", { name: "Add stay" })).toBeNull();
      const stayRow = screen.getByLabelText("New stay");
      expect(within(stayRow).getByRole("combobox", { name: /^Property\b/ })).toHaveValue("Beach House");
      expect(within(stayRow).getByRole("combobox", { name: /^Unit\b/ })).toHaveValue("Cabana");
      fireEvent.change(within(stayRow).getByLabelText(/^Guest name\b/), { target: { value: "Beach Guest" } });
      fireEvent.change(within(stayRow).getByLabelText(/^Check-in\b/), { target: { value: "2026-04-21" } });
      fireEvent.change(within(stayRow).getByLabelText(/^Check-out\b/), { target: { value: "2026-04-23" } });
      fireEvent.click(within(stayRow).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        const createRequest = fake.requests.find((request) => request.method === "POST" && request.path === "/w/acme/api/v1/stays");
        expect(createRequest?.body).toMatchObject({ property_id: "prop_2", unit_id: "unit_3" });
      });

      fireEvent.click(await screen.findByRole("button", { name: "Import iCal" }));
      const icalDialog = await screen.findByRole("dialog", { name: "Import iCal" });
      expect(within(icalDialog).getByRole("combobox", { name: /^Property\b/ })).toHaveValue("Beach House");
      expect(within(icalDialog).getByRole("combobox", { name: /^Unit\b/ })).toHaveValue("Cabana");
      fireEvent.change(within(icalDialog).getByLabelText(/^Feed URL\b/), {
        target: { value: "https://ical.vrbo.com/property.ics" },
      });
      fireEvent.click(within(icalDialog).getByRole("button", { name: "Add feed" }));

      await waitFor(() => {
        const feedRequest = fake.requests.find((request) => request.method === "POST" && request.path === "/w/acme/api/v1/stays/ical-feeds");
        expect(feedRequest?.body).toMatchObject({ property_id: "prop_2", unit_id: "unit_3" });
      });
    } finally {
      fake.restore();
    }
  });

  it("renders stays in the shared infinite agenda instead of the old static April grid", async () => {
    const fake = installFetch({ reservations: [existingReservation] });
    try {
      render(<Harness />);

      expect(await screen.findByText("Scroll up for past weeks")).toBeInTheDocument();
      expect(screen.getByText("Keep scrolling for more")).toBeInTheDocument();
      expect(screen.getByLabelText("Stays calendar legend")).toBeInTheDocument();
      expect(screen.queryByText("April 2026, calendar")).not.toBeInTheDocument();
      expect(document.querySelector(".cal-wide")).toBeNull();
      expect(document.querySelector(".schedule__monthbar")).not.toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("labels stays, turnovers, and approved leave in the agenda", async () => {
    const todayIso = isoOffset(0);
    const tomorrowIso = isoOffset(1);
    const fake = installFetch({
      reservations: [
        {
          ...existingReservation,
          id: "res_current",
          check_in: `${todayIso}T16:00:00Z`,
          check_out: `${tomorrowIso}T10:00:00Z`,
          guest_name: "May Guest",
        },
      ],
      leaves: [
        {
          id: "leave_1",
          user_id: "usr_leave",
          starts_on: todayIso,
          ends_on: todayIso,
          category: "vacation",
          note_md: null,
          approved_at: "2026-05-01T10:00:00Z",
        },
      ],
      employees: [
        {
          id: "usr_leave",
          name: "Lina Leave",
          avatar_initials: "LL",
          role: "Housekeeper",
          email: "lina@example.com",
          phone: null,
          status: "active",
          employment_type: "employee",
          weekly_capacity_hours: null,
          hourly_rate_cents: null,
          overtime_rate_cents: null,
          locale: "en-US",
          timezone: "Europe/Paris",
          hired_on: null,
          notes_md: null,
        },
      ],
    });
    try {
      render(<Harness />);

      expect(await screen.findByText("Scroll up for past weeks")).toBeInTheDocument();
      expect(screen.getAllByText("May Guest").length).toBeGreaterThan(0);
      expect(screen.getAllByText("Turnover").length).toBeGreaterThan(0);
      expect(screen.getByText("LL")).toBeInTheDocument();
      expect(screen.getByText(/Lina Leave · vacation/)).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("selects draft stay dates from the shared planner without saving", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      const createRow = await screen.findByLabelText("New stay");
      expect(await screen.findByText("Scroll up for past weeks")).toBeInTheDocument();
      const dayCell = (iso: string) => document.querySelector(`[data-schedule-iso="${iso}"]`) as HTMLElement;
      const scheduleIsos = Array.from(document.querySelectorAll<HTMLElement>("[data-schedule-iso]"))
        .slice(0, 2)
        .map((cell) => cell.dataset.scheduleIso ?? "");
      const [startIso, endIso] = scheduleIsos;
      if (!startIso || !endIso) throw new Error("Expected at least two scheduler cells.");
      const checkoutIso = nextIso(endIso);

      fireEvent.pointerDown(dayCell(startIso), { button: 0 });
      fireEvent.pointerEnter(dayCell(endIso), { buttons: 1 });

      expect(within(createRow).getByLabelText(/^Check-in\b/)).toHaveValue(startIso);
      expect(within(createRow).getByLabelText(/^Check-out\b/)).toHaveValue(checkoutIso);
      expect(within(dayCell(startIso)).getByLabelText("Draft stay, Unsaved manual stay")).toBeInTheDocument();
      expect(fake.requests.some((request) => request.method === "POST" && request.path === "/w/acme/api/v1/stays")).toBe(false);
    } finally {
      fake.restore();
    }
  });

  it("does not create a stay when a date-only create row loses focus", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      const createRow = await screen.findByLabelText("New stay");
      const checkIn = within(createRow).getByLabelText(/^Check-in\b/);
      fireEvent.change(checkIn, { target: { value: "2026-04-21" } });
      fireEvent.blur(checkIn, { relatedTarget: document.body });

      expect(fake.requests.some((request) => request.method === "POST" && request.path === "/w/acme/api/v1/stays")).toBe(false);
      expect(within(createRow).queryByText("Enter check-in and check-out dates.")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("renders the agenda when cached stay payload is missing leaves", async () => {
    const fake = installFetch({ reservations: [existingReservation] });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queryClient.setQueryData(qk.stays(), { stays: [], closures: [] });
    try {
      render(<Harness queryClient={queryClient} />);

      expect(await screen.findByText("Scroll up for past weeks")).toBeInTheDocument();
      expect(document.querySelector(".stays-day__event--leave")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("renders the agenda when the leaves fetch has no data array", async () => {
    const fake = installFetch({ reservations: [existingReservation], leavesResponse: {} });
    try {
      render(<Harness />);

      expect(await screen.findByText("Scroll up for past weeks")).toBeInTheDocument();
      expect(document.querySelector(".stays-day__event--leave")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("creates a manual stay and renders overlap visibility", async () => {
    const fake = installFetch({ reservations: [existingReservation] });
    try {
      render(<Harness />);

      expect(await screen.findByText("Ada Existing")).toBeInTheDocument();
      expect(screen.queryByRole("menuitem", { name: "Add stay" })).toBeNull();

      const createRow = screen.getByLabelText("New stay");
      fireEvent.change(within(createRow).getByLabelText(/^Guest name\b/), { target: { value: "Bea Guest" } });
      fireEvent.change(within(createRow).getByLabelText(/^Guests\b/), { target: { value: "3" } });
      fireEvent.change(within(createRow).getByLabelText(/^Check-in\b/), { target: { value: "2026-04-19" } });
      fireEvent.change(within(createRow).getByLabelText(/^Check-out\b/), { target: { value: "2026-04-21" } });

      expect(within(createRow).getByText(/Overlaps Ada Existing/)).toBeInTheDocument();

      fireEvent.change(within(createRow).getByLabelText(/^Check-in\b/), { target: { value: "2026-04-21" } });
      fireEvent.change(within(createRow).getByLabelText(/^Check-out\b/), { target: { value: "2026-04-23" } });
      const reservationFetchesBeforeCreate = fake.requests.filter(
        (request) => request.path === "/w/acme/api/v1/stays/reservations?limit=500",
      ).length;
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));

      await waitFor(() => expect(screen.getByText("Bea Guest")).toBeInTheDocument());
      const createRequest = fake.requests.find((request) => request.method === "POST" && request.path === "/w/acme/api/v1/stays");
      expect(createRequest?.body).toMatchObject({
        property_id: "prop_1",
        unit_id: "unit_1",
        check_in_at: "2026-04-21T16:00:00Z",
        check_out_at: "2026-04-23T10:00:00Z",
        guest_name: "Bea Guest",
        guest_count: 3,
        source: "manual",
      });
      await waitFor(() => {
        expect(
          fake.requests.filter((request) => request.path === "/w/acme/api/v1/stays/reservations?limit=500").length,
        ).toBeGreaterThan(reservationFetchesBeforeCreate);
      });
    } finally {
      fake.restore();
    }
  });

  it("edits a saved manual stay inline through the API and keeps imported rows read-only", async () => {
    const importedReservation = {
      ...existingReservation,
      id: "res_imported",
      external_uid: "airbnb-1",
      ical_feed_id: "feed_1",
      guest_name: "Imported Guest",
      source: "airbnb",
    };
    const fake = installFetch({ reservations: [existingReservation, importedReservation] });
    try {
      render(<Harness />);

      const manualRow = await screen.findByLabelText("Ada Existing stay from Sat 18 Apr to Mon 20 Apr");
      const importedRow = screen.getByLabelText("Imported Guest stay from Sat 18 Apr to Mon 20 Apr");
      expect(within(manualRow).getByRole("button", { name: "Edit" })).toBeEnabled();
      expect(within(importedRow).getByRole("button", { name: "Edit" })).toBeDisabled();
      expect(within(importedRow).getByText("Imported reservation. Edit it in the source calendar.")).toBeInTheDocument();

      fireEvent.click(within(manualRow).getByRole("button", { name: "Edit" }));
      expect(within(manualRow).getByLabelText(/^Guest name\b/)).toHaveValue("Ada Existing");
      expect(within(manualRow).getByLabelText(/^Unit\b/)).toHaveValue("Garden Suite");
      expect(within(manualRow).getByLabelText(/^Check-in\b/)).toHaveValue("2026-04-18");
      expect(within(manualRow).getByLabelText(/^Check-out\b/)).toHaveValue("2026-04-20");
      expect(within(manualRow).getByLabelText(/^Guests\b/)).toHaveValue("2");
      expect(within(manualRow).getByLabelText(/^Status\b/)).toHaveValue("confirmed");

      fireEvent.change(within(manualRow).getByLabelText(/^Guest name\b/), { target: { value: "Ada Updated" } });
      fireEvent.change(within(manualRow).getByLabelText(/^Guests\b/), { target: { value: "4" } });
      fireEvent.change(within(manualRow).getByLabelText(/^Check-out\b/), { target: { value: "2026-04-22" } });
      fireEvent.click(within(manualRow).getByRole("button", { name: "Save" }));

      await waitFor(() => expect(screen.getByText("Ada Updated")).toBeInTheDocument());
      const updateRequest = fake.requests.find((request) => (
        request.method === "PATCH" && request.path === "/w/acme/api/v1/stays/res_existing"
      ));
      expect(updateRequest?.body).toMatchObject({
        property_id: "prop_1",
        unit_id: "unit_1",
        check_in_at: "2026-04-18T16:00:00Z",
        check_out_at: "2026-04-22T10:00:00Z",
        guest_name: "Ada Updated",
        guest_count: 4,
        status: "confirmed",
      });
    } finally {
      fake.restore();
    }
  });

  it("does not turn hidden guest placeholders into editable manual stay guest names", async () => {
    const fake = installFetch({
      properties: [property, secondProperty],
      reservations: [existingReservation],
      units: { prop_1: [unit], prop_2: [otherPropertyUnit] },
      memberships: {
        prop_1: { membershipRole: "managed_workspace", shareGuestIdentity: false },
        prop_2: { membershipRole: "managed_workspace", shareGuestIdentity: true },
      },
    });
    try {
      render(<Harness />);

      const manualRow = await screen.findByLabelText("Hidden guest stay from Sat 18 Apr to Mon 20 Apr");
      fireEvent.click(within(manualRow).getByRole("button", { name: "Edit" }));
      expect(within(manualRow).getByLabelText(/^Guest name\b/)).toBeDisabled();
      expect(within(manualRow).getByLabelText(/^Guest name\b/)).toHaveValue("");

      await chooseSearchableOption(manualRow, /^Property\b/, "Beach House");
      expect(within(manualRow).getByLabelText(/^Guest name\b/)).toBeEnabled();
      expect(within(manualRow).getByLabelText(/^Guest name\b/)).toHaveValue("");
      fireEvent.click(within(manualRow).getByRole("button", { name: "Save" }));

      expect(within(manualRow).getByText("Guest name is required for this property.")).toBeInTheDocument();
      expect(fake.requests.some((request) => request.method === "PATCH")).toBe(false);

      fireEvent.change(within(manualRow).getByLabelText(/^Guest name\b/), { target: { value: "Visible Guest" } });
      fireEvent.click(within(manualRow).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        const updateRequest = fake.requests.find((request) => request.method === "PATCH");
        expect(updateRequest?.body).toMatchObject({ guest_name: "Visible Guest" });
      });
    } finally {
      fake.restore();
    }
  });

  it("cancels saved manual stay edits without calling the update endpoint", async () => {
    const fake = installFetch({ reservations: [existingReservation] });
    try {
      render(<Harness />);

      const manualRow = await screen.findByLabelText("Ada Existing stay from Sat 18 Apr to Mon 20 Apr");
      fireEvent.click(within(manualRow).getByRole("button", { name: "Edit" }));
      fireEvent.change(within(manualRow).getByLabelText(/^Guest name\b/), { target: { value: "Discard Me" } });
      fireEvent.click(within(manualRow).getByRole("button", { name: "Cancel" }));

      expect(within(manualRow).getByText("Ada Existing")).toBeInTheDocument();
      expect(within(manualRow).queryByText("Discard Me")).toBeNull();
      expect(fake.requests.some((request) => request.method === "PATCH")).toBe(false);
    } finally {
      fake.restore();
    }
  });

  it("keeps saved manual stay API errors local to the edited row", async () => {
    const fake = installFetch({
      reservations: [existingReservation],
      updateStay: () => ({
        status: 422,
        body: {
          type: "validation",
          title: "Validation failed",
          user_message: "Server rejected the updated stay.",
        },
      }),
    });
    try {
      render(<Harness />);

      const manualRow = await screen.findByLabelText("Ada Existing stay from Sat 18 Apr to Mon 20 Apr");
      fireEvent.click(within(manualRow).getByRole("button", { name: "Edit" }));
      fireEvent.change(within(manualRow).getByLabelText(/^Check-out\b/), { target: { value: "2026-04-22" } });
      fireEvent.click(within(manualRow).getByRole("button", { name: "Save" }));

      expect(await within(manualRow).findByText("Server rejected the updated stay.")).toBeInTheDocument();
      expect(within(manualRow).getByLabelText(/^Check-out\b/)).toHaveValue("2026-04-22");

      fireEvent.change(within(manualRow).getByLabelText(/^Guest name\b/), { target: { value: "" } });
      fireEvent.click(within(manualRow).getByRole("button", { name: "Save" }));

      expect(within(manualRow).getByText("Guest name is required for this property.")).toBeInTheDocument();
      expect(within(manualRow).queryByText("Server rejected the updated stay.")).toBeNull();
      expect(fake.requests.filter((request) => request.method === "PATCH")).toHaveLength(1);
    } finally {
      fake.restore();
    }
  });

  it("keeps new-row stay validation and server errors local to the inline row", async () => {
    const fake = installFetch({
      createStay: () => ({
        status: 422,
        body: {
          type: "validation",
          title: "Validation failed",
          user_message: "Server rejected these stay dates.",
        },
      }),
    });
    try {
      render(<Harness />);

      const createRow = await screen.findByLabelText("New stay");
      fireEvent.change(within(createRow).getByLabelText(/^Guest name\b/), { target: { value: "Bea Guest" } });
      fireEvent.change(within(createRow).getByLabelText(/^Check-in\b/), { target: { value: "2026-04-21" } });
      fireEvent.change(within(createRow).getByLabelText(/^Check-out\b/), { target: { value: "2026-04-23" } });
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));

      expect(await within(createRow).findByText("Server rejected these stay dates.")).toBeInTheDocument();
      expect(within(createRow).getByLabelText(/^Guest name\b/)).toHaveValue("Bea Guest");
      expect(within(createRow).getByLabelText(/^Check-in\b/)).toHaveValue("2026-04-21");
      expect(within(createRow).getByLabelText(/^Check-out\b/)).toHaveValue("2026-04-23");

      fireEvent.change(within(createRow).getByLabelText(/^Guest name\b/), { target: { value: "" } });
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));

      expect(within(createRow).getByText("Guest name is required for this property.")).toBeInTheDocument();
      expect(within(createRow).queryByText("Server rejected these stay dates.")).toBeNull();
      expect(fake.requests.filter((request) => request.method === "POST" && request.path === "/w/acme/api/v1/stays")).toHaveLength(1);
    } finally {
      fake.restore();
    }
  });

  it("checks overlap by unit and hides guest identity for non-sharing workspaces", async () => {
    const fake = installFetch({
      reservations: [{ ...existingReservation, unit_id: "unit_2" }],
      units: [unit, secondUnit],
      membershipRole: "managed_workspace",
      shareGuestIdentity: false,
    });
    try {
      render(<Harness />);

      expect(await screen.findByText("Hidden guest")).toBeInTheDocument();
      expect(screen.queryByText("Ada Existing")).not.toBeInTheDocument();

      const createRow = screen.getByLabelText("New stay");
      expect(within(createRow).getByLabelText(/^Guest name\b/)).toBeDisabled();

      fireEvent.change(within(createRow).getByLabelText(/^Check-in\b/), { target: { value: "2026-04-19" } });
      fireEvent.change(within(createRow).getByLabelText(/^Check-out\b/), { target: { value: "2026-04-21" } });
      expect(within(createRow).queryByText(/Overlaps/)).not.toBeInTheDocument();

      await chooseSearchableOption(createRow, /^Unit\b/, "Roof Studio");
      expect(within(createRow).getByText(/Overlaps Hidden guest/)).toBeInTheDocument();

      fireEvent.change(within(createRow).getByLabelText(/^Check-in\b/), { target: { value: "2026-04-21" } });
      fireEvent.change(within(createRow).getByLabelText(/^Check-out\b/), { target: { value: "2026-04-23" } });
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));

      await waitFor(() => expect(fake.requests.some((request) => request.method === "POST")).toBe(true));
      const createRequest = fake.requests.find((request) => request.method === "POST" && request.path === "/w/acme/api/v1/stays");
      expect(createRequest?.body).toMatchObject({ unit_id: "unit_2", guest_name: null });
    } finally {
      fake.restore();
    }
  });

  it("does not leak a stale draft guest name after switching to a non-sharing property", async () => {
    const todayIso = isoOffset(0);
    const checkoutIso = isoOffset(2);
    const fake = installFetch({
      properties: [property, secondProperty],
      units: { prop_1: [unit], prop_2: [otherPropertyUnit] },
      memberships: {
        prop_1: { membershipRole: "owner_workspace", shareGuestIdentity: false },
        prop_2: { membershipRole: "managed_workspace", shareGuestIdentity: false },
      },
    });
    try {
      render(<Harness />);

      const createRow = await screen.findByLabelText("New stay");
      fireEvent.change(within(createRow).getByLabelText(/^Guest name\b/), { target: { value: "Private Guest" } });
      await chooseSearchableOption(createRow, /^Property\b/, "Beach House");
      fireEvent.change(within(createRow).getByLabelText(/^Check-in\b/), { target: { value: todayIso } });
      fireEvent.change(within(createRow).getByLabelText(/^Check-out\b/), { target: { value: checkoutIso } });

      expect(within(createRow).getByRole("combobox", { name: /^Property\b/ })).toHaveValue("Beach House");
      expect(within(createRow).getByLabelText(/^Guest name\b/)).toBeDisabled();
      expect(within(createRow).getByLabelText(/^Guest name\b/)).toHaveValue("");
      const dayCell = screen.getByLabelText(new RegExp(todayIso));
      expect(within(dayCell).getByLabelText("Draft stay, Unsaved manual stay")).toBeInTheDocument();
      expect(screen.queryByText("Private Guest")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("adds an iCal feed with provider and unit mapping", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "Import iCal" }));

      const dialog = await screen.findByRole("dialog", { name: "Import iCal" });
      fireEvent.change(within(dialog).getByLabelText(/^Provider\b/), { target: { value: "gcal" } });
      fireEvent.change(within(dialog).getByLabelText(/^Feed URL\b/), {
        target: { value: "https://calendar.google.com/calendar/ical/abc.ics" },
      });
      const feedFetchesBeforeCreate = fake.requests.filter(
        (request) => request.path === "/w/acme/api/v1/stays/ical-feeds",
      ).length;
      fireEvent.click(within(dialog).getByRole("button", { name: "Add feed" }));

      expect(await within(dialog).findByText(/Google Calendar parsed successfully/)).toBeInTheDocument();
      const createRequest = fake.requests.find((request) => request.method === "POST" && request.path === "/w/acme/api/v1/stays/ical-feeds");
      expect(createRequest?.body).toMatchObject({
        property_id: "prop_1",
        unit_id: "unit_1",
        provider_override: "gcal",
        url: "https://calendar.google.com/calendar/ical/abc.ics",
      });
      await waitFor(() => {
        expect(
          fake.requests.filter((request) => request.path === "/w/acme/api/v1/stays/ical-feeds").length,
        ).toBeGreaterThan(feedFetchesBeforeCreate);
      });
    } finally {
      fake.restore();
    }
  });

  it("surfaces malformed, duplicate, and server iCal validation failures", async () => {
    const fake = installFetch({
      feeds: [
        {
          id: "feed_existing",
          property_id: "prop_1",
          unit_id: "unit_1",
          provider: "airbnb",
          provider_override: "airbnb",
          url_preview: "https://calendar.airbnb.com",
          enabled: true,
          poll_cadence: "*/15 * * * *",
          last_polled_at: null,
          last_error: null,
          created_at: "2026-04-01T00:00:00Z",
        },
      ],
      createFeed: (body) => {
        const url = typeof body === "object" && body !== null && "url" in body ? String(body.url) : "";
        if (url.includes("duplicate")) {
          return {
            status: 409,
            body: {
              type: "https://crewday.dev/errors/conflict",
              title: "Conflict",
              detail: "feed already exists",
              error: "ical_feed_duplicate",
            },
          };
        }
        return {
          status: 422,
          body: {
            type: "https://crewday.dev/errors/validation",
            title: "Validation failed",
            detail: "ical_url_private_address",
            error: "ical_url_private_address",
          },
        };
      },
    });
    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "Import iCal" }));
      const dialog = await screen.findByRole("dialog", { name: "Import iCal" });

      fireEvent.change(within(dialog).getByLabelText(/^Feed URL\b/), { target: { value: "not a url" } });
      fireEvent.click(within(dialog).getByRole("button", { name: "Add feed" }));
      expect(await within(dialog).findByText("Enter a valid https:// iCal feed URL.")).toBeInTheDocument();

      fireEvent.change(within(dialog).getByLabelText(/^Feed URL\b/), {
        target: { value: "https://calendar.airbnb.com/calendar/ical/duplicate.ics" },
      });
      expect(within(dialog).getByText("A feed from this host is already mapped to that unit.")).toBeInTheDocument();
      fireEvent.click(within(dialog).getByRole("button", { name: "Add feed" }));
      expect(await within(dialog).findByText("This iCal feed already exists for the selected property or unit.")).toBeInTheDocument();

      fireEvent.change(within(dialog).getByLabelText(/^Feed URL\b/), {
        target: { value: "https://bad.example.test/feed.ics" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Add feed" }));
      expect(await within(dialog).findByText("That iCal URL resolves to a private address and was blocked.")).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });
});
