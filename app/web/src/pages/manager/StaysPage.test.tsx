import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
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

function installFetch(options: {
  reservations?: unknown[];
  units?: unknown[];
  feeds?: unknown[];
  leaves?: unknown[];
  leavesResponse?: unknown;
  employees?: unknown[];
  membershipRole?: "owner_workspace" | "managed_workspace" | "observer_workspace";
  shareGuestIdentity?: boolean;
  createStay?: (body: unknown) => { status?: number; body?: unknown };
  createFeed?: (body: unknown) => { status?: number; body?: unknown };
} = {}) {
  const reservations = [...(options.reservations ?? [])];
  const feeds = [...(options.feeds ?? [])];
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
      path: "/w/acme/api/v1/user_leaves?approved=true&limit=500",
      respond: { body: options.leavesResponse ?? { data: options.leaves ?? [] } },
    },
    { path: "/w/acme/api/v1/properties", respond: { body: [property] } },
    { path: "/w/acme/api/v1/employees", respond: { body: options.employees ?? [] } },
    { path: "/w/acme/api/v1/stays/ical-feeds", respond: { body: feeds } },
    {
      path: "/w/acme/api/v1/properties/prop_1/units?limit=100",
      respond: { body: { data: options.units ?? [unit] } },
    },
    {
      path: "/w/acme/api/v1/properties/prop_1/share",
      respond: {
        body: {
          data: [
            {
              property_id: "prop_1",
              workspace_id: "ws_1",
              label: "Acme",
              membership_role: options.membershipRole ?? "owner_workspace",
              status: "active",
              share_guest_identity: options.shareGuestIdentity ?? false,
              created_at: "2026-04-01T00:00:00Z",
            },
          ],
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

function Harness({ queryClient }: { queryClient?: QueryClient } = {}) {
  const qc = queryClient ?? new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WorkspaceProvider>
          <StaysPage />
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
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
  it("renders stays in the shared infinite agenda instead of the old static April grid", async () => {
    const fake = installFetch({ reservations: [existingReservation] });
    try {
      render(<Harness />);

      expect(await screen.findByText("Scroll up for past weeks")).toBeInTheDocument();
      expect(screen.getByText("Keep scrolling for more")).toBeInTheDocument();
      expect(screen.getByLabelText("Stays calendar legend")).toBeInTheDocument();
      expect(screen.queryByText("April 2026 — calendar")).not.toBeInTheDocument();
      expect(document.querySelector(".cal-wide")).toBeNull();
      expect(document.querySelector(".schedule__monthbar")).not.toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("labels stays, turnovers, and approved leave in the agenda", async () => {
    const fake = installFetch({
      reservations: [
        {
          ...existingReservation,
          id: "res_current",
          check_in: "2026-05-08T16:00:00Z",
          check_out: "2026-05-09T10:00:00Z",
          guest_name: "May Guest",
        },
      ],
      leaves: [
        {
          id: "leave_1",
          user_id: "usr_leave",
          starts_on: "2026-05-09",
          ends_on: "2026-05-09",
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
      expect(screen.getAllByText("May Guest").length).toBeGreaterThan(1);
      expect(screen.getAllByText("Turnover").length).toBeGreaterThan(1);
      expect(screen.getByText("LL")).toBeInTheDocument();
      expect(screen.getByText(/Lina Leave · vacation/)).toBeInTheDocument();
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

      fireEvent.click(screen.getByRole("button", { name: "More actions" }));
      fireEvent.click(screen.getByRole("menuitem", { name: "Add stay" }));

      const dialog = await screen.findByRole("dialog", { name: "Add stay" });
      fireEvent.change(within(dialog).getByLabelText("Guest name"), { target: { value: "Bea Guest" } });
      fireEvent.change(within(dialog).getByLabelText("Guests"), { target: { value: "3" } });
      fireEvent.change(within(dialog).getByLabelText("Check-in"), { target: { value: "2026-04-19" } });
      fireEvent.change(within(dialog).getByLabelText("Check-out"), { target: { value: "2026-04-21" } });

      expect(within(dialog).getByText(/Overlaps Ada Existing/)).toBeInTheDocument();

      fireEvent.change(within(dialog).getByLabelText("Check-in"), { target: { value: "2026-04-21" } });
      fireEvent.change(within(dialog).getByLabelText("Check-out"), { target: { value: "2026-04-23" } });
      const reservationFetchesBeforeCreate = fake.requests.filter(
        (request) => request.path === "/w/acme/api/v1/stays/reservations?limit=500",
      ).length;
      fireEvent.click(within(dialog).getByRole("button", { name: "Create stay" }));

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

      fireEvent.click(screen.getByRole("button", { name: "More actions" }));
      fireEvent.click(screen.getByRole("menuitem", { name: "Add stay" }));

      const dialog = await screen.findByRole("dialog", { name: "Add stay" });
      expect(within(dialog).getByLabelText("Guest name")).toBeDisabled();

      fireEvent.change(within(dialog).getByLabelText("Check-in"), { target: { value: "2026-04-19" } });
      fireEvent.change(within(dialog).getByLabelText("Check-out"), { target: { value: "2026-04-21" } });
      expect(within(dialog).queryByText(/Overlaps/)).not.toBeInTheDocument();

      fireEvent.change(within(dialog).getByLabelText("Unit"), { target: { value: "unit_2" } });
      expect(within(dialog).getByText(/Overlaps Hidden guest/)).toBeInTheDocument();

      fireEvent.change(within(dialog).getByLabelText("Check-in"), { target: { value: "2026-04-21" } });
      fireEvent.change(within(dialog).getByLabelText("Check-out"), { target: { value: "2026-04-23" } });
      fireEvent.click(within(dialog).getByRole("button", { name: "Create stay" }));

      await waitFor(() => expect(fake.requests.some((request) => request.method === "POST")).toBe(true));
      const createRequest = fake.requests.find((request) => request.method === "POST" && request.path === "/w/acme/api/v1/stays");
      expect(createRequest?.body).toMatchObject({ unit_id: "unit_2", guest_name: null });
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
      fireEvent.change(within(dialog).getByLabelText("Provider"), { target: { value: "gcal" } });
      fireEvent.change(within(dialog).getByLabelText("Feed URL"), {
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

      fireEvent.change(within(dialog).getByLabelText("Feed URL"), { target: { value: "not a url" } });
      fireEvent.click(within(dialog).getByRole("button", { name: "Add feed" }));
      expect(await within(dialog).findByText("Enter a valid https:// iCal feed URL.")).toBeInTheDocument();

      fireEvent.change(within(dialog).getByLabelText("Feed URL"), {
        target: { value: "https://calendar.airbnb.com/calendar/ical/duplicate.ics" },
      });
      expect(within(dialog).getByText("A feed from this host is already mapped to that unit.")).toBeInTheDocument();
      fireEvent.click(within(dialog).getByRole("button", { name: "Add feed" }));
      expect(await within(dialog).findByText("This iCal feed already exists for the selected property or unit.")).toBeInTheDocument();

      fireEvent.change(within(dialog).getByLabelText("Feed URL"), {
        target: { value: "https://bad.example.test/feed.ics" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Add feed" }));
      expect(await within(dialog).findByText("That iCal URL resolves to a private address and was blocked.")).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });
});
