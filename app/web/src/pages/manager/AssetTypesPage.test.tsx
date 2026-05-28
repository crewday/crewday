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
import { jsonResponse } from "@/test/helpers";

const ASSET_TYPES: AssetType[] = [
  {
    id: "type_lock",
    key: "lock",
    name: "Smart lock",
    category: "security",
    icon_name: "lock",
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
  it("gates the asset type route before asset type content can render", () => {
    // The scope.view block bundles every read-only asset / inventory
    // surface; sibling routes (e.g. `/inventory`) may sit between
    // `/assets` and `/asset_types`. Match the wrapper + the AssetTypes
    // route presence rather than locking adjacency.
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

  it("renders asset types from paginated API envelopes", async () => {
    const restore = installFetch();
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      render(<Harness />);

      expect(await screen.findByText("Smart lock")).toBeInTheDocument();
      expect(screen.getByText("Pool pump")).toBeInTheDocument();
      expect(screen.getByText("security")).toBeInTheDocument();
      expect(screen.getByText("Expected lifespan: 5 years")).toBeInTheDocument();
      expect(screen.getAllByText("Battery check")).toHaveLength(2);
      expect(screen.getAllByText("every 30d")).toHaveLength(2);
      const consoleText = consoleError.mock.calls.flat().join("\n");
      expect(consoleText).not.toContain('Each child in a list should have a unique "key" prop');
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
