import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import PropertiesPage from "./PropertiesPage";
import { jsonResponse } from "@/test/helpers";

function installFetch({
  createPermission = "allow",
  emptyProperties = false,
  failProperties = false,
  holdPermission = false,
}: {
  createPermission?: "allow" | "deny";
  emptyProperties?: boolean;
  failProperties?: boolean;
  holdPermission?: boolean;
} = {}) {
  const calls: string[] = [];
  const requests: { url: string; method: string; body: unknown }[] = [];
  const original = globalThis.fetch;
  let createdProperty = false;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    // code-health: ignore[ccn nloc] Properties route fetch fixture keeps all promoted endpoint shapes explicit.
    const resolved = typeof url === "string" ? url : url.toString();
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
    calls.push(resolved);
    requests.push({ url: resolved, method, body });
    if (resolved === "/api/v1/auth/me") {
      return jsonResponse({
        user_id: "usr_1",
        display_name: "Mina",
        email: "mina@example.com",
        available_workspaces: [
          {
            workspace_id: "ws_owner",
            workspace: {
              id: "acme",
              name: "Acme",
              timezone: "Europe/Lisbon",
              default_currency: "EUR",
              default_country: "PT",
              default_locale: "pt-PT",
            },
            grant_role: "manager",
            binding_org_id: null,
            source: "workspace_grant",
          },
        ],
        current_workspace_id: "ws_owner",
        is_deployment_admin: false,
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
      if (method === "POST") {
        const createBody = body as {
          name: string;
          kind: string;
          address_json: Record<string, unknown>;
          country: string;
          locale: string | null;
          default_currency: string | null;
          timezone: string;
          property_notes_md: string;
        };
        createdProperty = true;
        return jsonResponse(
          {
            id: "prop_created",
            name: createBody.name,
            kind: createBody.kind,
            address: "Austin, US",
            address_json: createBody.address_json,
            country: createBody.country,
            locale: createBody.locale,
            default_currency: createBody.default_currency,
            timezone: createBody.timezone,
            lat: null,
            lon: null,
            client_org_id: null,
            owner_user_id: null,
            tags_json: [],
            welcome_defaults_json: {},
            property_notes_md: createBody.property_notes_md,
            created_at: "2026-05-06T00:00:00Z",
            updated_at: null,
            deleted_at: null,
          },
          201,
        );
      }
      if (failProperties) {
        return jsonResponse({ type: "server_error", title: "Server error" }, 500);
      }
      if (emptyProperties && !createdProperty) {
        return jsonResponse([]);
      }
      return jsonResponse([
        {
          id: createdProperty ? "prop_created" : "prop_1",
          name: createdProperty ? "Lake House" : "Villa Rosa",
          city: createdProperty ? "Austin" : "Porto",
          timezone: createdProperty ? "America/Chicago" : "Europe/Lisbon",
          color: "moss",
          kind: createdProperty ? "residence" : "str",
          areas: createdProperty ? [] : ["Kitchen", "Terrace"],
          evidence_policy: "inherit",
          country: "PT",
          locale: "pt-PT",
          settings_override: {},
          client_org_id: createdProperty ? null : "org_1",
          owner_user_id: null,
        },
      ]);
    }
    if (resolved.startsWith("/w/acme/api/v1/permissions/resolved/self?")) {
      const parsed = new URL(resolved, "http://test.local");
      expect(parsed.searchParams.get("action_key")).toBe("properties.create");
      expect(parsed.searchParams.get("scope_kind")).toBe("workspace");
      expect(parsed.searchParams.get("scope_id")).toBe("ws_owner");
      if (holdPermission) {
        return new Promise<Response>(() => undefined);
      }
      return jsonResponse({
        effect: createPermission,
        source_layer: "default",
        source_rule_id: null,
        matched_groups: createPermission === "allow" ? ["managers"] : [],
      });
    }
    if (resolved === "/w/acme/api/v1/stays/reservations?limit=500") {
      return jsonResponse({
        data: [
          {
            id: "res_1",
            property_id: "prop_1",
            check_in: "2026-04-29T12:00:00Z",
            check_out: "2026-04-30T10:00:00Z",
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
    if (resolved === "/w/acme/api/v1/billing/organizations") {
      return jsonResponse({
        data: [
          {
            id: "org_1",
            workspace_id: "ws_owner",
            kind: "client",
            display_name: "Luxe Guests",
            tax_id: null,
            default_currency: "EUR",
            notes_md: null,
          },
        ],
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
          {
            property_id: "prop_1",
            workspace_id: "ws_partner",
            label: "Partner Ops",
            membership_role: "managed_workspace",
            status: "active",
            share_guest_identity: false,
            created_at: "2026-04-29T00:00:00Z",
          },
        ],
        next_cursor: null,
        has_more: false,
      });
    }
    if (resolved === "/w/acme/api/v1/properties/prop_created/share") {
      return jsonResponse({
        data: [
          {
            property_id: "prop_created",
            workspace_id: "ws_owner",
            label: "Lake House",
            membership_role: "owner_workspace",
            status: "active",
            share_guest_identity: true,
            created_at: "2026-05-06T00:00:00Z",
          },
        ],
        next_cursor: null,
        has_more: false,
      });
    }
    if (resolved === "/w/acme/api/v1/property_closures?property_id=prop_1&limit=100") {
      return jsonResponse({
        data: [
          {
            id: "closure_1",
            property_id: "prop_1",
            starts_at: "2026-05-01T00:00:00Z",
            ends_at: "2026-05-02T00:00:00Z",
            reason: "renovation",
          },
        ],
        next_cursor: null,
        has_more: false,
      });
    }
    if (resolved === "/w/acme/api/v1/property_closures?property_id=prop_created&limit=100") {
      return jsonResponse({
        data: [],
        next_cursor: null,
        has_more: false,
      });
    }
    throw new Error(`Unexpected fetch call: ${resolved}`);
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    requests,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/w/acme/properties"]}>
        <WorkspaceProvider>
          <PropertiesPage />
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

describe("<PropertiesPage>", () => {
  it("renders the mock cards from production property endpoints", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByRole("heading", { name: "Villa Rosa" })).toBeInTheDocument();
      expect(screen.getByText("Porto · Europe/Lisbon")).toBeInTheDocument();
      expect(screen.getByText("1 stays")).toBeInTheDocument();
      expect(screen.getByText("2 areas")).toBeInTheDocument();
      expect(screen.getByText("1 closure")).toBeInTheDocument();
      expect(screen.getByText("Owner")).toBeInTheDocument();
      expect(screen.getByText("Managed: Partner Ops")).toBeInTheDocument();
      expect(screen.getByText("Client: Luxe Guests")).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Overview/ })).toHaveAttribute("href", "/w/acme/property/prop_1");
      expect(fake.calls).toContain("/api/v1/auth/me");
      expect(fake.calls).toContain("/api/v1/me/workspaces");
      expect(fake.calls).toContain("/w/acme/api/v1/properties/prop_1/share");
    } finally {
      fake.restore();
    }
  });

  it("renders a zero-property empty state with the create action for permitted users", async () => {
    const fake = installFetch({ emptyProperties: true });
    try {
      render(<Harness />);

      expect(await screen.findByRole("heading", { name: "No properties visible" })).toBeInTheDocument();
      expect(screen.getByText("Properties added to this workspace or shared with it will appear here.")).toBeInTheDocument();
      const header = screen.getByRole("banner");
      expect(await within(header).findByRole("button", { name: "+ Add property" })).toBeEnabled();
      expect(screen.getByText("Create the first property to start tracking stays, closures, and work areas.")).toBeInTheDocument();
      const emptyState = screen.getByRole("heading", { name: "No properties visible" }).closest(".empty-state");
      expect(emptyState).not.toBeNull();
      expect(within(emptyState as HTMLElement).queryByRole("button", { name: /Add property/ })).toBeNull();
      expect(screen.queryByRole("heading", { name: "Villa Rosa" })).toBeNull();
      expect(fake.calls).not.toContain("/w/acme/api/v1/properties/prop_1/share");
      expect(fake.calls).not.toContain("/w/acme/api/v1/stays/reservations?limit=500");
      expect(fake.calls).not.toContain("/w/acme/api/v1/billing/organizations");
      expect(fake.calls).not.toContain("/api/v1/me/workspaces");
    } finally {
      fake.restore();
    }
  });

  it("opens creation from the zero-property empty state and adds the created property", async () => {
    const fake = installFetch({ emptyProperties: true });
    try {
      render(<Harness />);

      expect(await screen.findByRole("heading", { name: "No properties visible" })).toBeInTheDocument();
      fireEvent.click(await within(screen.getByRole("banner")).findByRole("button", { name: "+ Add property" }));
      const dialog = await screen.findByRole("dialog", { name: "Add property" });
      expect(dialog.querySelector("form")).toHaveClass("property-edit-dialog");
      expect(within(dialog).getByLabelText("Name Required").closest("label")).toHaveClass(
        "property-edit-dialog__identity-field",
      );
      expect(within(dialog).getByLabelText("Kind Optional").closest("label")).toHaveClass(
        "property-edit-dialog__identity-field",
      );
      expect(within(dialog).getByLabelText("Address line 1 Optional").closest("label")).not.toHaveClass(
        "property-edit-dialog__identity-field",
      );
      expect(within(dialog).getByRole("combobox", { name: /^Country\b/ })).toHaveValue("Portugal");
      expect(within(dialog).getByRole("combobox", { name: /^Timezone\b/ })).toHaveValue("Europe/Lisbon");
      expect(within(dialog).getByLabelText(/^Locale\b/)).toHaveValue("pt-PT");
      expect(within(dialog).getByLabelText(/^Default currency\b/)).toHaveValue("EUR");
      const countryInput = within(dialog).getByRole("combobox", { name: /^Country\b/ });
      const timezoneInput = within(dialog).getByRole("combobox", { name: /^Timezone\b/ });
      for (const field of [
        within(dialog).getByLabelText("Name Required"),
        countryInput,
        timezoneInput,
      ]) {
        expect(field).toBeRequired();
      }
      for (const field of [
        within(dialog).getByLabelText("Kind Optional"),
        within(dialog).getByLabelText("Address line 1 Optional"),
        within(dialog).getByLabelText("Address line 2 Optional"),
        within(dialog).getByLabelText("City Optional"),
        within(dialog).getByLabelText("State / province Optional"),
        within(dialog).getByLabelText("Postal code Optional"),
        within(dialog).getByLabelText("Locale Optional"),
        within(dialog).getByLabelText("Default currency Optional"),
        within(dialog).getByLabelText("Notes Optional"),
      ]) {
        expect(field).not.toBeRequired();
      }
      expect(within(dialog).getAllByText("Required")).toHaveLength(3);
      expect(within(dialog).getAllByText("Optional")).toHaveLength(9);
      expect(within(dialog).getByRole("option", {
        name: "Primary residence - no automatic area or stay lifecycle setup",
      })).toHaveValue("residence");
      expect(within(dialog).getByRole("option", {
        name: "Vacation home - seed turnover areas and checkout workflow",
      })).toHaveValue("vacation");
      expect(within(dialog).getByRole("option", {
        name: "Short-term rental - seed turnover areas and checkout workflow",
      })).toHaveValue("str");
      expect(within(dialog).getByRole("option", {
        name: "Mixed use - seed turnover setup for guest, staff, and other stays",
      })).toHaveValue("mixed");
      expect(within(dialog).queryByRole("option", { name: "str" })).toBeNull();
      fireEvent.change(within(dialog).getByLabelText(/^Name\b/), {
        target: { value: "Lake House" },
      });
      fireEvent.change(within(dialog).getByLabelText(/^Kind\b/), {
        target: { value: "str" },
      });
      fireEvent.change(within(dialog).getByLabelText(/^City\b/), {
        target: { value: "Austin" },
      });
      fireEvent.change(countryInput, {
        target: { value: "Atlantis" },
      });
      await waitFor(() => expect(countryInput).toBeInvalid());
      fireEvent.change(countryInput, {
        target: { value: "United" },
      });
      const countryListbox = await screen.findByRole("listbox", { name: /^Country\b/ });
      expect(
        within(countryListbox).getByRole("option", { name: /United States/ }),
      ).toBeInTheDocument();
      fireEvent.keyDown(countryInput, { key: "ArrowDown" });
      fireEvent.keyDown(countryInput, { key: "ArrowDown" });
      fireEvent.keyDown(countryInput, { key: "Enter" });
      expect(countryInput).toHaveValue("United States");
      fireEvent.change(timezoneInput, {
        target: { value: "Chicago" },
      });
      const timezoneListbox = await screen.findByRole("listbox", { name: /^Timezone\b/ });
      expect(
        within(timezoneListbox).getByRole("option", { name: /America\/Chicago/ }),
      ).toBeInTheDocument();
      fireEvent.keyDown(timezoneInput, { key: "Enter" });
      expect(timezoneInput).toHaveValue("America/Chicago");
      fireEvent.click(within(dialog).getByRole("button", { name: "Create property" }));

      expect(await screen.findByRole("heading", { name: "Lake House" })).toBeInTheDocument();
      expect(screen.getByText("Austin · America/Chicago")).toBeInTheDocument();
      await waitFor(() =>
        expect(
          fake.requests.some((request) => request.url === "/w/acme/api/v1/properties" && request.method === "POST"),
        ).toBe(true),
      );
      const createRequest = fake.requests.find((request) =>
        request.url === "/w/acme/api/v1/properties" && request.method === "POST"
      );
      expect(createRequest?.body).toMatchObject({
        name: "Lake House",
        kind: "str",
        timezone: "America/Chicago",
        country: "US",
        locale: "pt-PT",
        default_currency: "EUR",
        address_json: { city: "Austin", country: "US" },
      });
      const propertiesReads = fake.requests.filter((request) =>
        request.url === "/w/acme/api/v1/properties" && request.method === "GET"
      );
      expect(propertiesReads.length).toBeGreaterThanOrEqual(2);
    } finally {
      fake.restore();
    }
  });

  it("keeps the zero-property empty state distinct when the user cannot create properties", async () => {
    const fake = installFetch({ createPermission: "deny", emptyProperties: true });
    try {
      render(<Harness />);

      expect(await screen.findByRole("heading", { name: "No properties visible" })).toBeInTheDocument();
      expect(
        await screen.findByText("Ask an owner or manager to add a property or share one with this workspace."),
      ).toBeInTheDocument();
      expect(within(screen.getByRole("banner")).queryByRole("button", { name: /Add property/ })).toBeNull();
      expect(screen.queryByRole("button", { name: /Add property/ })).toBeNull();
      expect(screen.queryByText("Failed to load.")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("keeps zero-property permission checks distinct from denied users", async () => {
    const fake = installFetch({ emptyProperties: true, holdPermission: true });
    try {
      render(<Harness />);

      expect(await screen.findByRole("heading", { name: "No properties visible" })).toBeInTheDocument();
      expect(screen.getByRole("status")).toHaveTextContent("Checking whether you can add a property.");
      expect(screen.queryByText("Ask an owner or manager to add a property or share one with this workspace.")).toBeNull();
      expect(within(screen.getByRole("banner")).queryByRole("button", { name: /Add property/ })).toBeNull();
      expect(screen.queryByRole("button", { name: /Add property/ })).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("renders the mock failure copy when the properties query fails", async () => {
    const fake = installFetch({ failProperties: true });
    try {
      render(<Harness />);

      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "Villa Rosa" })).toBeNull();
    } finally {
      fake.restore();
    }
  });
});
