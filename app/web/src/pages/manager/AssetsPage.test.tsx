import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import type { Asset, AssetType, Property } from "@/types/api";
import AssetsPage from "./AssetsPage";
import appSource from "../../App.tsx?raw";
import managerPanelsCss from "@/styles/manager-panels.css?raw";
import { jsonResponse } from "@/test/helpers";

const originalShowModal = HTMLDialogElement.prototype.showModal;
const originalClose = HTMLDialogElement.prototype.close;

const ASSET_TYPES: AssetType[] = [
  {
    id: "type_lock",
    key: "lock",
    name: "Smart lock",
    category: "security",
    icon_name: "lock",
    default_actions: [],
    default_lifespan_years: 5,
  },
  {
    id: "type_pump",
    key: "pump",
    name: "Pool pump",
    category: "pool",
    icon_name: "waves",
    default_actions: [],
    default_lifespan_years: 8,
  },
];

const PROPERTIES: Property[] = [
  {
    id: "prop_1",
    name: "Villa Rosa",
    city: "Porto",
    timezone: "Europe/Lisbon",
    color: "moss",
    kind: "str",
    areas: ["Entry"],
    evidence_policy: "inherit",
    country: "PT",
    locale: "pt-PT",
    settings_override: {},
    client_org_id: null,
    owner_user_id: null,
  },
  {
    id: "prop_2",
    name: "Casa Azul",
    city: "Lisbon",
    timezone: "Europe/Lisbon",
    color: "sky",
    kind: "residence",
    areas: ["Pool"],
    evidence_policy: "inherit",
    country: "PT",
    locale: "pt-PT",
    settings_override: {},
    client_org_id: null,
    owner_user_id: null,
  },
];

const ASSETS: Asset[] = [
  {
    id: "asset_lock",
    property_id: "prop_1",
    asset_type_id: "type_lock",
    name: "Front door lock",
    area: "Entry",
    condition: "good",
    status: "active",
    make: "Nuki",
    model: "Pro",
    serial_number: null,
    installed_on: null,
    purchased_on: null,
    purchase_price_cents: null,
    purchase_currency: null,
    purchase_vendor: null,
    warranty_expires_on: null,
    expected_lifespan_years: null,
    guest_visible: true,
    guest_instructions: null,
    notes: null,
    qr_token: "qr_lock",
  },
  {
    id: "asset_pump",
    property_id: "prop_2",
    asset_type_id: "type_pump",
    name: "Pool pump",
    area: "Pool",
    condition: "fair",
    status: "in_repair",
    make: null,
    model: null,
    serial_number: null,
    installed_on: null,
    purchased_on: null,
    purchase_price_cents: null,
    purchase_currency: null,
    purchase_vendor: null,
    warranty_expires_on: null,
    expected_lifespan_years: null,
    guest_visible: false,
    guest_instructions: null,
    notes: null,
    qr_token: "qr_pump",
  },
];

interface FetchRequest {
  path: string;
  method: string;
  body: unknown;
}

interface InstallFetchOptions {
  createStatus?: number;
  createBody?: unknown;
}

function parseRequestBody(body: BodyInit | null | undefined): unknown {
  if (typeof body !== "string") return body ?? null;
  try {
    return JSON.parse(body);
  } catch {
    return body;
  }
}

