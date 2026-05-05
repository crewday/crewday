import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import type { Asset, AssetType, Property } from "@/types/api";
import AssetsPage from "./AssetsPage";
import appSource from "../../App.tsx?raw";
import { jsonResponse } from "@/test/helpers";

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

function installFetch() {
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const path = new URL(resolved, "http://crewday.test").pathname;
    if (path === "/w/acme/api/v1/assets") return jsonResponse({ data: ASSETS });
    if (path === "/w/acme/api/v1/asset_types") {
      return jsonResponse({ data: ASSET_TYPES });
    }
    if (path === "/w/acme/api/v1/properties") return jsonResponse(PROPERTIES);
    throw new Error(`Unexpected fetch call: ${resolved}`);
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return () => {
    (globalThis as { fetch: typeof fetch }).fetch = original;
  };
}

function Harness({ initial = "/assets" }: { initial?: string }) {
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
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<AssetsPage>", () => {
  it("wraps the assets routes in the scope-view permission guard", () => {
    expect(appSource).toMatch(
      /<Route element={<RequirePermission actionKey="scope\.view" \/>}>\s*<Route element={<ManagerLayout \/>}>\s*<Route path="\/assets" element={<AssetsPage \/>} \/>/,
    );
  });

  it("renders assets from paginated API envelopes and filters from the URL", async () => {
    const restore = installFetch();
    try {
      render(<Harness initial="/assets?category=security" />);

      expect(await screen.findByText("Front door lock")).toBeInTheDocument();
      expect(screen.queryByText("Pool pump")).not.toBeInTheDocument();
      expect(screen.getByText("Smart lock")).toBeInTheDocument();
      expect(screen.getAllByText("Villa Rosa").length).toBeGreaterThan(0);
    } finally {
      restore();
    }
  });

  it("opens the QR sheet for the active filters", async () => {
    const restore = installFetch();
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    try {
      render(<Harness initial="/assets?category=security&property_id=prop_1" />);
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
    const restore = installFetch();
    try {
      render(<Harness />);
      await screen.findByText("Front door lock");

      fireEvent.click(screen.getAllByText("Casa Azul")[0]!);

      await screen.findAllByText("Pool pump");
      expect(screen.queryByText("Front door lock")).not.toBeInTheDocument();
    } finally {
      restore();
    }
  });
});
