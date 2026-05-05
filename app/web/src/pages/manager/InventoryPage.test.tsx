import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import type { Property } from "@/types/api";
import InventoryPage from "./InventoryPage";
import appSource from "../../App.tsx?raw";
import { installFetchRouteHandlers } from "@/test/helpers";

const PROPERTIES: Property[] = [
  {
    id: "prop_1",
    name: "Villa Rosa",
    city: "Porto",
    timezone: "Europe/Lisbon",
    color: "moss",
    kind: "str",
    areas: ["Utility"],
    evidence_policy: "inherit",
    country: "PT",
    locale: "pt-PT",
    settings_override: {},
    client_org_id: null,
    owner_user_id: null,
  },
];

const ITEMS = [
  {
    id: "item_1",
    workspace_id: "ws_1",
    property_id: "prop_1",
    name: "Paper towels",
    sku: "PT-12",
    on_hand: 10,
    unit: "rolls",
    reorder_point: 12,
    reorder_target: 24,
    vendor: null,
    vendor_url: null,
    unit_cost_cents: null,
    barcode_ean13: null,
    tags: ["Utility"],
    notes_md: null,
    created_at: "2026-04-29T10:00:00Z",
    updated_at: null,
    deleted_at: null,
  },
];

const MOVEMENTS = [
  {
    id: "move_1",
    workspace_id: "ws_1",
    item_id: "item_1",
    delta: -2,
    reason: "consume",
    source_task_id: "task_1",
    occurrence_id: "task_1",
    source_stocktake_id: null,
    actor_kind: "user",
    actor_id: "user_1",
    occurred_at: "2026-04-29T10:00:00Z",
    note: null,
    on_hand_after: 10,
  },
];

function installFetch(items = ITEMS) {
  const env = installFetchRouteHandlers([
    { path: "/w/acme/api/v1/properties", respond: { body: PROPERTIES } },
    {
      path: "/w/acme/api/v1/inventory/properties/prop_1/items",
      respond: { body: { data: items } },
    },
    {
      path: "/w/acme/api/v1/inventory/item_1/movements",
      respond: { body: { data: MOVEMENTS, next_cursor: null, has_more: false } },
    },
    {
      path: "/w/acme/api/v1/inventory/item_1/adjust",
      method: "POST",
      respond: { status: 201, body: { ...MOVEMENTS[0], id: "move_adjust", delta: 4 } },
    },
    {
      path: "/w/acme/api/v1/inventory/properties/prop_1/items/item_1",
      method: "PATCH",
      respond: { body: { ...ITEMS[0], reorder_point: 8, reorder_target: 20 } },
    },
    {
      path: "/w/acme/api/v1/properties/prop_1/stocktakes",
      method: "POST",
      respond: { status: 201, body: { id: "stock_1" } },
    },
    {
      path: "/w/acme/api/v1/stocktakes/stock_1/lines/item_1",
      method: "PATCH",
      respond: { body: { stocktake_id: "stock_1", item_id: "item_1" } },
    },
    {
      path: "/w/acme/api/v1/stocktakes/stock_1/commit",
      method: "POST",
      respond: { body: { stocktake: { id: "stock_1" }, movements: [] } },
    },
  ]);
  return {
    requests: env.requests,
    restore: env.restore,
  };
}