function createdAsset(body: Record<string, unknown>): Asset {
  return {
    id: "asset_grill",
    property_id: String(body.property_id),
    asset_type_id:
      typeof body.asset_type_id === "string" ? body.asset_type_id : null,
    name: String(body.name),
    area: body.area_id === "area_entry" ? "Entry" : null,
    condition: body.condition === "fair" ? "fair" : "good",
    status: body.status === "in_repair" ? "in_repair" : "active",
    make: typeof body.make === "string" ? body.make : null,
    model: typeof body.model === "string" ? body.model : null,
    serial_number:
      typeof body.serial_number === "string" ? body.serial_number : null,
    installed_on: typeof body.installed_on === "string" ? body.installed_on : null,
    purchased_on: typeof body.purchased_on === "string" ? body.purchased_on : null,
    purchase_price_cents:
      typeof body.purchase_price_cents === "number" ? body.purchase_price_cents : null,
    purchase_currency:
      typeof body.purchase_currency === "string" ? body.purchase_currency : null,
    purchase_vendor:
      typeof body.purchase_vendor === "string" ? body.purchase_vendor : null,
    warranty_expires_on:
      typeof body.warranty_expires_on === "string" ? body.warranty_expires_on : null,
    expected_lifespan_years:
      typeof body.expected_lifespan_years === "number" ? body.expected_lifespan_years : null,
    guest_visible: body.guest_visible === true,
    guest_instructions:
      typeof body.guest_instructions_md === "string" ? body.guest_instructions_md : null,
    notes: typeof body.notes_md === "string" ? body.notes_md : null,
    qr_token: "qr_grill",
  };
}

function installFetch(options: InstallFetchOptions = {}) {
  const original = globalThis.fetch;
  const assets = [...ASSETS];
  const requests: FetchRequest[] = [];
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const method = (init?.method ?? "GET").toUpperCase();
    const path = new URL(resolved, "http://crewday.test").pathname;
    const body = parseRequestBody(init?.body);
    requests.push({ path, method, body });
    if (path === "/w/acme/api/v1/assets" && method === "GET") {
      return jsonResponse({ data: assets });
    }
    if (path === "/w/acme/api/v1/assets" && method === "POST") {
      if (options.createStatus) {
        return jsonResponse(options.createBody, options.createStatus);
      }
      const asset = createdAsset(body as Record<string, unknown>);
      assets.push(asset);
      return jsonResponse(asset, 201);
    }
    if (path === "/w/acme/api/v1/asset_types") {
      return jsonResponse({ data: ASSET_TYPES });
    }
    if (path === "/w/acme/api/v1/properties") return jsonResponse(PROPERTIES);
    if (path === "/w/acme/api/v1/properties/prop_1/areas") {
      return jsonResponse({ data: [{ id: "area_entry", name: "Entry" }] });
    }
    if (path === "/w/acme/api/v1/properties/prop_2/areas") {
      return jsonResponse({ data: [{ id: "area_pool", name: "Pool" }] });
    }
    throw new Error(`Unexpected fetch call: ${resolved}`);
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    requests,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function Harness({ initial = "/w/acme/assets" }: { initial?: string }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <WorkspaceProvider>
        <MemoryRouter initialEntries={[initial]}>
          <AssetsPage />
        </MemoryRouter>
      </WorkspaceProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  });
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
});

