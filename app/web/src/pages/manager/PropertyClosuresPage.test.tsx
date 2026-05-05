import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import PropertyClosuresPage from "./PropertyClosuresPage";
import { installFetchRouteHandlers } from "@/test/helpers";

function installFetch({ failClosures = false }: { failClosures?: boolean } = {}) {
  // code-health: ignore[nloc] Route fixtures stay local; shared fetch mechanics live in test/helpers.
  const env = installFetchRouteHandlers([
    {
      path: "/w/acme/api/v1/property_closures",
      method: "POST",
      respond: {
        status: 201,
        body: {
          id: "closure_new",
          property_id: "prop_1",
          unit_id: null,
          starts_at: "2026-04-16T00:00:00Z",
          ends_at: "2026-04-18T00:00:00Z",
          reason: "seasonal",
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
      respond: {
        body: {
          id: "closure_1",
          property_id: "prop_1",
          unit_id: null,
          starts_at: "2026-04-11T00:00:00Z",
          ends_at: "2026-04-12T00:00:00Z",
          reason: "owner_stay",
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
        body: [{
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
        body: {
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
            data: [{
            id: "closure_1",
            property_id: "prop_1",
            starts_at: "2026-04-10T00:00:00Z",
            ends_at: "2026-04-13T00:00:00Z",
            reason: "renovation",
          },
          {
            id: "closure_2",
            property_id: "prop_1",
            starts_at: "2026-04-20T00:00:00Z",
            ends_at: "2026-04-22T00:00:00Z",
            reason: "ical_unavailable",
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
      <MemoryRouter initialEntries={["/property/prop_1/closures"]}>
        <WorkspaceProvider>
          <Routes>
            <Route path="/property/:pid/closures" element={<PropertyClosuresPage />} />
          </Routes>
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function hiddenButton(name: string): HTMLElement {
  const button = screen.getAllByRole("button", { name, hidden: true })[0];
  if (!button) throw new Error(`Missing button: ${name}`);
  return button;
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
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
      expect(screen.getByRole("link", { name: /Back to property/ })).toHaveAttribute("href", "/property/prop_1");
      expect(screen.getByRole("button", { name: "+ Add closure" })).toBeInTheDocument();
      expect(screen.getByText("10 Apr → 12 Apr")).toBeInTheDocument();
      expect(screen.getAllByText("renovation").length).toBeGreaterThan(0);
      expect(screen.getByText("Airbnb / VRBO")).toBeInTheDocument();
      expect(screen.getByText("Read-only — edit in Airbnb / VRBO")).toBeInTheDocument();
      expect(screen.getByText("Calendar view")).toBeInTheDocument();
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

  it("posts a new manual closure through the production endpoint", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "+ Add closure" }));
      fireEvent.change(screen.getByLabelText("Start"), { target: { value: "2026-04-16" } });
      fireEvent.change(screen.getByLabelText("End"), { target: { value: "2026-04-18" } });
      fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "seasonal" } });
      fireEvent.click(hiddenButton("Save"));

      await waitFor(() => {
        expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/property_closures" && request.init?.method === "POST")).toBe(true);
      });
      const request = fake.requests.find((entry) => entry.url === "/w/acme/api/v1/property_closures" && entry.init?.method === "POST");
      expect(JSON.parse(String(request?.init?.body))).toEqual({
        property_id: "prop_1",
        unit_id: null,
        starts_at: "2026-04-16T00:00:00Z",
        ends_at: "2026-04-19T00:00:00.000Z",
        reason: "seasonal",
        source_ical_feed_id: null,
      });
    } finally {
      fake.restore();
    }
  });

  it("patches and deletes an existing manual closure", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
      fireEvent.change(screen.getByLabelText("Start"), { target: { value: "2026-04-11" } });
      fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "owner_stay" } });
      fireEvent.click(hiddenButton("Save"));

      await waitFor(() => {
        expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/property_closures/closure_1" && request.init?.method === "PATCH")).toBe(true);
      });
      const patch = fake.requests.find((entry) => entry.url === "/w/acme/api/v1/property_closures/closure_1" && entry.init?.method === "PATCH");
      expect(JSON.parse(String(patch?.init?.body))).toEqual({
        unit_id: null,
        starts_at: "2026-04-11T00:00:00Z",
        ends_at: "2026-04-13T00:00:00.000Z",
        reason: "owner_stay",
        source_ical_feed_id: null,
      });

      fireEvent.click(screen.getByRole("button", { name: "Edit" }));
      fireEvent.click(hiddenButton("Delete"));

      await waitFor(() => {
        expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/property_closures/closure_1" && request.init?.method === "DELETE")).toBe(true);
      });
    } finally {
      fake.restore();
    }
  });
});
