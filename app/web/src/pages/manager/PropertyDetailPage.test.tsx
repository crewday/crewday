import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Link, MemoryRouter, Route, Routes, useLocation, useParams } from "react-router-dom";
import { CalendarClock } from "lucide-react";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { EmptyState } from "@/components/common";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { jsonResponse } from "@/test/helpers";
import managerPanelsCss from "@/styles/manager-panels.css?raw";
import PropertyDetailPage from "./PropertyDetailPage";
import PropertyTabs from "./property/PropertyTabs";
import { type PropertyRelatedPage } from "./property/PropertyTabs.lib";
import type { AssetDocument } from "@/types/api";

interface RequestRecord {
  url: string;
  method: string;
  body: unknown;
}

interface TestArea {
  id: string;
  property_id: string;
  unit_id: string | null;
  name: string;
  kind: "indoor_room" | "outdoor" | "service";
  order_hint: number;
  parent_area_id: string | null;
  notes_md: string;
  created_at: string;
  updated_at: string | null;
  deleted_at: string | null;
}

interface TestProperty {
  id: string;
  name: string;
  kind: "residence" | "vacation" | "str" | "mixed";
  address: string;
  address_json: {
    line1: string;
    line2: string;
    city: string;
    state_province: string;
    postal_code: string;
    country: string;
  };
  timezone: string;
  country: string;
  locale: string | null;
  default_currency: string | null;
  lat: number | null;
  lon: number | null;
  client_org_id: string | null;
  owner_user_id: string | null;
  tags_json: string[];
  welcome_defaults_json: Record<string, unknown>;
  property_notes_md: string;
  settings_override: Record<string, unknown>;
  created_at: string;
  updated_at: string | null;
  deleted_at: string | null;
}

interface InstallFetchOptions {
  emptyOverview?: boolean;
  denyPropertyDocuments?: boolean;
  failAreaPost?: boolean;
  failAreasList?: boolean;
  failPropertyPatch?: boolean;
  failPropertySettingsPatch?: boolean;
  settingsOverride?: Record<string, unknown>;
  availableWorkspaces?: Array<{
    workspace_id?: string | null;
    workspace: {
      id: string;
      name: string;
      timezone: string;
      default_currency: string;
      default_country: string;
      default_locale: string;
    };
    grant_role: "manager" | "worker" | "client" | "guest" | "admin" | null;
    binding_org_id: string | null;
    source: "workspace_grant" | "property_grant" | "org_grant" | "work_engagement";
  }>;
}

function parseBody(init?: RequestInit): unknown {
  // code-health: ignore[nloc] Lizard misattributes the property-detail fetch fixture to this tiny body parser.
  if (init?.body instanceof FormData) return init.body;
  if (typeof init?.body !== "string") return null;
  return JSON.parse(init.body);
}