afterEach(() => {
  cleanup();
  HTMLDialogElement.prototype.showModal = originalShowModal;
  HTMLDialogElement.prototype.close = originalClose;
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<AssetsPage>", () => {
  it("wraps the assets routes in the scope-view permission guard", () => {
    expect(appSource).toMatch(
      /<Route element={<RequirePermission actionKey="scope\.view" \/>}>\s*<Route element={<ManagerLayout \/>}>\s*<Route path="assets" element={<AssetsPage \/>} \/>/,
    );
  });

  it("renders assets from paginated API envelopes and filters from the URL", async () => {
    const { restore } = installFetch();
    try {
      render(<Harness initial="/w/acme/assets?category=security" />);

      expect(await screen.findByText("Front door lock")).toBeInTheDocument();
      const table = screen.getByRole("table");
      expect(within(table).queryByRole("link", { name: /Pool pump/ })).not.toBeInTheDocument();
      expect(within(table).getByText("Smart lock")).toBeInTheDocument();
      expect(screen.getAllByText("Villa Rosa").length).toBeGreaterThan(0);
    } finally {
      restore();
    }
  });

  it("opens the QR sheet for the active filters", async () => {
    const { restore } = installFetch();
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    try {
      render(<Harness initial="/w/acme/assets?category=security&property_id=prop_1" />);
      await screen.findByText("Front door lock");

      fireEvent.click(screen.getByRole("button", { name: "Print QR labels" }));

      expect(open).toHaveBeenCalledWith(
        "/w/acme/api/v1/assets/qr-sheet?category=security&property_id=prop_1",
        "_blank",
        "noopener,noreferrer",
      );
    } finally {
      restore();
    }
  });

  it("updates property filters through search params", async () => {
    const { restore } = installFetch();
    try {
      render(<Harness />);
      await screen.findByText("Front door lock");

      const casaFilter = screen
        .getAllByText("Casa Azul")
        .find((element) => element.parentElement?.className === "desk-filters");
      expect(casaFilter).toBeDefined();
      fireEvent.click(casaFilter!);

      const table = screen.getByRole("table");
      await within(table).findByRole("link", { name: /Pool pump/ });
      expect(within(table).queryByRole("link", { name: /Front door lock/ })).not.toBeInTheDocument();
    } finally {
      restore();
    }
  });

  it("opens the new asset flow, validates required fields, and creates an asset", async () => {
    const { requests, restore } = installFetch();
    try {
      render(<Harness />);
      await screen.findByText("Front door lock");

      fireEvent.click(screen.getByRole("button", { name: "+ New asset" }));
      const dialog = screen.getByRole("dialog", { name: "New asset" });
      expect(dialog).toHaveClass("asset-create-dialog");
      expect(
        within(dialog).getByRole("heading", { name: "Basics and location" }),
      ).toBeInTheDocument();
      expect(
        within(dialog).getByRole("heading", { name: "Identity" }),
      ).toBeInTheDocument();
      expect(
        within(dialog).getByRole("heading", { name: "Purchase and warranty" }),
      ).toBeInTheDocument();
      expect(
        within(dialog).getByRole("heading", { name: "Guest visibility" }),
      ).toBeInTheDocument();
      expect(
        within(dialog).getByRole("heading", { name: "Notes" }),
      ).toBeInTheDocument();
      expect(dialog.querySelectorAll(".asset-create__grid")).toHaveLength(7);
      expect(
        within(dialog).getByLabelText("Make").closest(".asset-create__grid"),
      ).toBe(
        within(dialog)
          .getByLabelText("Model")
          .closest(".asset-create__grid"),
      );
      expect(
        within(dialog)
          .getByLabelText("Purchase price")
          .closest(".asset-create__grid"),
      ).toBe(
        within(dialog)
          .getByLabelText("Currency")
          .closest(".asset-create__grid"),
      );
      expect(
        within(dialog)
          .getByRole("button", { name: "Create asset" })
          .closest(".asset-create__footer"),
      ).toBeInTheDocument();
      fireEvent.click(within(dialog).getByRole("button", { name: "Create asset" }));

      const name = within(dialog).getByLabelText("Name");
      expect(await within(dialog).findByRole("alert")).toHaveTextContent(
        "Enter an asset name",
      );
      expect(name).toHaveAttribute("aria-invalid", "true");
      expect(name).toHaveAccessibleDescription(
        "Enter an asset name before creating the asset.",
      );
      expect(requests.some((request) => request.method === "POST")).toBe(false);

      fireEvent.change(name, {
        target: { value: "Back patio grill" },
      });
      const purchasePrice = within(dialog).getByLabelText("Purchase price");
      fireEvent.change(purchasePrice, {
        target: { value: "12.345" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Create asset" }));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent(
        "up to two decimal places",
      );
      expect(purchasePrice).toHaveAttribute("aria-invalid", "true");
      expect(purchasePrice).toHaveAccessibleDescription(
        "Purchase price must be zero or more with up to two decimal places.",
      );
      expect(requests.some((request) => request.method === "POST")).toBe(false);

      fireEvent.change(purchasePrice, {
        target: { value: "12.34" },
      });
      const expectedLifespan = within(dialog).getByLabelText("Expected lifespan years");
      fireEvent.change(expectedLifespan, {
        target: { value: "1.5" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Create asset" }));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent(
        "Expected lifespan must be at least one year",
      );
      expect(expectedLifespan).toHaveAttribute("aria-invalid", "true");
      expect(expectedLifespan).toHaveAccessibleDescription(
        "Expected lifespan must be at least one year.",
      );
      expect(requests.some((request) => request.method === "POST")).toBe(false);

      fireEvent.change(expectedLifespan, {
        target: { value: "2" },
      });
      await within(dialog).findByRole("option", { name: "Entry" });
      fireEvent.change(within(dialog).getByLabelText("Area"), {
        target: { value: "area_entry" },
      });
      fireEvent.change(within(dialog).getByLabelText("Type"), {
        target: { value: "type_pump" },
      });
      fireEvent.change(purchasePrice, {
        target: { value: "12.34" },
      });
      fireEvent.change(within(dialog).getByLabelText("Currency"), {
        target: { value: "eur" },
      });
      fireEvent.click(within(dialog).getByLabelText("Visible to guests"));
      fireEvent.click(within(dialog).getByRole("button", { name: "Create asset" }));

      expect(await screen.findByText("Back patio grill")).toBeInTheDocument();
      await waitFor(() => {
        expect(screen.queryByRole("dialog", { name: "New asset" })).not.toBeInTheDocument();
      });
      const createRequest = requests.find((request) => request.method === "POST");
      expect(createRequest?.path).toBe("/w/acme/api/v1/assets");
      expect(createRequest?.body).toMatchObject({
        name: "Back patio grill",
        property_id: "prop_1",
        area_id: "area_entry",
        asset_type_id: "type_pump",
        condition: "good",
        status: "active",
        purchase_price_cents: 1234,
        purchase_currency: "EUR",
        expected_lifespan_years: 2,
        guest_visible: true,
      });
    } finally {
      restore();
    }
  });

  it("keeps the new asset dialog open with server validation errors", async () => {
    const { restore } = installFetch({
      createStatus: 422,
      createBody: {
        type: "https://crewday.dev/errors/validation",
        title: "Validation error",
        status: 422,
        detail: "Request validation failed",
        errors: [{ loc: ["body", "property_id"], msg: "Property is required" }],
      },
    });
    try {
      render(<Harness />);
      await screen.findByText("Front door lock");

      fireEvent.click(screen.getByRole("button", { name: "+ New asset" }));
      const dialog = screen.getByRole("dialog", { name: "New asset" });
      fireEvent.change(within(dialog).getByLabelText("Name"), {
        target: { value: "Back patio grill" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Create asset" }));

      expect(await within(dialog).findByRole("alert")).toHaveTextContent(
        "Property: Property is required",
      );
      expect(screen.getByRole("dialog", { name: "New asset" })).toBeInTheDocument();
    } finally {
      restore();
    }
  });

  it("falls back to the first property when the URL property filter is stale", async () => {
    const { restore } = installFetch();
    try {
      render(<Harness initial="/w/acme/assets?property_id=missing_property" />);

      await waitFor(() =>
        expect(screen.getByRole("button", { name: "+ New asset" })).toBeEnabled(),
      );
      fireEvent.click(screen.getByRole("button", { name: "+ New asset" }));

      const dialog = screen.getByRole("dialog", { name: "New asset" });
      expect(within(dialog).getByLabelText("Property")).toHaveValue("prop_1");
    } finally {
      restore();
    }
  });

  it("styles the new asset form with responsive grids and design-system control radius", () => {
    expect(managerPanelsCss).toContain(".asset-create__grid");
    expect(managerPanelsCss).toMatch(
      /\.asset-create__field input,\n\.asset-create__field select,\n\.asset-create__field textarea \{[^}]*border-radius: 6px;/,
    );
    expect(managerPanelsCss).toMatch(
      /\.asset-create__field input\[aria-invalid="true"\],[\s\S]*border-color: var\(--rust\);/,
    );
    expect(managerPanelsCss).toMatch(
      /@media \(max-width: 560px\) \{[\s\S]*\.asset-create__grid \{\n    grid-template-columns: 1fr;/,
    );
  });
});
