import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import type { AssetType } from "@/types/api";
import AssetTypesPage from "./AssetTypesPage";
import appSource from "../../App.tsx?raw";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? "OK" : "Error",
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const ASSET_TYPES: AssetType[] = [
  {
    id: "type_lock",
    key: "lock",
    name: "Smart lock",
    category: "security",
    icon_name: "lock",
    default_actions: [
      {
        key: "battery_check",
        label: "Battery check",
        interval_days: 30,
        estimated_duration_minutes: 10,
      },
    ],
    default_lifespan_years: 5,
  },
  {
    id: "type_pump",
    key: "pump",
    name: "Pool pump",
    category: "pool",
    icon_name: "waves",
    default_actions: [],
    default_lifespan_years: null,
  },
];

function installFetch(body: unknown = { data: ASSET_TYPES }, status = 200) {
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const path = new URL(resolved, "http://crewday.test").pathname;
    if (path === "/w/acme/api/v1/asset_types") return jsonResponse(body, status);
    throw new Error(`Unexpected fetch call: ${resolved}`);
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return () => {
    (globalThis as { fetch: typeof fetch }).fetch = original;
  };
}

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <WorkspaceProvider>
        <MemoryRouter initialEntries={["/asset_types"]}>
          <AssetTypesPage />
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

describe("<AssetTypesPage>", () => {
  it("wraps the asset type route in the scope-view permission guard", () => {
    expect(appSource).toMatch(
      /<Route element={<RequirePermission actionKey="scope\.view" \/>}>\s*<Route element={<ManagerLayout \/>}>\s*<Route path="\/assets" element={<AssetsPage \/>} \/>\s*<Route path="\/asset_types" element={<AssetTypesPage \/>} \/>/,
    );
  });

  it("also wires the workspace-scoped asset type route", () => {
    expect(appSource).toContain(
      '<Route path="/w/:slug/asset_types" element={<AssetTypesPage />} />',
    );
  });

  it("renders asset types from paginated API envelopes", async () => {
    const restore = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Smart lock")).toBeInTheDocument();
      expect(screen.getByText("Pool pump")).toBeInTheDocument();
      expect(screen.getByText("security")).toBeInTheDocument();
      expect(screen.getByText("Expected lifespan: 5 years")).toBeInTheDocument();
      expect(screen.getByText("Battery check")).toBeInTheDocument();
      expect(screen.getByText("every 30d")).toBeInTheDocument();
    } finally {
      restore();
    }
  });

  it("renders bare list responses for mock parity", async () => {
    const restore = installFetch(ASSET_TYPES);
    try {
      render(<Harness />);

      expect(await screen.findByText("Smart lock")).toBeInTheDocument();
      expect(screen.getByText("Pool pump")).toBeInTheDocument();
    } finally {
      restore();
    }
  });

  it("shows the mock loading and error states", async () => {
    const restore = installFetch({ detail: "nope" }, 500);
    try {
      render(<Harness />);

      expect(screen.getByText(/Loading/)).toBeInTheDocument();
      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
    } finally {
      restore();
    }
  });
});