function installFetch(options: InstallFetchOptions = {}) {
  let property: TestProperty = {
    id: "prop_1",
    name: "Villa Rosa",
    kind: "str",
    address: "Rua das Flores 12, Porto, PT",
    address_json: {
      line1: "Rua das Flores 12",
      line2: "",
      city: "Porto",
      state_province: "",
      postal_code: "4000",
      country: "PT",
    },
    timezone: "Europe/Lisbon",
    country: "PT",
    locale: "pt-PT",
    default_currency: "EUR",
    lat: null,
    lon: null,
    client_org_id: "org_1",
    owner_user_id: null,
    tags_json: [],
    welcome_defaults_json: {},
    property_notes_md: "Gate code changes each spring.",
    settings_override: options.settingsOverride ?? {},
    created_at: "2026-04-29T00:00:00Z",
    updated_at: null,
    deleted_at: null,
  };
  let areas: TestArea[] = [
    {
      id: "area_1",
      property_id: "prop_1",
      unit_id: null,
      name: "Kitchen",
      kind: "indoor_room",
      order_hint: 1,
      parent_area_id: null,
      notes_md: "",
      created_at: "2026-04-29T00:00:00Z",
      updated_at: null,
      deleted_at: null,
    },
    {
      id: "area_2",
      property_id: "prop_1",
      unit_id: null,
      name: "Terrace",
      kind: "outdoor",
      order_hint: 2,
      parent_area_id: null,
      notes_md: "",
      created_at: "2026-04-29T00:00:00Z",
      updated_at: null,
      deleted_at: null,
    },
  ];
  let propertyDocuments: AssetDocument[] = [
    {
      id: "doc_1",
      asset_id: null,
      property_id: "prop_1",
      kind: "permit",
      title: "Pool permit",
      filename: "pool-permit.pdf",
      size_kb: 24,
      uploaded_at: "2026-04-29T10:00:00Z",
      expires_on: null,
      amount_cents: null,
      amount_currency: null,
      extraction_status: "succeeded",
      extracted_at: "2026-04-29T10:05:00Z",
    },
  ];
  const calls: RequestRecord[] = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    // code-health: ignore[ccn nloc] Property-detail fixture keeps every promoted endpoint branch visible beside the route tests.
    const resolved = typeof url === "string" ? url : url.toString();
    const method = init?.method ?? "GET";
    const body = parseBody(init);
    calls.push({ url: resolved, method, body });
    if (resolved === "/api/v1/auth/me") {
      return jsonResponse({
        user_id: "usr_1",
        display_name: "Mina",
        email: "mina@example.com",
        available_workspaces: options.availableWorkspaces ?? [],
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
          settings_override: property.settings_override,
        },
        ...(options.availableWorkspaces ?? []).map((entry) => ({
          workspace_id: entry.workspace_id ?? entry.workspace.id,
          slug: entry.workspace.id,
          name: entry.workspace.name,
          current_role: entry.grant_role,
          last_seen_at: null,
          settings_override: {},
        })),
      ]);
    }
    if (resolved === "/w/acme/api/v1/properties") {
      return jsonResponse([
        {
          id: property.id,
          name: property.name,
          city: property.address_json.city,
          timezone: property.timezone,
          color: "moss",
          kind: property.kind,
          areas: areas.map((area) => area.name),
          evidence_policy: "inherit",
          country: property.country,
          locale: property.locale,
          settings_override: {},
          client_org_id: property.client_org_id,
          owner_user_id: property.owner_user_id,
        },
        {
          id: "prop_2",
          name: "Casa Azul",
          city: "Lisbon",
          timezone: "Europe/Lisbon",
          color: "sky",
          kind: "str",
          areas: [],
          evidence_policy: "inherit",
          country: "PT",
          locale: "pt-PT",
          settings_override: {},
          client_org_id: null,
          owner_user_id: null,
        },
      ]);
    }
    if (resolved === "/w/acme/api/v1/tasks?property_id=prop_2&limit=100") {
      return jsonResponse({ data: [], next_cursor: null, has_more: false });
    }
    if (resolved === "/w/acme/api/v1/stays/reservations?property_id=prop_2&limit=100") {
      return jsonResponse({ data: [], next_cursor: null, has_more: false });
    }
    if (resolved === "/w/acme/api/v1/properties/prop_2/share") {
      return jsonResponse({ data: [], next_cursor: null, has_more: false });
    }
    if (resolved === "/w/acme/api/v1/properties/prop_2") {
      return jsonResponse({
        ...property,
        id: "prop_2",
        name: "Casa Azul",
        client_org_id: null,
        settings_override: {},
      });
    }
    if (resolved === "/w/acme/api/v1/properties/prop_1") {
      if (method === "PATCH") {
        if (options.failPropertyPatch) {
          return jsonResponse({ detail: "You do not have permission to edit this property." }, 403);
        }
        const patch = body as Partial<typeof property>;
        property = {
          ...property,
          ...patch,
          address_json: {
            ...property.address_json,
            ...(patch.address_json ?? {}),
          },
          updated_at: "2026-05-05T10:00:00Z",
        };
      }
      return jsonResponse(property);
    }
    if (resolved === "/w/acme/api/v1/properties/prop_1/areas") {
      if (method === "GET" && options.failAreasList) {
        return jsonResponse({ detail: "You do not have permission to view areas." }, 403);
      }
      if (method === "POST") {
        if (options.failAreaPost) {
          return jsonResponse({ detail: "name must be a non-blank string" }, 422);
        }
        const draft = body as Omit<TestArea, "id" | "property_id" | "created_at" | "updated_at" | "deleted_at">;
        const next: TestArea = {
          id: "area_" + String(areas.length + 1),
          property_id: "prop_1",
          unit_id: draft.unit_id,
          name: draft.name,
          kind: draft.kind,
          order_hint: draft.order_hint,
          parent_area_id: draft.parent_area_id,
          notes_md: draft.notes_md,
          created_at: "2026-05-05T10:00:00Z",
          updated_at: null,
          deleted_at: null,
        };
        areas = [...areas, next];
        return jsonResponse(next, 201);
      }
      return jsonResponse({ data: areas, next_cursor: null, has_more: false });
    }
    if (resolved === "/w/acme/api/v1/properties/prop_1/documents") {
      if (options.denyPropertyDocuments) {
        return jsonResponse({ title: "Forbidden", detail: "Forbidden" }, 403);
      }
      if (method === "POST") {
        const form = body as FormData;
        const file = form.get("file");
        const uploaded: AssetDocument = {
          id: "doc_" + String(propertyDocuments.length + 1),
          asset_id: null,
          property_id: "prop_1",
          kind: form.get("category") as AssetDocument["kind"],
          title: String(form.get("title") ?? "Untitled"),
          filename: file instanceof File ? file.name : "upload.bin",
          size_kb: file instanceof File ? Math.max(1, Math.round(file.size / 1024)) : 0,
          uploaded_at: "2026-05-05T10:00:00Z",
          expires_on: null,
          amount_cents: null,
          amount_currency: null,
          extraction_status: "pending",
          extracted_at: null,
        };
        propertyDocuments = [...propertyDocuments, uploaded];
        return jsonResponse(uploaded, 201);
      }
      return jsonResponse({ data: propertyDocuments, next_cursor: null, has_more: false });
    }
    if (resolved === "/w/acme/api/v1/properties/prop_2/documents") {
      return jsonResponse({ data: [], next_cursor: null, has_more: false });
    }
    if (resolved === "/w/acme/api/v1/documents/doc_1/extraction" && method === "GET") {
      return jsonResponse({
        document_id: "doc_1",
        status: "succeeded",
        extractor: "pypdf",
        body_preview: "Pool permit text",
        page_count: 2,
        token_count: 450,
        has_secret_marker: false,
        last_error: null,
        extracted_at: "2026-04-29T10:05:00Z",
      });
    }
    if (resolved === "/w/acme/api/v1/documents/doc_1/extraction/pages/1" && method === "GET") {
      return jsonResponse({
        page: 1,
        char_start: 0,
        char_end: 20,
        body: "Pool permit notes.",
        more_pages: false,
      });
    }
    if (resolved === "/w/acme/api/v1/documents/doc_1/extraction/retry" && method === "POST") {
      return jsonResponse(null);
    }
    if (resolved.startsWith("/w/acme/api/v1/areas/")) {
      const areaId = resolved.split("/").at(-1) ?? "";
      const existing = areas.find((area) => area.id === areaId);
      if (!existing) throw new Error(`Unexpected area id: ${areaId}`);
      if (method === "PATCH") {
        const draft = body as Partial<TestArea>;
        areas = areas.map((area) =>
          area.id === areaId
            ? { ...area, ...draft, updated_at: "2026-05-05T10:00:00Z" }
            : area,
        );
        return jsonResponse(areas.find((area) => area.id === areaId));
      }
      if (method === "DELETE") {
        areas = areas.filter((area) => area.id !== areaId && area.parent_area_id !== areaId);
        return jsonResponse(null, 204);
      }
    }
    if (resolved === "/w/acme/api/v1/tasks?property_id=prop_1&limit=100") {
      if (options.emptyOverview) {
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
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
      if (options.emptyOverview) {
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
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
      if (method === "POST") {
        const request = body as { workspace_slug?: string; membership_role?: string };
        return jsonResponse({
          property_id: "prop_1",
          workspace_id: request.workspace_slug ?? "",
          label: "Agency Partners",
          membership_role: request.membership_role ?? "managed_workspace",
          status: "active",
          share_guest_identity: false,
          created_at: "2026-05-05T10:00:00Z",
        }, 201);
      }
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
    if (resolved === "/w/acme/api/v1/properties/prop_1/settings") {
      if (method === "PATCH") {
        if (options.failPropertySettingsPatch) {
          return jsonResponse({ detail: "Setting update rejected." }, 422);
        }
        const patch = body as Record<string, unknown>;
        property = {
          ...property,
          settings_override: Object.entries(patch).reduce<Record<string, unknown>>(
            (next, [key, value]) => {
              if (value === null) {
                delete next[key];
              } else {
                next[key] = value;
              }
              return next;
            },
            { ...property.settings_override },
          ),
        };
      }
      const defaults: Record<string, unknown> = {
        evidence_policy: "inherit",
        require_photos: true,
        minimum_notice_hours: 24,
      };
      const catalog = [
        { key: "evidence_policy", catalog_default: "inherit" },
        { key: "require_photos", catalog_default: false },
        { key: "minimum_notice_hours", catalog_default: 12 },
        { key: "workspace_only", catalog_default: true },
      ];
      return jsonResponse({
        overrides: property.settings_override,
        resolved: Object.fromEntries(catalog.map(({ key, catalog_default }) => {
          if (key in property.settings_override) {
            return [key, { value: property.settings_override[key], source: "property" }];
          }
          if (key in defaults) {
            return [key, { value: defaults[key], source: "workspace" }];
          }
          return [key, { value: catalog_default, source: "catalog" }];
        })),
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
          override_scope: "W/P/U/WE/T",
          description: "Evidence requirement for work on this property.",
          spec: "docs/specs/06-tasks-and-scheduling.md",
        },
        {
          key: "require_photos",
          label: "Require photos",
          type: "bool",
          catalog_default: false,
          enum_values: null,
          override_scope: "P",
          description: "Whether this property requires photo evidence.",
          spec: "docs/specs/06-tasks-and-scheduling.md",
        },
        {
          key: "minimum_notice_hours",
          label: "Minimum notice",
          type: "int",
          catalog_default: 12,
          enum_values: null,
          override_scope: "P",
          description: "Minimum scheduling notice in hours.",
          spec: "docs/specs/06-tasks-and-scheduling.md",
        },
        {
          key: "workspace_only",
          label: "Workspace only",
          type: "bool",
          catalog_default: true,
          enum_values: null,
          override_scope: "W",
          description: "Workspace-only setting.",
          spec: "docs/specs/00.md",
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

function Harness({ initial = "/w/acme/property/prop_1" }: { initial?: string }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <WorkspaceProvider>
          <Routes>
            <Route path="/w/:slug/property/:pid" element={<PropertyDetailPage />} />
            <Route path="/w/:slug/property/:pid/assets" element={<RelatedRoute label="Assets route reached" activeRelatedPage="assets" />} />
            <Route path="/w/:slug/property/:pid/stays" element={<RelatedRoute label="Stays route reached" activeRelatedPage="stays" />} />
            <Route path="/w/:slug/property/:pid/instructions" element={<RelatedRoute label="Instructions route reached" activeRelatedPage="instructions" />} />
            <Route path="/w/:slug/property/:pid/closures" element={<RelatedRoute label="Closures route reached" activeRelatedPage="closures" />} />
            <Route path="/w/:slug/property/:pid/inventory" element={<RelatedRoute label="Inventory route reached" activeRelatedPage="inventory" />} />
            <Route path="/w/:slug/property/:pid/schedules" element={<RelatedRoute label="Schedules route reached" activeRelatedPage="schedules" />} />
          </Routes>
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function propertyPreferenceCalls(calls: RequestRecord[], method?: string): RequestRecord[] {
  return calls.filter((call) =>
    call.url === "/w/acme/api/v1/agent_preferences/property/prop_1" &&
    (method === undefined || call.method === method)
  );
}

function settingRow(label: string): HTMLElement {
  const row = screen.getAllByLabelText(label).find((candidate) =>
    candidate instanceof HTMLElement && candidate.classList.contains("inline-table-form__group")
  );
  if (!row) throw new Error(`Missing settings row: ${label}`);
  return row;
}

function RelatedRoute({
  label,
  activeRelatedPage,
}: {
  label: string;
  activeRelatedPage: PropertyRelatedPage;
}) {
  const { pathname } = useLocation();
  const { pid = "" } = useParams<{ pid: string }>();
  return (
    <>
      <PropertyTabs
        pathname={pathname}
        propertyId={pid}
        activeRelatedPage={activeRelatedPage}
      />
      <div>{label}</div>
    </>
  );
}

function SwitchingHarness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/w/acme/property/prop_1#settings"]}>
        <WorkspaceProvider>
          <Link to="/w/acme/property/prop_2">Next property</Link>
          <Routes>
            <Route path="/w/:slug/property/:pid" element={<PropertyDetailPage />} />
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
  window.history.replaceState(null, "", "/");
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
  window.history.replaceState(null, "", "/");
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<PropertyDetailPage>", () => {
  it("renders shared empty states for blank overview stays and tasks", async () => {
    const emptyState = render(
      <EmptyState
        icon={CalendarClock}
        title="No upcoming stays"
        copy="Plain empty copy."
      />,
    );
    expect(screen.getByText("Plain empty copy.").tagName).toBe("P");
    const glyph = emptyState.container.querySelector(".empty-state__glyph");
    expect(glyph).toHaveAttribute("aria-hidden", "true");
    const icon = emptyState.container.querySelector(".empty-state__icon");
    expect(icon).toHaveAttribute("aria-hidden", "true");
    expect(icon).toHaveAttribute("width", "22");
    expect(icon).toHaveAttribute("stroke-width", "2");
    cleanup();

    const fake = installFetch({ emptyOverview: true });
    try {
      render(<Harness />);

      expect(await screen.findByRole("heading", { name: "Villa Rosa" })).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "Upcoming stays" })).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "No upcoming stays" })).toBeInTheDocument();
      expect(screen.getByText("New reservations for this property will appear here with guest, source, and date details.")).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "Tasks for this property" })).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "No tasks scheduled" })).toBeInTheDocument();
      expect(screen.getByText("Property tasks will land here once cleanings, inspections, or maintenance work are assigned.")).toBeInTheDocument();
      expect(screen.queryByRole("table")).not.toBeInTheDocument();
      expect(screen.queryByRole("list")).not.toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("loads and saves property agent preferences on overview only", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      const preferencesRegion = await screen.findByRole("region", {
        name: "Agent preferences, Villa Rosa",
      });
      await waitFor(() => {
        expect(propertyPreferenceCalls(fake.calls, "GET")).toHaveLength(1);
      });

      const guidance = within(preferencesRegion).getByLabelText("Guidance (Markdown)");
      fireEvent.change(guidance, {
        target: { value: "Never schedule outdoor work on Tuesdays." },
      });
      fireEvent.click(within(preferencesRegion).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(propertyPreferenceCalls(fake.calls, "PUT").at(-1)?.body).toEqual({
          body_md: "Never schedule outdoor work on Tuesdays.",
        });
      });

      fireEvent.click(screen.getByRole("tab", { name: "Areas" }));
      expect(await screen.findByText("Kitchen")).toBeInTheDocument();
      expect(screen.queryByRole("region", { name: "Agent preferences, Villa Rosa" })).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("tab", { name: "Documents" }));
      expect(await screen.findByText("Pool permit")).toBeInTheDocument();
      expect(screen.queryByRole("region", { name: "Agent preferences, Villa Rosa" })).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("tab", { name: "Sharing & client" }));
      expect(await screen.findByText("Billing client")).toBeInTheDocument();
      expect(screen.queryByRole("region", { name: "Agent preferences, Villa Rosa" })).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("tab", { name: "Settings" }));
      expect(await screen.findByText("Settings overrides")).toBeInTheDocument();
      expect(screen.queryByRole("region", { name: "Agent preferences, Villa Rosa" })).not.toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("does not fetch property agent preferences when opened directly to a non-overview tab", async () => {
    const fake = installFetch();
    try {
      window.history.replaceState(null, "", "/w/acme/property/prop_1#areas");
      render(<Harness initial="/w/acme/property/prop_1#areas" />);

      expect(await screen.findByText("Kitchen")).toBeInTheDocument();
      expect(screen.queryByRole("region", { name: "Agent preferences, Villa Rosa" })).not.toBeInTheDocument();
      expect(propertyPreferenceCalls(fake.calls)).toHaveLength(0);
    } finally {
      fake.restore();
    }
  });

  it("edits supported property fields through the property API", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByRole("heading", { name: "Villa Rosa" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Edit property" })).toBeEnabled();
      expect(screen.queryByText("Editing is not implemented yet.")).not.toBeInTheDocument();
      expect(screen.queryByText("Area editing for property detail is not implemented yet.")).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Edit property" }));
      const dialog = await screen.findByRole("dialog", { name: "Edit property" });
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
      for (const field of [
        within(dialog).getByLabelText("Name Required"),
        within(dialog).getByRole("combobox", { name: /^Country\b/ }),
        within(dialog).getByRole("combobox", { name: /^Timezone\b/ }),
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
        target: { value: "Villa Aurora" },
      });
      fireEvent.change(within(dialog).getByLabelText(/^Kind\b/), {
        target: { value: "mixed" },
      });
      fireEvent.change(within(dialog).getByLabelText(/^City\b/), {
        target: { value: "Braga" },
      });
      fireEvent.change(within(dialog).getByRole("combobox", { name: /^Timezone\b/ }), {
        target: { value: "Madrid" },
      });
      expect((await screen.findAllByText("Europe/Madrid")).length).toBeGreaterThan(0);
      fireEvent.keyDown(within(dialog).getByRole("combobox", { name: /^Timezone\b/ }), { key: "Enter" });
      fireEvent.click(within(dialog).getByRole("button", { name: "Save property" }));

      expect(await screen.findByRole("heading", { name: "Villa Aurora" })).toBeInTheDocument();
      expect(screen.getByText("Braga · Europe/Madrid")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "More actions" }));
      expect(screen.getByRole("menuitem", { name: /New task/ })).toBeDisabled();
      expect(screen.getByText("Create tasks from Tasks or Today until property-scoped quick add ships.")).toBeInTheDocument();
      const patch = fake.calls.find((call) =>
        call.url === "/w/acme/api/v1/properties/prop_1" && call.method === "PATCH"
      );
      expect(patch?.body).toMatchObject({
        name: "Villa Aurora",
        kind: "mixed",
        timezone: "Europe/Madrid",
        country: "PT",
        client_org_id: "org_1",
        address_json: {
          city: "Braga",
          country: "PT",
        },
      });
    } finally {
      fake.restore();
    }
  });

  it("creates, updates, and deletes areas from the property areas tab", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Tasks for this property")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: "Areas" }));
      expect(await screen.findByText("Kitchen")).toBeInTheDocument();
      expect(screen.getByText("Terrace")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "New area" })).not.toBeInTheDocument();

      const createRow = screen.getByLabelText("New area");
      fireEvent.change(within(createRow).getByLabelText("Name"), {
        target: { value: "Pool" },
      });
      fireEvent.change(within(createRow).getByLabelText("Kind"), {
        target: { value: "outdoor" },
      });
      await chooseSearchableOption(createRow, /^Parent\b/, "Kitchen");
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));
      expect(await screen.findByText("Pool")).toBeInTheDocument();

      const poolRow = screen.getByLabelText("Pool");
      fireEvent.click(within(poolRow).getByRole("button", { name: "Edit" }));
      fireEvent.change(within(poolRow).getByLabelText("Name"), {
        target: { value: "Pool deck" },
      });
      fireEvent.change(within(poolRow).getByLabelText("Notes"), {
        target: { value: "Check loungers after checkout." },
      });
      fireEvent.click(within(poolRow).getByRole("button", { name: "Save" }));
      expect(await screen.findByText("Pool deck")).toBeInTheDocument();
      expect(screen.getByText("Check loungers after checkout.")).toBeInTheDocument();

      const kitchenRow = screen.getByLabelText("Kitchen");
      fireEvent.click(within(kitchenRow).getByRole("button", { name: "Edit" }));
      fireEvent.change(within(kitchenRow).getByRole("combobox", { name: /^Parent\b/ }), {
        target: { value: "Pool deck" },
      });
      expect(await screen.findByText("No parent areas")).toBeInTheDocument();
      fireEvent.click(within(kitchenRow).getByRole("button", { name: "Cancel" }));

      fireEvent.click(within(kitchenRow).getByRole("button", { name: "Delete" }));
      const parentDeleteDialog = screen.getByRole("alertdialog", { name: "Delete area?" });
      expect(parentDeleteDialog).toHaveTextContent("Delete Kitchen? This will also delete 1 descendant area.");
      fireEvent.click(within(parentDeleteDialog).getByRole("button", { name: "Cancel" }));

      const deckRow = screen.getByLabelText("Pool deck");
      fireEvent.click(within(deckRow).getByRole("button", { name: "Delete" }));
      const childDeleteDialog = screen.getByRole("alertdialog", { name: "Delete area?" });
      expect(childDeleteDialog).toHaveTextContent("Delete Pool deck? This cannot be undone.");
      expect(fake.calls.some((call) => call.url === "/w/acme/api/v1/areas/area_3" && call.method === "DELETE")).toBe(false);
      fireEvent.click(within(childDeleteDialog).getByRole("button", { name: "Delete area" }));
      await waitFor(() => {
        expect(screen.queryByText("Pool deck")).not.toBeInTheDocument();
      });

      expect(fake.calls.find((call) => call.url === "/w/acme/api/v1/properties/prop_1/areas" && call.method === "POST")?.body).toMatchObject({
        parent_area_id: "area_1",
      });
      expect(fake.calls.find((call) => call.url === "/w/acme/api/v1/areas/area_3" && call.method === "PATCH")?.body).toMatchObject({
        order_hint: 3,
        notes_md: "Check loungers after checkout.",
      });
      expect(fake.calls.some((call) => call.url === "/w/acme/api/v1/areas/area_3" && call.method === "DELETE")).toBe(true);
    } finally {
      fake.restore();
    }
  });

  it("invites a linked workspace through the searchable sharing control", async () => {
    const fake = installFetch({
      availableWorkspaces: [
        {
          workspace_id: "ws_agency",
          workspace: {
            id: "agency",
            name: "Agency Partners",
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
    });
    try {
      render(<Harness />);

      expect(await screen.findByText("Tasks for this property")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: "Sharing & client" }));

      const panel = await screen.findByText("Billing client").then((node) => node.closest(".panel"));
      if (!panel) throw new Error("sharing panel missing");
      await chooseSearchableOption(panel as HTMLElement, /^Workspace\b/, "Agency Partners");
      fireEvent.click(within(panel as HTMLElement).getByRole("button", { name: "Invite as agency" }));

      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("button", { name: "Invite" }));

      await waitFor(() => {
        expect(fake.calls.some((call) => call.url === "/w/acme/api/v1/properties/prop_1/share" && call.method === "POST")).toBe(true);
      });
      expect(fake.calls.find((call) => call.url === "/w/acme/api/v1/properties/prop_1/share" && call.method === "POST")?.body).toEqual({
        workspace_slug: "agency",
        membership_role: "managed_workspace",
      });
    } finally {
      fake.restore();
    }
  });

  it("keeps the property editor open and shows server permission errors", async () => {
    const fake = installFetch({ failPropertyPatch: true });
    try {
      render(<Harness />);

      expect(await screen.findByRole("heading", { name: "Villa Rosa" })).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Edit property" }));
      const dialog = await screen.findByRole("dialog", { name: "Edit property" });
      fireEvent.change(within(dialog).getByLabelText(/^Name\b/), {
        target: { value: "Villa Aurora" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Save property" }));

      expect(await within(dialog).findByRole("alert")).toHaveTextContent(
        "You do not have permission to edit this property.",
      );
      expect(screen.getByRole("dialog", { name: "Edit property" })).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "Villa Rosa" })).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("shows area load and validation errors in the areas workflow", async () => {
    const loadFailure = installFetch({ failAreasList: true });
    try {
      render(<Harness />);

      expect(await screen.findByText("Tasks for this property")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: "Areas" }));
      expect(await screen.findByRole("alert")).toHaveTextContent("You do not have permission to view areas.");
    } finally {
      loadFailure.restore();
    }

    cleanup();
    window.history.replaceState(null, "", "/");
    const saveFailure = installFetch({ failAreaPost: true });
    try {
      render(<Harness />);

      expect(await screen.findByText("Tasks for this property")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: "Areas" }));
      expect(await screen.findByText("Kitchen")).toBeInTheDocument();
      const createRow = screen.getByLabelText("New area");
      fireEvent.change(within(createRow).getByLabelText("Name"), {
        target: { value: "Pool" },
      });
      fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));

      expect(await screen.findByText("name must be a non-blank string")).toBeInTheDocument();
      expect(within(createRow).getByRole("button", { name: "Save" })).toBeInTheDocument();
      expect(screen.getByLabelText("Pool")).toBeInTheDocument();
    } finally {
      saveFailure.restore();
    }
  });

  it("switches implemented local tabs and links route-backed tabs to real pages", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Tasks for this property")).toBeInTheDocument();
      const settingsCallsBeforeSelection = fake.calls.filter((call) =>
        call.url === "/w/acme/api/v1/properties/prop_1/settings" || call.url === "/w/acme/api/v1/settings/catalog"
      );
      expect(settingsCallsBeforeSelection).toHaveLength(0);

      const tablist = screen.getByRole("tablist", { name: "Property sections" });
      expect(tablist).toHaveClass("page-tabs");
      expect(within(tablist).queryByRole("tab", { name: "Assets" })).not.toBeInTheDocument();
      expect(within(tablist).getByRole("tab", { name: "Documents" })).toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: "Sharing & client" }));
      expect(window.location.hash).toBe("#sharing");
      expect(await screen.findByText("Billing client")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: "Settings" }));
      expect(window.location.hash).toBe("#settings");
      expect(await screen.findByText("Settings overrides")).toBeInTheDocument();

      expect(screen.getByRole("link", { name: "Assets" })).toHaveAttribute("href", "/w/acme/property/prop_1/assets");
      expect(screen.getByRole("link", { name: "Stays" })).toHaveAttribute("href", "/w/acme/property/prop_1/stays");
      expect(screen.getByRole("link", { name: "Instructions" })).toHaveAttribute("href", "/w/acme/property/prop_1/instructions");
      expect(screen.getByRole("link", { name: "Closures" })).toHaveAttribute("href", "/w/acme/property/prop_1/closures");
      expect(screen.getByRole("link", { name: "Inventory" })).toHaveAttribute("href", "/w/acme/property/prop_1/inventory");
      expect(screen.getByRole("link", { name: "Schedules" })).toHaveAttribute("href", "/w/acme/property/prop_1/schedules");

      fireEvent.click(screen.getByRole("link", { name: "Assets" }));
      await waitFor(() => {
        expect(screen.getByText("Assets route reached")).toBeInTheDocument();
      });
      const assetsRelatedPages = screen.getByRole("navigation", { name: "Related property pages" });
      expect(within(assetsRelatedPages).getByRole("link", { name: "Assets" })).toHaveAttribute("aria-current", "page");
      expect(within(assetsRelatedPages).getByRole("link", { name: "Assets" })).toHaveClass("page-tabs__tab--active");

      cleanup();
      window.history.replaceState(null, "", "/");
      render(<Harness />);
      expect(await screen.findByText("Tasks for this property")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("link", { name: "Stays" }));
      await waitFor(() => {
        expect(screen.getByText("Stays route reached")).toBeInTheDocument();
      });
      const staysRelatedPages = screen.getByRole("navigation", { name: "Related property pages" });
      expect(within(staysRelatedPages).getByRole("link", { name: "Stays" })).toHaveAttribute("aria-current", "page");
      expect(within(staysRelatedPages).getByRole("link", { name: "Stays" })).toHaveClass("page-tabs__tab--active");

      cleanup();
      window.history.replaceState(null, "", "/");
      render(<Harness />);
      expect(await screen.findByText("Tasks for this property")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("link", { name: "Instructions" }));
      await waitFor(() => {
        expect(screen.getByText("Instructions route reached")).toBeInTheDocument();
      });
      const instructionsRelatedPages = screen.getByRole("navigation", { name: "Related property pages" });
      expect(within(instructionsRelatedPages).getByRole("link", { name: "Instructions" })).toHaveAttribute("aria-current", "page");
      expect(within(instructionsRelatedPages).getByRole("link", { name: "Instructions" })).toHaveClass("page-tabs__tab--active");

      cleanup();
      window.history.replaceState(null, "", "/");
      render(<Harness />);
      expect(await screen.findByText("Tasks for this property")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("link", { name: "Closures" }));
      await waitFor(() => {
        expect(screen.getByText("Closures route reached")).toBeInTheDocument();
      });

      cleanup();
      window.history.replaceState(null, "", "/");
      render(<Harness />);
      expect(await screen.findByText("Tasks for this property")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("link", { name: "Inventory" }));
      await waitFor(() => {
        expect(screen.getByText("Inventory route reached")).toBeInTheDocument();
      });
      const inventoryRelatedPages = screen.getByRole("navigation", { name: "Related property pages" });
      expect(within(inventoryRelatedPages).getByRole("link", { name: "Inventory" })).toHaveAttribute("aria-current", "page");
      expect(within(inventoryRelatedPages).getByRole("link", { name: "Inventory" })).toHaveClass("page-tabs__tab--active");

      cleanup();
      window.history.replaceState(null, "", "/");
      render(<Harness />);
      expect(await screen.findByText("Tasks for this property")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("link", { name: "Schedules" }));
      await waitFor(() => {
        expect(screen.getByText("Schedules route reached")).toBeInTheDocument();
      });
      const schedulesRelatedPages = screen.getByRole("navigation", { name: "Related property pages" });
      expect(within(schedulesRelatedPages).getByRole("link", { name: "Schedules" })).toHaveAttribute("aria-current", "page");
      expect(within(schedulesRelatedPages).getByRole("link", { name: "Schedules" })).toHaveClass("page-tabs__tab--active");
    } finally {
      fake.restore();
    }
  });

  it("loads hash-backed property tabs directly and treats legacy assets hash as overview", async () => {
    const fake = installFetch();
    try {
      window.history.replaceState(null, "", "/w/acme/property/prop_1#assets");
      render(<Harness initial="/w/acme/property/prop_1#assets" />);

      expect(await screen.findByRole("heading", { name: "Villa Rosa" })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
      expect(screen.queryByRole("tab", { name: "Assets" })).not.toBeInTheDocument();
      expect(screen.getByText("Tasks for this property")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("tab", { name: "Sharing & client" }));
      expect(window.location.hash).toBe("#sharing");
      expect(await screen.findByText("Billing client")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("tab", { name: "Settings" }));
      expect(window.location.hash).toBe("#settings");
      expect(await screen.findByText("Settings overrides")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("tab", { name: "Documents" }));
      expect(window.location.hash).toBe("#documents");
      expect(await screen.findByText("Pool permit")).toBeInTheDocument();

      window.history.back();
      await waitFor(() => expect(window.location.hash).toBe("#settings"));
      expect(screen.getByRole("tab", { name: "Settings" })).toHaveAttribute("aria-selected", "true");

      window.history.back();
      await waitFor(() => expect(window.location.hash).toBe("#sharing"));
      expect(screen.getByRole("tab", { name: "Sharing & client" })).toHaveAttribute("aria-selected", "true");

      window.history.forward();
      await waitFor(() => expect(window.location.hash).toBe("#settings"));
      expect(screen.getByRole("tab", { name: "Settings" })).toHaveAttribute("aria-selected", "true");

      expect(fake.calls.some((call) => call.url === "/w/acme/api/v1/properties/prop_1/settings")).toBe(true);
      expect(fake.calls.some((call) => call.url === "/w/acme/api/v1/settings/catalog")).toBe(true);
    } finally {
      fake.restore();
    }
  });

  it("renders property settings as editable InlineTableForm rows filtered to property scope", async () => {
    const fake = installFetch();
    try {
      window.history.replaceState(null, "", "/w/acme/property/prop_1#settings");
      render(<Harness initial="/w/acme/property/prop_1#settings" />);

      expect(await screen.findByRole("heading", { name: "Settings overrides" })).toBeInTheDocument();
      expect(
        screen.getByText("Property-scoped settings. Overridden values take precedence over workspace defaults."),
      ).toHaveClass("panel__sub");
      expect(screen.getByRole("table", { name: "Property settings overrides" })).toHaveClass("inline-table-form__table");
      expect(screen.queryByText("evidence_policy")).not.toBeInTheDocument();
      expect(screen.queryByText("require_photos")).not.toBeInTheDocument();
      expect(screen.queryByText("minimum_notice_hours")).not.toBeInTheDocument();
      expect(screen.queryByText("workspace_only")).not.toBeInTheDocument();
      const settingsTable = screen.getByRole("table", { name: "Property settings overrides" });
      expect(within(settingsTable).queryByRole("columnheader", { name: "Source" })).not.toBeInTheDocument();

      const evidence = settingRow("Evidence policy");
      expect(within(evidence).getByText("Evidence policy")).toHaveClass("setting-name__label");
      expect(within(evidence).getByText("Evidence requirement for work on this property.")).toHaveClass(
        "muted",
        "setting-name__description",
      );
      expect(within(evidence).getByText("Inherited")).toBeInTheDocument();
      expect(within(evidence).queryByText("inherited (workspace)")).not.toBeInTheDocument();

      fireEvent.click(within(evidence).getByRole("button", { name: "Edit" }));
      expect(within(evidence).getByText("Evidence policy")).toHaveClass("setting-name__label");
      expect(within(evidence).getByText("Evidence requirement for work on this property.")).toHaveClass(
        "muted",
        "setting-name__description",
      );
      expect(within(evidence).queryByText("evidence_policy")).not.toBeInTheDocument();
      expect(fake.calls).toContainEqual({
        url: "/w/acme/api/v1/properties/prop_1/settings",
        method: "GET",
        body: null,
      });
      expect(fake.calls.some((call) => call.url === "/w/acme/api/v1/settings" && call.method === "GET")).toBe(false);
    } finally {
      fake.restore();
    }
  });

  it("keeps standalone panel subtitles spaced from following table and form content", () => {
    expect(managerPanelsCss).toContain(
      ".panel > .panel__sub + :where(.inline-table-form, .form-layout, form, table, ul, ol, .settings-kv, .settings-list) {\n  margin-top: var(--space-6);\n}",
    );
    expect(managerPanelsCss).toContain(".panel__head-stack {\n  display: flex; flex-direction: column; gap: var(--space-2);");
  });

  it("renders existing property setting overrides distinctly without saving unchanged rows", async () => {
    const fake = installFetch({ settingsOverride: { require_photos: false } });
    try {
      window.history.replaceState(null, "", "/w/acme/property/prop_1#settings");
      render(<Harness initial="/w/acme/property/prop_1#settings" />);

      expect(await screen.findByRole("heading", { name: "Settings overrides" })).toBeInTheDocument();
      expect(
        within(screen.getByRole("table", { name: "Property settings overrides" })).queryByRole("columnheader", {
          name: "Source",
        }),
      ).not.toBeInTheDocument();
      const photos = settingRow("Require photos");
      expect(within(photos).getByText("property override")).toBeInTheDocument();
      expect(within(photos).getAllByText("no")).toHaveLength(2);

      fireEvent.click(within(photos).getByRole("button", { name: "Edit" }));
      expect(within(photos).getByRole("button", { name: "Save" })).toBeDisabled();
      fireEvent.click(within(photos).getByRole("button", { name: "Cancel" }));
      expect(fake.calls.some((call) => (
        call.url === "/w/acme/api/v1/properties/prop_1/settings" && call.method === "PATCH"
      ))).toBe(false);
    } finally {
      fake.restore();
    }
  });

  it("saves and clears property settings overrides without a page reload", async () => {
    const fake = installFetch();
    try {
      window.history.replaceState(null, "", "/w/acme/property/prop_1#settings");
      render(<Harness initial="/w/acme/property/prop_1#settings" />);

      expect(await screen.findByRole("heading", { name: "Settings overrides" })).toBeInTheDocument();
      const evidence = settingRow("Evidence policy");
      fireEvent.click(within(evidence).getByRole("button", { name: "Edit" }));
      const evidenceSelect = within(evidence).getByLabelText("Evidence policy");
      expect(within(evidenceSelect).getByRole("option", { name: "Inherited" })).toBeInTheDocument();
      fireEvent.change(evidenceSelect, {
        target: { value: "required" },
      });
      fireEvent.click(within(evidence).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(fake.calls).toContainEqual({
          url: "/w/acme/api/v1/properties/prop_1/settings",
          method: "PATCH",
          body: { evidence_policy: "required" },
        });
      });
      const savedEvidence = settingRow("Evidence policy");
      expect(await within(savedEvidence).findByText("property override")).toBeInTheDocument();
      expect(within(savedEvidence).getAllByText("required")).toHaveLength(2);

      fireEvent.click(within(savedEvidence).getByRole("button", { name: "Edit" }));
      const savedEvidenceSelect = within(savedEvidence).getByLabelText("Evidence policy");
      const inheritedOption = within(savedEvidenceSelect).getByRole("option", { name: "Inherited" }) as HTMLOptionElement;
      fireEvent.change(savedEvidenceSelect, {
        target: { value: inheritedOption.value },
      });
      fireEvent.click(within(savedEvidence).getByRole("button", { name: "Save" }));
      await waitFor(() => {
        expect(fake.calls).toContainEqual({
          url: "/w/acme/api/v1/properties/prop_1/settings",
          method: "PATCH",
          body: { evidence_policy: null },
        });
      });
      const inheritedEvidence = settingRow("Evidence policy");
      expect(await within(inheritedEvidence).findByText("Inherited")).toBeInTheDocument();
      expect(within(inheritedEvidence).queryByText("inherited (workspace)")).not.toBeInTheDocument();
      await waitFor(() => {
        expect(fake.calls.filter((call) => call.url === "/w/acme/api/v1/properties/prop_1/settings").length).toBeGreaterThan(1);
        expect(fake.calls.filter((call) => call.url === "/w/acme/api/v1/properties/prop_1" && call.method === "GET").length).toBeGreaterThan(1);
        expect(fake.calls.filter((call) => call.url === "/w/acme/api/v1/properties" && call.method === "GET").length).toBeGreaterThan(1);
      });

      fireEvent.click(within(settingRow("Evidence policy")).getByRole("button", { name: "Edit" }));
      fireEvent.change(within(settingRow("Evidence policy")).getByLabelText("Evidence policy"), {
        target: { value: "required" },
      });
      fireEvent.click(within(settingRow("Evidence policy")).getByRole("button", { name: "Save" }));
      expect(await within(settingRow("Evidence policy")).findByText("property override")).toBeInTheDocument();

      fireEvent.click(within(settingRow("Evidence policy")).getByRole("button", { name: "Clear" }));
      await waitFor(() => {
        expect(fake.calls).toContainEqual({
          url: "/w/acme/api/v1/properties/prop_1/settings",
          method: "PATCH",
          body: { evidence_policy: null },
        });
      });
      const clearedEvidence = settingRow("Evidence policy");
      expect(await within(clearedEvidence).findByText("Inherited")).toBeInTheDocument();
      expect(within(clearedEvidence).queryByText("inherited (workspace)")).not.toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("saves bool property setting overrides", async () => {
    const fake = installFetch();
    try {
      window.history.replaceState(null, "", "/w/acme/property/prop_1#settings");
      render(<Harness initial="/w/acme/property/prop_1#settings" />);

      expect(await screen.findByRole("heading", { name: "Settings overrides" })).toBeInTheDocument();
      const photos = settingRow("Require photos");
      fireEvent.click(within(photos).getByRole("button", { name: "Edit" }));
      fireEvent.change(within(photos).getByLabelText("Require photos"), {
        target: { value: "false" },
      });
      fireEvent.click(within(photos).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(fake.calls).toContainEqual({
          url: "/w/acme/api/v1/properties/prop_1/settings",
          method: "PATCH",
          body: { require_photos: false },
        });
      });
      const savedPhotos = settingRow("Require photos");
      expect(await within(savedPhotos).findByText("property override")).toBeInTheDocument();
      expect(within(savedPhotos).getAllByText("no")).toHaveLength(2);
    } finally {
      fake.restore();
    }
  });

  it("keeps property setting row validation and save errors attached to the draft", async () => {
    const fake = installFetch({ failPropertySettingsPatch: true });
    try {
      window.history.replaceState(null, "", "/w/acme/property/prop_1#settings");
      render(<Harness initial="/w/acme/property/prop_1#settings" />);

      expect(await screen.findByRole("heading", { name: "Settings overrides" })).toBeInTheDocument();
      const notice = settingRow("Minimum notice");
      fireEvent.click(within(notice).getByRole("button", { name: "Edit" }));
      fireEvent.change(within(notice).getByLabelText("Minimum notice"), {
        target: { value: "soon" },
      });
      expect(within(notice).getByText("Enter a whole number.")).toBeInTheDocument();
      fireEvent.click(within(notice).getByRole("button", { name: "Save" }));
      expect(fake.calls.some((call) => call.method === "PATCH" && call.url === "/w/acme/api/v1/properties/prop_1/settings")).toBe(false);

      fireEvent.change(within(notice).getByLabelText("Minimum notice"), {
        target: { value: "48" },
      });
      fireEvent.click(within(notice).getByRole("button", { name: "Save" }));

      expect(await within(notice).findByRole("alert")).toHaveTextContent("Setting update rejected.");
      expect(within(notice).getByLabelText("Minimum notice")).toHaveValue("48");
    } finally {
      fake.restore();
    }
  });

  it("loads property documents from the hash-backed tab and uploads property-owned files", async () => {
    const fake = installFetch();
    try {
      window.history.replaceState(null, "", "/w/acme/property/prop_1#documents");
      const { container } = render(<Harness initial="/w/acme/property/prop_1#documents" />);

      expect(await screen.findByRole("heading", { name: "Villa Rosa" })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: "Documents" })).toHaveAttribute("aria-selected", "true");
      expect(await screen.findByText("Pool permit")).toBeInTheDocument();
      expect(screen.getByText("pool-permit.pdf")).toBeInTheDocument();
      expect(screen.queryByText("Casa Azul insurance")).not.toBeInTheDocument();
      expect(container.querySelector(".doc-thumb")).toBeNull();
      expect(fake.calls).toContainEqual({
        url: "/w/acme/api/v1/properties/prop_1/documents",
        method: "GET",
        body: null,
      });

      fireEvent.click(screen.getByText("indexed"));
      expect(await screen.findByText("pypdf")).toBeInTheDocument();
      fireEvent.click(screen.getByText("Extracted text"));
      expect(await screen.findByText("Pool permit notes.")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await waitFor(() => {
        expect(
          fake.calls.filter((call) => call.url === "/w/acme/api/v1/properties/prop_1/documents" && call.method === "GET"),
        ).toHaveLength(2);
      });

      fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "insurance" } });
      fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Umbrella insurance" } });
      fireEvent.change(screen.getByLabelText("Document notes"), { target: { value: "Policy renewal" } });
      const upload = new File(["policy"], "umbrella.pdf", { type: "application/pdf" });
      fireEvent.change(screen.getByLabelText("Upload property document"), {
        target: { files: [upload] },
      });
      expect(await screen.findByText("umbrella.pdf")).toBeInTheDocument();
      expect(screen.getByLabelText("Kind for umbrella.pdf")).toHaveValue("insurance");
      expect(screen.getByLabelText("Title for umbrella.pdf")).toHaveValue("Umbrella insurance");
      expect(screen.getByLabelText("Notes for umbrella.pdf")).toHaveValue("Policy renewal");
      fireEvent.click(screen.getByRole("button", { name: "Upload" }));

      await screen.findByText("Umbrella insurance");
      const post = fake.calls.find(
        (call) => call.url === "/w/acme/api/v1/properties/prop_1/documents" && call.method === "POST",
      );
      expect(post?.body).toBeInstanceOf(FormData);
      const form = post!.body as FormData;
      expect(form.get("category")).toBe("insurance");
      expect(form.get("title")).toBe("Umbrella insurance");
      expect(form.get("notes_md")).toBe("Policy renewal");
      expect(form.get("file")).toBe(upload);
      expect(
        fake.calls.filter((call) => call.url === "/w/acme/api/v1/properties/prop_1/documents" && call.method === "GET"),
      ).toHaveLength(3);
    } finally {
      fake.restore();
    }
  });

  it("uses the access denied pattern when property documents are not authorised", async () => {
    const fake = installFetch({ denyPropertyDocuments: true });
    try {
      window.history.replaceState(null, "", "/w/acme/property/prop_1#documents");
      render(<Harness initial="/w/acme/property/prop_1#documents" />);

      expect(await screen.findByRole("heading", { name: "Villa Rosa" })).toBeInTheDocument();
      expect(await screen.findByRole("alert")).toHaveTextContent("You do not have permission to open this page.");
      expect(screen.queryByLabelText("Upload property document")).not.toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("falls back to overview for absent or invalid hashes when the property id changes", async () => {
    const fake = installFetch();
    try {
      window.history.replaceState(null, "", "/w/acme/property/prop_1#settings");
      render(<SwitchingHarness />);

      expect(await screen.findByText("Settings overrides")).toBeInTheDocument();
      window.history.replaceState(null, "", "/w/acme/property/prop_2");
      fireEvent.click(screen.getByRole("link", { name: "Next property" }));

      expect(await screen.findByRole("heading", { name: "Casa Azul" })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
      expect(screen.getByText("Tasks for this property")).toBeInTheDocument();

      window.history.replaceState(null, "", "/w/acme/property/prop_2#unknown");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
      expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    } finally {
      fake.restore();
    }
  });
});
