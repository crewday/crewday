import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests, registerWorkspaceSlugGetter } from "@/lib/api";
import {
  __resetQueryKeyGetterForTests,
  qk,
  registerQueryKeyWorkspaceGetter,
} from "@/lib/queryKeys";
import { installFetchRouteHandlers, type FetchRoute } from "@/test/helpers";
import type { AssetType, Me } from "@/types/api";
import AssetTypesPage from "./AssetTypesPage";
import appSource from "../../App.tsx?raw";

const originalShowModal = HTMLDialogElement.prototype.showModal;
const originalClose = HTMLDialogElement.prototype.close;

const ME: Me = {
  role: "manager",
  theme: "system",
  agent_sidebar_collapsed: false,
  employee: {
    id: "emp_1",
    name: "Mina Manager",
    roles: ["manager"],
    properties: [],
    avatar_initials: "MM",
    avatar_file_id: null,
    avatar_url: null,
    phone: "",
    email: "mina@example.test",
    started_on: "",
    capabilities: {},
    workspaces: ["ws_1"],
    villas: [],
    language: "en",
    weekly_availability: {},
    evidence_policy: "inherit",
    preferred_locale: null,
    settings_override: {},
  },
  manager_name: "Mina",
  today: "2026-05-05",
  now: "2026-05-05T10:00:00Z",
  user_id: "usr_1",
  agent_approval_mode: "auto",
  current_workspace_id: "ws_1",
  available_workspaces: [],
  client_binding_org_ids: [],
  is_deployment_admin: false,
  is_deployment_owner: false,
};

const SYSTEM_TYPE: AssetType = {
  id: "type_system_fire",
  workspace_id: null,
  key: "fire_extinguisher",
  name: "Fire extinguisher",
  category: "safety",
  icon_name: "Flame",
  description_md: "Portable fire safety equipment.",
  default_actions: [
    {
      kind: "inspect",
      label: "Visual inspection",
      interval_days: 30,
      warn_before_days: 7,
    },
  ],
  default_actions_json: [
    {
      kind: "inspect",
      label: "Visual inspection",
      interval_days: 30,
      warn_before_days: 7,
    },
  ],
  default_lifespan_years: 12,
  created_at: "2026-04-29T12:00:00Z",
  updated_at: "2026-04-29T12:00:00Z",
  deleted_at: null,
  archived_at: null,
  is_system: true,
};

const CUSTOM_TYPE: AssetType = {
  id: "type_lock",
  workspace_id: "ws_1",
  key: "lock",
  name: "Smart lock",
  category: "security",
  icon_name: "Lock",
  description_md: "Connected entry locks.",
  default_actions: [
    {
      kind: "inspect",
      label: "Battery check",
      interval_days: 30,
      warn_before_days: 7,
    },
    {
      kind: "inspect",
      label: "Battery check",
      interval_days: 30,
      warn_before_days: 7,
    },
  ],
  default_actions_json: [
    {
      kind: "inspect",
      label: "Battery check",
      interval_days: 30,
      warn_before_days: 7,
    },
    {
      kind: "inspect",
      label: "Battery check",
      interval_days: 30,
      warn_before_days: 7,
    },
  ],
  default_lifespan_years: 5,
  created_at: "2026-04-29T12:00:00Z",
  updated_at: "2026-04-29T12:00:00Z",
  deleted_at: null,
  archived_at: null,
  is_system: false,
};

function renderAssetTypes(
  routes: FetchRoute[] = [],
  options: { permission?: "allow" | "deny"; initialTypes?: AssetType[] } = {},
) {
  const types = [...(options.initialTypes ?? [SYSTEM_TYPE, CUSTOM_TYPE])];
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  qc.setQueryData(qk.assets(), { data: [] });
  const fetchEnv = installFetchRouteHandlers([
    ...routes,
    { path: "/w/acme/api/v1/me", respond: { body: ME } },
    {
      path: "/w/acme/api/v1/permissions/resolved/self?action_key=assets.manage_types&scope_kind=workspace&scope_id=ws_1",
      respond: {
        body: {
          effect: options.permission ?? "allow",
          source_layer: "default_allow",
          source_rule_id: null,
          matched_groups: options.permission === "deny" ? [] : ["managers"],
        },
      },
    },
    {
      path: "/w/acme/api/v1/asset_types",
      respond: () => ({ body: { data: types, next_cursor: null, has_more: false } }),
    },
  ]);
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/w/acme/asset_types"]}>
        <WorkspaceProvider>
          <AssetTypesPage />
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, ...fetchEnv, qc, types };
}

async function findCatalog(): Promise<HTMLElement> {
  const catalog = await screen.findByRole("region", { name: "Asset types" });
  await within(catalog).findByRole("table", { name: "Asset type catalog" });
  return catalog;
}

