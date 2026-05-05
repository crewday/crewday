import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { jsonResponse } from "@/test/helpers";
import PropertyDetailPage from "./PropertyDetailPage";

function installFetch() {
  const calls: { url: string; method: string }[] = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const method = init?.method ?? "GET";
    calls.push({ url: resolved, method });
    if (resolved === "/api/v1/auth/me") {
      return jsonResponse({
        user_id: "usr_1",
        display_name: "Mina",
        email: "mina@example.com",
        available_workspaces: [],
        current_workspace_id: "ws_owner",
      });
    }
    if (resolved === "/api/v1/me/workspaces") {
      return jsonResponse([
        {
          workspace_id: "ws_owner",
          slug: "acme",
          name: "Acme",
          current_role: "manager",
          last_seen_at: null,
          settings_override: {},
        },
      ]);
    }
    if (resolved === "/w/acme/api/v1/properties") {
      return jsonResponse([
        {
          id: "prop_1",
          name: "Villa Rosa",
          city: "Porto",
          timezone: "Europe/Lisbon",
          color: "moss",
          kind: "str",
          areas: ["Kitchen", "Terrace"],
          evidence_policy: "inherit",
          country: "PT",
          locale: "pt-PT",
          settings_override: {},
          client_org_id: "org_1",
          owner_user_id: null,
        },
      ]);
    }
    if (resolved === "/w/acme/api/v1/properties/prop_1") {
      return jsonResponse({
        id: "prop_1",
        name: "Villa Rosa",
        kind: "str",
        address: {
          city: "Porto",
          country: "PT",
        },
        timezone: "Europe/Lisbon",
        country: "PT",
        locale: "pt-PT",
        client_org_id: "org_1",
        owner_user_id: null,
        settings_override: {},
      });
    }
    if (resolved === "/w/acme/api/v1/tasks?property_id=prop_1&limit=100") {
      return jsonResponse({
        data: [
          {
            id: "task_1",
            title: "Turn over suite",
            status: "scheduled",
            scheduled_start: "2026-05-06T09:00:00Z",
            property_id: "prop_1",
            area: "Kitchen",
            assigned_user_id: "emp_1",
          },
        ],
        next_cursor: null,
        has_more: false,
      });
    }
    if (resolved === "/w/acme/api/v1/stays/reservations?property_id=prop_1&limit=100") {
      return jsonResponse({
        data: [
          {
            id: "res_1",
            property_id: "prop_1",
            check_in: "2026-05-08T12:00:00Z",
            check_out: "2026-05-10T10:00:00Z",
            guest_name: "Guest",
            guest_count: 2,
            status: "scheduled",
            source: "api",
          },
        ],
        next_cursor: null,
        has_more: false,
      });
    }
    if (resolved === "/w/acme/api/v1/properties/prop_1/share") {
      return jsonResponse({
        data: [
          {
            property_id: "prop_1",
            workspace_id: "ws_owner",
            label: "Acme",
            membership_role: "owner_workspace",
            status: "active",
            share_guest_identity: true,
            created_at: "2026-04-29T00:00:00Z",
          },
        ],
        next_cursor: null,
        has_more: false,
      });
    }
    if (resolved === "/w/acme/api/v1/billing/organizations") {
      return jsonResponse({
        data: [
          {
            id: "org_1",
            workspace_id: "ws_owner",
            kind: "client",
            display_name: "Luxe Guests",
            tax_id: "PT123",
            default_currency: "EUR",
            notes_md: null,
          },
        ],
      });
    }
    if (resolved === "/w/acme/api/v1/settings") {
      return jsonResponse({
        meta: {
          name: "Acme",
          timezone: "Europe/Lisbon",
          currency: "EUR",
          country: "PT",
          default_locale: "pt-PT",
        },
        defaults: {
          evidence_policy: "inherit",
        },
        policy: {
          approvals: { always_gated: [], configurable: [] },
          danger_zone: [],
        },
      });
    }
    if (resolved === "/w/acme/api/v1/settings/catalog") {
      return jsonResponse([
        {
          key: "evidence_policy",
          label: "Evidence policy",
          type: "enum",
          catalog_default: "inherit",
          enum_values: ["inherit", "required"],
          override_scope: "WPET",
          description: "Evidence requirement for work on this property.",
          spec: "docs/specs/06-tasks-and-scheduling.md",
        },
      ]);
    }
    if (resolved === "/w/acme/api/v1/agent_preferences/property/prop_1") {
      return jsonResponse({
        scope_kind: "property",
        scope_id: "prop_1",
        body_md: "",
        token_count: 0,
        updated_by_user_id: null,
        updated_at: null,
        writable: true,
        soft_cap: 800,
        hard_cap: 1200,
        blocked_actions: [],
        default_approval_mode: "auto",
      });
    }
    if (resolved === "/w/acme/api/v1/employees") {
      return jsonResponse([
        {
          id: "emp_1",
          name: "Maya Santos",
          roles: ["housekeeper"],
          properties: ["prop_1"],
          avatar_initials: "MS",
          avatar_file_id: null,
          avatar_url: null,
          phone: null,
          email: "maya@example.com",
          started_on: "2026-01-01",
          capabilities: {},
          workspaces: ["ws_owner"],
          villas: ["prop_1"],
          language: "en",
          weekly_availability: {},
          evidence_policy: "inherit",
          preferred_locale: null,
          settings_override: {},
        },
      ]);
    }
    throw new Error(`Unexpected fetch call: ${resolved}`);
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function Harness({ initial = "/property/prop_1" }: { initial?: string }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <WorkspaceProvider>
          <Routes>
            <Route path="/property/:pid" element={<PropertyDetailPage />} />
            <Route path="/stays" element={<div>Stays route reached</div>} />
            <Route path="/instructions" element={<div>Instructions route reached</div>} />
            <Route path="/property/:pid/closures" element={<div>Closures route reached</div>} />
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

describe("<PropertyDetailPage>", () => {
  it("marks unimplemented property detail actions as disabled with visible reasons", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByRole("heading", { name: "Villa Rosa" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Edit property" })).toBeDisabled();
      expect(screen.getByText("Editing is not implemented yet.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Areas" })).toBeDisabled();
      expect(screen.getByText("Area editing for property detail is not implemented yet.")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "More actions" }));
      expect(screen.getByRole("menuitem", { name: /New task/ })).toBeDisabled();
      expect(screen.getByText("Create tasks from Tasks or Today until property-scoped quick add ships.")).toBeInTheDocument();
      expect(fake.calls).toContainEqual({
        url: "/w/acme/api/v1/properties/prop_1",
        method: "GET",
      });
    } finally {
      fake.restore();
    }
  });

  it("switches implemented local tabs and links route-backed tabs to real pages", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Tasks for this property")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Assets" }));
      expect(await screen.findByText("No assets tracked for this property.")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Sharing & client" }));
      expect(await screen.findByText("Billing client")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Settings" }));
      expect(await screen.findByText("Settings overrides")).toBeInTheDocument();

      expect(screen.getByRole("link", { name: "Stays" })).toHaveAttribute("href", "/stays?property_id=prop_1");
      expect(screen.getByRole("link", { name: "Instructions" })).toHaveAttribute("href", "/instructions?property_id=prop_1");
      expect(screen.getByRole("link", { name: "Closures" })).toHaveAttribute("href", "/property/prop_1/closures");

      fireEvent.click(screen.getByRole("link", { name: "Stays" }));
      await waitFor(() => {
        expect(screen.getByText("Stays route reached")).toBeInTheDocument();
      });

      cleanup();
      render(<Harness />);
      expect(await screen.findByText("Tasks for this property")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("link", { name: "Instructions" }));
      await waitFor(() => {
        expect(screen.getByText("Instructions route reached")).toBeInTheDocument();
      });

      cleanup();
      render(<Harness />);
      expect(await screen.findByText("Tasks for this property")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("link", { name: "Closures" }));
      await waitFor(() => {
        expect(screen.getByText("Closures route reached")).toBeInTheDocument();
      });
    } finally {
      fake.restore();
    }
  });
});