function Harness() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WorkspaceProvider>
          <InventoryPage />
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      observe(): void {}
      disconnect(): void {}
    },
  );
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close() {
    this.open = false;
  };
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("<InventoryPage>", () => {
  it("wraps the inventory route in the scope-view permission guard", () => {
    expect(appSource).toMatch(
      /<Route element={<RequirePermission actionKey="scope\.view" \/>}>\s*<Route element={<ManagerLayout \/>}>\s*<Route path="\/assets" element={<AssetsPage \/>} \/>\s*<Route path="\/inventory" element={<InventoryPage \/>} \/>/,
    );
  });

  it("loads inventory and closes the drawer on Escape", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);
      fireEvent.click(await screen.findByText("Paper towels"));

      expect(
        await screen.findByRole("dialog", { name: /Inventory ledger/ }),
      ).toBeInTheDocument();
      fireEvent.keyDown(window, { key: "Escape" });

      await waitFor(() => {
        expect(
          screen.queryByRole("dialog", { name: /Inventory ledger/ }),
        ).not.toBeInTheDocument();
      });
      expect(fake.requests.map((r) => r.url)).toContain(
        "/w/acme/api/v1/inventory/properties/prop_1/items",
      );
    } finally {
      fake.restore();
    }
  });

  it("posts adjustment and reorder-rule payloads from the drawer", async () => {
    const fake = installFetch();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    try {
      render(<Harness />);
      fireEvent.click(await screen.findByText("Paper towels"));
      const drawer = await screen.findByRole("dialog", { name: /Inventory ledger/ });
      const numberInputs = within(drawer).getAllByRole("spinbutton");
      const parInput = numberInputs[0]!;
      const targetInput = numberInputs[1]!;
      const observedInput = numberInputs[2]!;

      fireEvent.change(observedInput, {
        target: { value: "14" },
      });
      fireEvent.change(within(drawer).getByLabelText("Reason"), {
        target: { value: "found" },
      });
      fireEvent.click(within(drawer).getByRole("button", { name: "Record adjustment" }));

      await waitFor(() => {
        expect(
          fake.requests.some(
            (r) =>
              r.method === "POST" &&
              r.url === "/w/acme/api/v1/inventory/item_1/adjust",
          ),
        ).toBe(true);
      });
      expect(
        fake.requests.find(
          (r) => r.method === "POST" && r.url === "/w/acme/api/v1/inventory/item_1/adjust",
        )?.body,
      ).toEqual({
        observed_on_hand: 14,
        reason: "found",
        note: "",
      });

      fireEvent.change(parInput, {
        target: { value: "8" },
      });
      fireEvent.change(targetInput, {
        target: { value: "20" },
      });
      fireEvent.click(within(drawer).getByRole("button", { name: "Save reorder rule" }));

      await waitFor(() => {
        expect(
          fake.requests.some(
            (r) =>
              r.method === "PATCH" &&
              r.url === "/w/acme/api/v1/inventory/properties/prop_1/items/item_1",
          ),
        ).toBe(true);
      });
      expect(confirm).toHaveBeenCalled();
      expect(
        fake.requests.find(
          (r) =>
            r.method === "PATCH" &&
            r.url === "/w/acme/api/v1/inventory/properties/prop_1/items/item_1",
        )?.body,
      ).toEqual({
        reorder_point: 8,
        reorder_target: 20,
      });
    } finally {
      fake.restore();
    }
  });

  it("confirms when lowering an already-below-stock reorder point", async () => {
    const belowStockItems = ITEMS.map((item) => ({
      ...item,
      reorder_point: 9,
      reorder_target: 24,
    }));
    const fake = installFetch(belowStockItems);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    try {
      render(<Harness />);
      fireEvent.click(await screen.findByText("Paper towels"));
      const drawer = await screen.findByRole("dialog", { name: /Inventory ledger/ });
      const parInput = within(drawer).getAllByRole("spinbutton")[0]!;

      fireEvent.change(parInput, {
        target: { value: "8" },
      });
      fireEvent.click(within(drawer).getByRole("button", { name: "Save reorder rule" }));

      await waitFor(() => {
        expect(confirm).toHaveBeenCalled();
      });
    } finally {
      fake.restore();
    }
  });

  it("opens, saves changed lines, and commits a stocktake", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);
      await screen.findByText("Paper towels");

      fireEvent.click(screen.getByRole("button", { name: "Start stocktake" }));
      expect(await screen.findByText("Stocktake — Villa Rosa")).toBeInTheDocument();
      fireEvent.change(screen.getByDisplayValue("10"), {
        target: { value: "8" },
      });
      fireEvent.change(screen.getByRole("combobox"), {
        target: { value: "loss" },
      });
      fireEvent.click(
        await screen.findByRole("button", { name: "Commit 1 change" }),
      );

      await waitFor(() => {
        expect(
          fake.requests.some(
            (r) =>
              r.method === "POST" &&
              r.url === "/w/acme/api/v1/stocktakes/stock_1/commit",
          ),
        ).toBe(true);
      });
      expect(
        fake.requests.find(
          (r) =>
            r.method === "PATCH" &&
            r.url === "/w/acme/api/v1/stocktakes/stock_1/lines/item_1",
        )?.body,
      ).toEqual({
        observed_on_hand: 8,
        reason: "loss",
        note: "",
      });
      expect(
        fake.requests.find(
          (r) =>
            r.method === "POST" &&
            r.url === "/w/acme/api/v1/stocktakes/stock_1/commit",
        )?.headers["Idempotency-Key"],
      ).toMatch(/^stocktake:stock_1:commit:/);
    } finally {
      fake.restore();
    }
  });

  it("keeps the stocktake sheet stable when inventory refetch adds an item", async () => {
    const liveItems = [...ITEMS];
    const fake = installFetch(liveItems);
    try {
      render(<Harness />);
      await screen.findByText("Paper towels");

      const baseItem = ITEMS[0]!;
      liveItems.push({
        ...baseItem,
        id: "item_2",
        name: "Dish soap",
        sku: "DS-1",
        on_hand: 3,
        reorder_point: 4,
        reorder_target: 8,
        tags: ["Kitchen"],
      });
      fireEvent.click(screen.getByRole("button", { name: "Start stocktake" }));

      const sheet = await screen.findByText("Stocktake — Villa Rosa");
      expect(sheet).toBeInTheDocument();
      const dialog = sheet.closest("dialog")!;
      await waitFor(() => {
        expect(within(dialog).getByText("Dish soap")).toBeInTheDocument();
      });
    } finally {
      fake.restore();
    }
  });
});