function installDialogPolyfill(): void {
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  });
}

beforeEach(() => {
  installDialogPolyfill();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  registerWorkspaceSlugGetter(() => "acme");
  registerQueryKeyWorkspaceGetter(() => "acme");
});

afterEach(() => {
  cleanup();
  HTMLDialogElement.prototype.showModal = originalShowModal;
  HTMLDialogElement.prototype.close = originalClose;
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<AssetTypesPage>", () => {
  it("gates the asset type route before asset type content can render", () => {
    expect(appSource).toMatch(
      /<Route element={<RequirePermission actionKey="scope\.view" \/>}>\s*(?:(?!<\/Route>)[\s\S])*?<Route path="asset_types" element={<AssetTypesPage \/>} \/>/,
    );
  });

  it("also wires the workspace-scoped asset type route", () => {
    expect(appSource).toContain(
      '<Route path="/w/:slug" element={<WorkspaceRouteRoot />}>',
    );
    expect(appSource).toContain('<Route path="asset_types" element={<AssetTypesPage />} />');
  });

  it("renders an inline table with locked system rows and a create row for managers", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    renderAssetTypes();

    const catalog = await findCatalog();
    expect(within(catalog).queryByRole("article")).not.toBeInTheDocument();
    expect(within(catalog).getByText("Fire extinguisher")).toBeInTheDocument();
    expect(within(catalog).getByText("Smart lock")).toBeInTheDocument();
    expect(within(catalog).getAllByText("Battery check")).toHaveLength(2);
    expect(within(catalog).getAllByText("every 30d")).toHaveLength(3);
    expect(within(catalog).getByText(/System type/)).toBeInTheDocument();
    expect(within(catalog).getByLabelText("New asset type")).toBeInTheDocument();
    const systemRow = within(catalog).getByLabelText("Fire extinguisher");
    expect(within(systemRow).getByRole("button", { name: "Edit" })).toBeDisabled();
    expect(within(systemRow).getByRole("button", { name: "Archive" })).toBeDisabled();
    expect(within(systemRow).queryByRole("button", { name: "Add action" })).not.toBeInTheDocument();
    expect(within(systemRow).getByLabelText("Locked")).toBeInTheDocument();
    const consoleText = consoleError.mock.calls.flat().join("\n");
    expect(consoleText).not.toContain('Each child in a list should have a unique "key" prop');
  });

  it("shows catalog rows without mutation affordances for view-only users", async () => {
    renderAssetTypes([], { permission: "deny" });

    const catalog = await findCatalog();
    expect(within(catalog).getByText("Smart lock")).toBeInTheDocument();
    expect(within(catalog).getAllByText("Battery check")).toHaveLength(2);
    expect(within(catalog).queryByLabelText("New asset type")).not.toBeInTheDocument();
    expect(within(catalog).queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(within(catalog).queryByRole("button", { name: "Archive" })).not.toBeInTheDocument();
    expect(within(catalog).queryByRole("button", { name: "Add action" })).not.toBeInTheDocument();
    expect(within(catalog).getByText(/do not have permission to manage asset types/i)).toBeInTheDocument();
  });

  it("creates a workspace-custom asset type and invalidates dependent catalog queries", async () => {
    const { requests, types, qc } = renderAssetTypes([
      {
        path: "/w/acme/api/v1/asset_types",
        method: "POST",
        respond: ({ body }) => {
          const created = {
            ...CUSTOM_TYPE,
            ...(body as Partial<AssetType>),
            id: "type_pool_heater",
            workspace_id: "ws_1",
            created_at: "2026-05-05T00:00:00Z",
            updated_at: "2026-05-05T00:00:00Z",
            deleted_at: null,
            archived_at: null,
            is_system: false,
          } satisfies AssetType;
          types.push(created);
          return { status: 201, body: created };
        },
      },
    ]);

    const catalog = await findCatalog();
    const createRow = within(catalog).getByLabelText("New asset type");
    fireEvent.change(within(createRow).getByLabelText("Name"), { target: { value: "Pool heater" } });
    fireEvent.change(within(createRow).getByLabelText("Key"), { target: { value: "pool_heater" } });
    fireEvent.change(within(createRow).getByLabelText("Category"), { target: { value: "pool" } });
    fireEvent.change(within(createRow).getByLabelText("Default lifespan years"), { target: { value: "10" } });
    fireEvent.change(within(createRow).getByLabelText("Description"), {
      target: { value: "Outdoor pool heating equipment." },
    });
    fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(requests.some((request) => request.method === "POST" && request.path === "/w/acme/api/v1/asset_types")).toBe(true);
    });
    const createRequest = requests.find((request) => request.method === "POST" && request.path === "/w/acme/api/v1/asset_types");
    expect(createRequest?.body).toEqual({
      key: "pool_heater",
      name: "Pool heater",
      category: "pool",
      icon_name: null,
      description_md: "Outdoor pool heating equipment.",
      default_lifespan_years: 10,
      default_actions: [],
    });
    expect(await screen.findByText("Pool heater")).toBeInTheDocument();
    expect(within(screen.getByLabelText("New asset type")).getByLabelText("Name")).toHaveValue("");
    expect(requests.filter((request) => request.path === "/w/acme/api/v1/asset_types").length).toBeGreaterThan(1);
    expect(qc.getQueryState(qk.assets())?.isInvalidated).toBe(true);
  });

  it("edits supported fields on a workspace-custom row while preserving default actions", async () => {
    const { requests, types } = renderAssetTypes([
      {
        path: "/w/acme/api/v1/asset_types/type_lock",
        method: "PATCH",
        respond: ({ body }) => {
          types[1] = { ...types[1]!, ...(body as Partial<AssetType>) };
          return { body: types[1] };
        },
      },
    ]);

    const catalog = await findCatalog();
    const row = within(catalog).getByLabelText("Smart lock");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(row).getByLabelText("Name"), { target: { value: "Smart entry lock" } });
    fireEvent.change(within(row).getByLabelText("Category"), { target: { value: "safety" } });
    fireEvent.change(within(row).getByLabelText("Default lifespan years"), { target: { value: "6" } });
    fireEvent.change(within(row).getByLabelText("Description"), { target: { value: "Updated locks." } });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Smart entry lock")).toBeInTheDocument();
    const patchRequest = requests.find((request) => request.method === "PATCH");
    expect(patchRequest?.body).toEqual({
      key: "lock",
      name: "Smart entry lock",
      category: "safety",
      icon_name: "Lock",
      description_md: "Updated locks.",
      default_lifespan_years: 6,
    });
    expect(patchRequest?.body).not.toHaveProperty("default_actions");
    expect(screen.getAllByText("Battery check")).toHaveLength(2);
  });

  it("adds edits and removes default action templates inline for workspace-custom rows", async () => {
    const { requests, types } = renderAssetTypes([
      {
        path: "/w/acme/api/v1/asset_types/type_lock",
        method: "PATCH",
        respond: ({ body }) => {
          types[1] = { ...types[1]!, ...(body as Partial<AssetType>) };
          return { body: types[1] };
        },
      },
    ]);

    const catalog = await findCatalog();
    const row = within(catalog).getByLabelText("Smart lock");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(row).getByLabelText("Default action 1 kind"), {
      target: { value: "service" },
    });
    fireEvent.change(within(row).getByLabelText("Default action 1 label"), {
      target: { value: "Replace batteries" },
    });
    fireEvent.change(within(row).getByLabelText("Default action 1 interval days"), {
      target: { value: "90" },
    });
    fireEvent.change(within(row).getByLabelText("Default action 1 warn before days"), {
      target: { value: "14" },
    });
    fireEvent.click(within(row).getByRole("button", { name: "Remove default action 2" }));
    fireEvent.click(within(row).getByRole("button", { name: "Add action" }));
    fireEvent.change(within(row).getByLabelText("Default action 2 kind"), {
      target: { value: "replace" },
    });
    fireEvent.change(within(row).getByLabelText("Default action 2 label"), {
      target: { value: "Replace keypad" },
    });
    fireEvent.change(within(row).getByLabelText("Default action 2 interval days"), {
      target: { value: "365" },
    });
    fireEvent.change(within(row).getByLabelText("Default action 2 warn before days"), {
      target: { value: "30" },
    });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Replace batteries")).toBeInTheDocument();
    const patchRequest = requests.find((request) => request.method === "PATCH");
    expect(patchRequest?.body).toMatchObject({
      key: "lock",
      name: "Smart lock",
      category: "security",
      icon_name: "Lock",
      description_md: "Connected entry locks.",
      default_lifespan_years: 5,
      default_actions: [
        {
          kind: "service",
          label: "Replace batteries",
          interval_days: 90,
          warn_before_days: 14,
        },
        {
          kind: "replace",
          label: "Replace keypad",
          interval_days: 365,
          warn_before_days: 30,
        },
      ],
    });
    expect(screen.getByText("Replace keypad")).toBeInTheDocument();
  });

  it("keeps invalid default action drafts inline until managers fix them", async () => {
    const { requests } = renderAssetTypes();

    const catalog = await findCatalog();
    const row = within(catalog).getByLabelText("Smart lock");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(row).getByLabelText("Default action 1 label"), {
      target: { value: "" },
    });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    expect(await within(row).findByText("Default action 1: enter a label.")).toBeInTheDocument();
    expect(within(row).getByLabelText("Default action 1 label")).toHaveValue("");
    expect(requests.some((request) => request.method === "PATCH")).toBe(false);
  });

  it("renders backend default action validation errors without losing the edited draft", async () => {
    renderAssetTypes([
      {
        path: "/w/acme/api/v1/asset_types/type_lock",
        method: "PATCH",
        respond: {
          status: 422,
          body: {
            type: "https://crewday.dev/errors/validation",
            title: "Validation error",
            status: 422,
            detail: "Invalid asset type",
            errors: [
              {
                loc: ["body", "default_actions", 0, "interval_days"],
                msg: "interval_days must be less than 3650",
                type: "value_error",
              },
            ],
          },
        },
      },
    ]);

    const catalog = await findCatalog();
    const row = within(catalog).getByLabelText("Smart lock");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(row).getByLabelText("Default action 1 label"), {
      target: { value: "Backend rejected action" },
    });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    expect(await within(row).findByRole("alert")).toHaveTextContent(
      "Could not save asset type. interval_days must be less than 3650",
    );
    expect(within(row).getByText("interval_days must be less than 3650")).toBeInTheDocument();
    expect(within(row).getByLabelText("Default action 1 label")).toHaveValue("Backend rejected action");
  });

  it("confirms archive behavior before deleting a workspace-custom row", async () => {
    const { requests, types } = renderAssetTypes([
      {
        path: "/w/acme/api/v1/asset_types/type_lock",
        method: "DELETE",
        respond: () => {
          types.splice(1, 1);
          return { status: 204, body: null };
        },
      },
    ]);

    const catalog = await findCatalog();
    const row = within(catalog).getByLabelText("Smart lock");
    fireEvent.click(within(row).getByRole("button", { name: "Archive" }));
    const dialog = screen.getByRole("alertdialog", { name: "Archive asset type?" });
    expect(dialog).toHaveTextContent("Unused custom types may disappear immediately");
    expect(dialog).toHaveTextContent("referenced types are retained");
    fireEvent.click(within(dialog).getByRole("button", { name: "Archive type" }));

    await waitFor(() => {
      expect(requests.some((request) => request.method === "DELETE")).toBe(true);
    });
  });

  it("keeps create drafts visible when duplicate-key errors return from the backend", async () => {
    renderAssetTypes([
      {
        path: "/w/acme/api/v1/asset_types",
        method: "POST",
        respond: {
          status: 409,
          body: {
            type: "https://crewday.dev/errors/conflict",
            title: "Conflict",
            status: 409,
            detail: "asset type key 'lock' already exists in this workspace",
            error: "asset_type_key_conflict",
          },
        },
      },
    ]);

    const catalog = await findCatalog();
    const createRow = within(catalog).getByLabelText("New asset type");
    fireEvent.change(within(createRow).getByLabelText("Name"), { target: { value: "Duplicate lock" } });
    fireEvent.change(within(createRow).getByLabelText("Key"), { target: { value: "lock" } });
    fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));

    expect(await within(createRow).findByRole("alert")).toHaveTextContent(
      "That asset type key is already used. Choose a unique key.",
    );
    expect(within(createRow).getByLabelText("Name")).toHaveValue("Duplicate lock");
    expect(within(createRow).getByLabelText("Key")).toHaveValue("lock");
  });

  it("surfaces stale mutation permission failures as inline row errors", async () => {
    renderAssetTypes([
      {
        path: "/w/acme/api/v1/asset_types/type_lock",
        method: "PATCH",
        respond: {
          status: 403,
          body: {
            type: "https://crewday.dev/errors/forbidden",
            title: "Forbidden",
            status: 403,
            detail: "Forbidden",
          },
        },
      },
    ]);

    const catalog = await findCatalog();
    const row = within(catalog).getByLabelText("Smart lock");
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(row).getByLabelText("Name"), { target: { value: "Denied lock" } });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    expect(await within(row).findByRole("alert")).toHaveTextContent(
      "You do not have permission to manage asset types.",
    );
    expect(within(row).getByLabelText("Name")).toHaveValue("Denied lock");
  });

  it("renders bare list responses for mock parity", async () => {
    renderAssetTypes([
      {
        path: "/w/acme/api/v1/asset_types",
        respond: { body: [SYSTEM_TYPE, CUSTOM_TYPE] },
      },
    ], { initialTypes: [] });

    expect(await screen.findByText("Fire extinguisher")).toBeInTheDocument();
    expect(screen.getByText("Smart lock")).toBeInTheDocument();
  });

  it("shows the loading and error states", async () => {
    renderAssetTypes([
      {
        path: "/w/acme/api/v1/asset_types",
        respond: { status: 500, body: { detail: "nope" } },
      },
    ], { initialTypes: [] });

    expect(screen.getByText(/Loading/)).toBeInTheDocument();
    expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
  });
});
