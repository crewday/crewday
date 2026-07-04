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
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import type { Property } from "@/types/api";
import InventoryPage from "./InventoryPage";
import appSource from "../../App.tsx?raw";
import formsCss from "@/styles/forms.css?raw";
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

const SECOND_PROPERTY: Property = {
  id: "prop_2",
  name: "Casa Azul",
  city: "Lisbon",
  timezone: "Europe/Lisbon",
  color: "sky",
  kind: "str",
  areas: ["Kitchen"],
  evidence_policy: "inherit",
  country: "PT",
  locale: "pt-PT",
  settings_override: {},
  client_org_id: null,
  owner_user_id: null,
};

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

const SECOND_ITEM = {
  ...ITEMS[0]!,
  id: "item_2",
  name: "Dish soap",
  sku: "DS-1",
  on_hand: 3,
  unit: "bottles",
  reorder_point: 4,
  reorder_target: 8,
  tags: ["Kitchen"],
};

const THIRD_ITEM = {
  ...ITEMS[0]!,
  id: "item_3",
  name: "Laundry pods",
  sku: "LP-3",
  on_hand: 18,
  unit: "pods",
  reorder_point: 10,
  reorder_target: 30,
  tags: ["Laundry"],
};

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

function installFetch(
  items = ITEMS,
  properties: Property[] = PROPERTIES,
  options: {
    createStatus?: number;
    createBody?: unknown;
    commitStatus?: number;
    commitBody?: unknown;
  } = {},
) {
  // code-health: ignore[nloc] Inventory route fixture keeps property, item, adjustment, and create endpoints explicit.
  const secondPropertyItems: typeof ITEMS = [];
  const env = installFetchRouteHandlers([
    { path: "/w/acme/api/v1/properties", respond: { body: properties } },
    {
      path: "/w/acme/api/v1/inventory/properties/prop_1/items",
      respond: { body: { data: items } },
    },
    {
      path: "/w/acme/api/v1/inventory/properties/prop_1/items",
      method: "POST",
      respond: (request) => {
        if (options.createStatus && options.createStatus >= 400) {
          return { status: options.createStatus, body: options.createBody };
        }
        const created = {
          ...ITEMS[0],
          ...(request.body as Record<string, unknown>),
          id: "item_new",
          workspace_id: "ws_1",
          property_id: "prop_1",
          on_hand: 0,
          tags: [],
          vendor: null,
          vendor_url: null,
          unit_cost_cents: null,
          notes_md: null,
          created_at: "2026-05-05T10:00:00Z",
          updated_at: null,
          deleted_at: null,
        };
        items.push(created as (typeof ITEMS)[number]);
        return { status: 201, body: created };
      },
    },
    {
      path: "/w/acme/api/v1/inventory/properties/prop_2/items",
      respond: { body: { data: secondPropertyItems } },
    },
    {
      path: "/w/acme/api/v1/inventory/properties/prop_2/items",
      method: "POST",
      respond: (request) => {
        const created = {
          ...ITEMS[0],
          ...(request.body as Record<string, unknown>),
          id: "item_new_2",
          workspace_id: "ws_1",
          property_id: "prop_2",
          on_hand: 0,
          tags: [],
          vendor: null,
          vendor_url: null,
          unit_cost_cents: null,
          notes_md: null,
          created_at: "2026-05-05T10:00:00Z",
          updated_at: null,
          deleted_at: null,
        };
        secondPropertyItems.push(created as (typeof ITEMS)[number]);
        return { status: 201, body: created };
      },
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
      path: "/w/acme/api/v1/stocktakes/stock_1/lines/item_2",
      method: "PATCH",
      respond: { body: { stocktake_id: "stock_1", item_id: "item_2" } },
    },
    {
      path: "/w/acme/api/v1/stocktakes/stock_1/lines/item_3",
      method: "PATCH",
      respond: { body: { stocktake_id: "stock_1", item_id: "item_3" } },
    },
    {
      path: "/w/acme/api/v1/stocktakes/stock_1/commit",
      method: "POST",
      respond: {
        status: options.commitStatus ?? 200,
        body: options.commitBody ?? { stocktake: { id: "stock_1" }, movements: [] },
      },
    },
  ]);
  return {
    requests: env.requests,
    restore: env.restore,
  };
}

function Harness({ initial = "/w/acme/inventory" }: { initial?: string } = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <WorkspaceProvider>
          <Routes>
            <Route path="/w/:slug/inventory" element={<InventoryPage />} />
            <Route path="/w/:slug/property/:pid/inventory" element={<InventoryPage />} />
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
    const guardStart = appSource.indexOf(
      '<Route element={<RequirePermission actionKey="scope.view" />}>',
    );
    expect(guardStart).toBeGreaterThan(-1);
    const nextGuard = appSource.indexOf(
      '<Route element={<RequirePermission actionKey="employees.read" />}>',
      guardStart,
    );
    expect(nextGuard).toBeGreaterThan(guardStart);
    expect(appSource.slice(guardStart, nextGuard)).toContain(
      '<Route path="inventory" element={<InventoryPage />} />',
    );
    expect(appSource.slice(guardStart, nextGuard)).toContain(
      '<Route path="property/:pid/inventory" element={<InventoryPage />} />',
    );
  });

  it("loads property-scoped inventory with the property tabs active", async () => {
    const fake = installFetch([...ITEMS], [PROPERTIES[0]!, SECOND_PROPERTY]);
    try {
      render(<Harness initial="/w/acme/property/prop_1/inventory" />);

      expect(await screen.findByText("Paper towels")).toBeInTheDocument();
      expect(screen.queryByText("Casa Azul")).not.toBeInTheDocument();
      expect(
        fake.requests.some((r) => r.url === "/w/acme/api/v1/inventory/properties/prop_1/items"),
      ).toBe(true);
      expect(
        fake.requests.some((r) => r.url === "/w/acme/api/v1/inventory/properties/prop_2/items"),
      ).toBe(false);

      const relatedPages = screen.getByRole("navigation", { name: "Related property pages" });
      expect(within(relatedPages).getByRole("link", { name: "Inventory" })).toHaveAttribute(
        "href",
        "/w/acme/property/prop_1/inventory",
      );
      expect(within(relatedPages).getByRole("link", { name: "Inventory" })).toHaveAttribute(
        "aria-current",
        "page",
      );
    } finally {
      fake.restore();
    }
  });

  it("keeps the workspace inventory route loading all visible properties", async () => {
    const fake = installFetch([...ITEMS], [PROPERTIES[0]!, SECOND_PROPERTY]);
    try {
      render(<Harness />);

      expect(await screen.findByText("Paper towels")).toBeInTheDocument();
      expect(
        fake.requests.some((r) => r.url === "/w/acme/api/v1/inventory/properties/prop_1/items"),
      ).toBe(true);
      expect(
        fake.requests.some((r) => r.url === "/w/acme/api/v1/inventory/properties/prop_2/items"),
      ).toBe(true);
      expect(screen.queryByRole("navigation", { name: "Related property pages" })).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("loads inventory and closes the drawer on Escape", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);
      fireEvent.click(await screen.findByText("Paper towels"));

      const drawer = await screen.findByRole("dialog", { name: /Inventory ledger/ });
      expect(drawer).toBeInTheDocument();
      // Native <dialog> Escape fires a `cancel` event on the dialog.
      fireEvent(drawer, new Event("cancel", { bubbles: false, cancelable: true }));

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

  it("keeps export inventory action unavailable", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Paper towels")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "More actions" }));
      expect(screen.getByRole("menuitem", { name: /Export CSV/ })).toBeDisabled();
      expect(screen.getByText("Inventory export needs a specified API endpoint first.")).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("opens an inline new item row with property choices for multi-property workspaces", async () => {
    const fake = installFetch([...ITEMS], [PROPERTIES[0]!, SECOND_PROPERTY]);
    try {
      render(<Harness />);
      await screen.findByText("Paper towels");

      fireEvent.click(screen.getByRole("button", { name: "+ New item" }));

      expect(screen.queryByRole("dialog", { name: "Create item" })).not.toBeInTheDocument();
      const inlineForm = await screen.findByRole("table", { name: "Create inventory item" });
      expect(inlineForm.closest(".inline-table-form")).toHaveClass("inv-create-inline");
      const property = within(inlineForm).getByRole("combobox", { name: /^Property\b/ });
      expect(property).toHaveValue("Villa Rosa");
      fireEvent.focus(property);
      expect(await screen.findByRole("option", { name: (name) => name.includes("Casa Azul") })).toBeInTheDocument();
      expect(within(inlineForm).getByLabelText(/^Name\b/)).toBeInTheDocument();
      expect(within(inlineForm).getByLabelText(/^Unit\b/)).toHaveValue("each");
      expect(within(inlineForm).getByLabelText(/^SKU\b/)).toBeInTheDocument();
      expect(within(inlineForm).getByLabelText(/^Barcode\b/)).toBeInTheDocument();
      expect(within(inlineForm).getByLabelText(/^Reorder point\b/)).toHaveValue("0");
      expect(within(inlineForm).getByLabelText(/^Reorder target\b/)).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("describes required and optional new item fields inline", async () => {
    const fake = installFetch([...ITEMS], [PROPERTIES[0]!, SECOND_PROPERTY]);
    try {
      render(<Harness />);
      await screen.findByText("Paper towels");

      fireEvent.click(screen.getByRole("button", { name: "+ New item" }));

      const inlineForm = await screen.findByRole("table", { name: "Create inventory item" });
      expect(within(inlineForm).getByText("Required: property, name, unit, reorder point.")).toBeVisible();
      expect(within(inlineForm).getByText("Reorder target is optional, but must be at least the reorder point.")).toBeVisible();
      expect(within(inlineForm).getByLabelText(/^Property\b/)).toBeInTheDocument();
      expect(within(inlineForm).getByLabelText(/^Name\b/)).toBeInTheDocument();
      expect(within(inlineForm).getByLabelText(/^Unit\b/)).toBeInTheDocument();
      expect(within(inlineForm).getByLabelText(/^Reorder point\b/)).toBeInTheDocument();
      expect(within(inlineForm).getByLabelText(/^SKU\b/)).toBeInTheDocument();
      expect(within(inlineForm).getByLabelText(/^Barcode\b/)).toBeInTheDocument();
      expect(within(inlineForm).getByLabelText(/^Reorder target\b/)).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("keeps structured form controls aligned to DESIGN.md tokens", () => {
    expect(formsCss).toMatch(
      /\.field input, \.field textarea, \.field select,[\s\S]*border: 1px solid var\(--line-strong\);[\s\S]*border-radius: var\(--radius-control\);/m,
    );
    expect(formsCss).not.toMatch(
      /\.field input:focus, \.field textarea:focus, \.field select:focus,/,
    );
  });

  it("shows client validation near the new item form", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);
      await screen.findByText("Paper towels");

      fireEvent.click(screen.getByRole("button", { name: "+ New item" }));
      const inlineForm = await screen.findByRole("table", { name: "Create inventory item" });
      fireEvent.click(within(inlineForm).getByRole("button", { name: "Save" }));

      const error = await within(inlineForm).findByText("Name is required.");
      const name = within(inlineForm).getByLabelText(/^Name\b/);

      expect(error).toBeVisible();
      expect(name).toHaveAttribute("aria-invalid", "true");
      expect(name).toHaveAccessibleDescription(/Name is required\./);
      expect(
        fake.requests.some((r) => r.method === "POST" && r.path.includes("/inventory/properties/")),
      ).toBe(false);
    } finally {
      fake.restore();
    }
  });

  it("reopens the new item form with a fresh draft", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);
      await screen.findByText("Paper towels");

      fireEvent.click(screen.getByRole("button", { name: "+ New item" }));
      const inlineForm = await screen.findByRole("table", { name: "Create inventory item" });
      fireEvent.change(within(inlineForm).getByLabelText(/^Name\b/), {
        target: { value: "Draft towels" },
      });
      fireEvent.change(within(inlineForm).getByLabelText(/^Unit\b/), {
        target: { value: "" },
      });
      fireEvent.click(within(inlineForm).getByRole("button", { name: "Save" }));
      expect(await within(inlineForm).findByText("Unit is required.")).toBeVisible();
      fireEvent.click(within(inlineForm).getByRole("button", { name: "Cancel" }));

      await waitFor(() => {
        expect(screen.queryByRole("table", { name: "Create inventory item" })).not.toBeInTheDocument();
      });
      fireEvent.click(screen.getByRole("button", { name: "+ New item" }));

      const reopened = await screen.findByRole("table", { name: "Create inventory item" });
      expect(within(reopened).queryByText(
        "Unit is required.",
      )).not.toBeInTheDocument();
      expect(within(reopened).getByLabelText(/^Name\b/)).toHaveValue("");
      expect(within(reopened).getByLabelText(/^Unit\b/)).toHaveValue("each");
    } finally {
      fake.restore();
    }
  });

  it("keeps reorder validation inline and blocks invalid new item submissions", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);
      await screen.findByText("Paper towels");

      fireEvent.click(screen.getByRole("button", { name: "+ New item" }));
      const inlineForm = await screen.findByRole("table", { name: "Create inventory item" });
      fireEvent.change(within(inlineForm).getByLabelText(/^Name\b/), {
        target: { value: "Dish soap" },
      });
      fireEvent.change(within(inlineForm).getByLabelText(/^Unit\b/), {
        target: { value: "bottle" },
      });
      fireEvent.change(within(inlineForm).getByLabelText(/^Reorder point\b/), {
        target: { value: "-1" },
      });
      fireEvent.click(within(inlineForm).getByRole("button", { name: "Save" }));

      expect(await within(inlineForm).findByText("Reorder point must be zero or more.")).toBeVisible();
      const reorderPoint = within(inlineForm).getByLabelText(/^Reorder point\b/);
      expect(reorderPoint).toHaveAttribute("aria-invalid", "true");
      expect(reorderPoint).toHaveAccessibleDescription(/Reorder point must be zero or more\./);

      fireEvent.change(reorderPoint, {
        target: { value: "4" },
      });
      fireEvent.change(within(inlineForm).getByLabelText(/^Reorder target\b/), {
        target: { value: "2" },
      });
      fireEvent.click(within(inlineForm).getByRole("button", { name: "Save" }));

      const reorderTarget = within(inlineForm).getByLabelText(/^Reorder target\b/);
      expect(await within(inlineForm).findByText("Reorder target must be at least the reorder point.")).toBeVisible();
      expect(reorderTarget).toHaveAttribute("aria-invalid", "true");
      expect(reorderTarget).toHaveAccessibleDescription(
        /Reorder target must be at least the reorder point\./,
      );
      expect(
        fake.requests.some((r) => r.method === "POST" && r.path.includes("/inventory/properties/")),
      ).toBe(false);
    } finally {
      fake.restore();
    }
  });

  it("posts a valid new item and shows it after inventory invalidation", async () => {
    const liveItems = [...ITEMS];
    const fake = installFetch(liveItems);
    try {
      render(<Harness />);
      await screen.findByText("Paper towels");

      fireEvent.click(screen.getByRole("button", { name: "+ New item" }));
      const inlineForm = await screen.findByRole("table", { name: "Create inventory item" });
      fireEvent.change(within(inlineForm).getByLabelText(/^Name\b/), {
        target: { value: "Dish soap" },
      });
      fireEvent.change(within(inlineForm).getByLabelText(/^Unit\b/), {
        target: { value: "bottle" },
      });
      fireEvent.change(within(inlineForm).getByLabelText(/^SKU\b/), {
        target: { value: "DS-1" },
      });
      fireEvent.change(within(inlineForm).getByLabelText(/^Barcode\b/), {
        target: { value: "0123456789012" },
      });
      fireEvent.change(within(inlineForm).getByLabelText(/^Reorder point\b/), {
        target: { value: "4" },
      });
      fireEvent.change(within(inlineForm).getByLabelText(/^Reorder target\b/), {
        target: { value: "8" },
      });
      fireEvent.click(within(inlineForm).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(screen.queryByRole("table", { name: "Create inventory item" })).not.toBeInTheDocument();
      });
      expect(screen.queryByRole("dialog", { name: "Create item" })).not.toBeInTheDocument();
      expect(await screen.findByText("Dish soap")).toBeInTheDocument();
      expect(
        fake.requests.find(
          (r) =>
            r.method === "POST" &&
            r.url === "/w/acme/api/v1/inventory/properties/prop_1/items",
        )?.body,
      ).toEqual({
        name: "Dish soap",
        unit: "bottle",
        sku: "DS-1",
        barcode_ean13: "0123456789012",
        reorder_point: 4,
        reorder_target: 8,
      });
      expect(
        fake.requests.filter(
          (r) =>
            r.method === "GET" &&
            r.url === "/w/acme/api/v1/inventory/properties/prop_1/items",
        ),
      ).toHaveLength(2);
    } finally {
      fake.restore();
    }
  });

  it("refetches only the property-scoped inventory page after creating an item from a property route", async () => {
    const liveItems = [...ITEMS];
    const fake = installFetch(liveItems, [PROPERTIES[0]!, SECOND_PROPERTY]);
    try {
      render(<Harness initial="/w/acme/property/prop_1/inventory" />);
      await screen.findByText("Paper towels");

      fireEvent.click(screen.getByRole("button", { name: "+ New item" }));
      const inlineForm = await screen.findByRole("table", { name: "Create inventory item" });
      expect(within(inlineForm).queryByRole("combobox", { name: /^Property\b/ })).toBeNull();
      fireEvent.change(within(inlineForm).getByLabelText(/^Name\b/), {
        target: { value: "Dish soap" },
      });
      fireEvent.change(within(inlineForm).getByLabelText(/^Unit\b/), {
        target: { value: "bottle" },
      });
      fireEvent.change(within(inlineForm).getByLabelText(/^Reorder point\b/), {
        target: { value: "4" },
      });
      fireEvent.click(within(inlineForm).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(screen.queryByRole("table", { name: "Create inventory item" })).not.toBeInTheDocument();
      });
      expect(await screen.findByText("Dish soap")).toBeInTheDocument();
      expect(
        fake.requests.filter(
          (r) =>
            r.method === "GET" &&
            r.url === "/w/acme/api/v1/inventory/properties/prop_1/items",
        ),
      ).toHaveLength(2);
      expect(
        fake.requests.some(
          (r) =>
            r.method === "GET" &&
            r.url === "/w/acme/api/v1/inventory/properties/prop_2/items",
        ),
      ).toBe(false);
    } finally {
      fake.restore();
    }
  });

  it("posts a new item to the selected property", async () => {
    const fake = installFetch([...ITEMS], [PROPERTIES[0]!, SECOND_PROPERTY]);
    try {
      render(<Harness />);
      await screen.findByText("Paper towels");

      fireEvent.click(screen.getByRole("button", { name: "+ New item" }));
      let inlineForm = await screen.findByRole("table", { name: "Create inventory item" });
      await chooseSearchableOption(inlineForm, /^Property\b/, "Casa Azul");
      inlineForm = await screen.findByRole("table", { name: "Create inventory item" });
      expect(within(inlineForm).getByRole("combobox", { name: /^Property\b/ })).toHaveValue("Casa Azul");
      expect(inlineForm.closest(".panel")).toHaveTextContent("Casa Azul");
      fireEvent.change(within(inlineForm).getByLabelText(/^Name\b/), {
        target: { value: "Olive oil" },
      });
      fireEvent.click(within(inlineForm).getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(screen.queryByRole("table", { name: "Create inventory item" })).not.toBeInTheDocument();
      });
      expect(await screen.findByText("Olive oil")).toBeInTheDocument();
      expect(
        fake.requests.some(
          (r) =>
            r.method === "POST" &&
            r.url === "/w/acme/api/v1/inventory/properties/prop_2/items",
        ),
      ).toBe(true);
    } finally {
      fake.restore();
    }
  });

  it("shows server validation copy when new item creation fails", async () => {
    const fake = installFetch([...ITEMS], PROPERTIES, {
      createStatus: 409,
      createBody: {
        type: "https://crewday.dev/errors/conflict",
        title: "Conflict",
        status: 409,
        error: "inventory_item_conflict",
        field: "sku",
      },
    });
    try {
      render(<Harness />);
      await screen.findByText("Paper towels");

      fireEvent.click(screen.getByRole("button", { name: "+ New item" }));
      const inlineForm = await screen.findByRole("table", { name: "Create inventory item" });
      fireEvent.change(within(inlineForm).getByLabelText(/^Name\b/), {
        target: { value: "Duplicate towels" },
      });
      fireEvent.click(within(inlineForm).getByRole("button", { name: "Save" }));

      const error = await within(inlineForm).findByText("SKU already exists for this property.");
      const sku = within(inlineForm).getByLabelText(/^SKU\b/);

      expect(error).toBeVisible();
      expect(sku).toHaveAttribute("aria-invalid", "true");
      expect(sku).toHaveAccessibleDescription(/SKU already exists for this property\./);
      expect(within(inlineForm).getByLabelText(/^Name\b/)).toHaveValue("Duplicate towels");
      expect(within(inlineForm).getByLabelText(/^Unit\b/)).toHaveValue("each");
      expect(screen.getByRole("table", { name: "Create inventory item" })).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("shows barcode conflict errors inline without losing the new item draft", async () => {
    const fake = installFetch([...ITEMS], PROPERTIES, {
      createStatus: 409,
      createBody: {
        type: "https://crewday.dev/errors/conflict",
        title: "Conflict",
        status: 409,
        error: "inventory_item_conflict",
        field: "barcode_ean13",
      },
    });
    try {
      render(<Harness />);
      await screen.findByText("Paper towels");

      fireEvent.click(screen.getByRole("button", { name: "+ New item" }));
      const inlineForm = await screen.findByRole("table", { name: "Create inventory item" });
      fireEvent.change(within(inlineForm).getByLabelText(/^Name\b/), {
        target: { value: "Duplicate barcode" },
      });
      fireEvent.change(within(inlineForm).getByLabelText(/^Barcode\b/), {
        target: { value: "0123456789012" },
      });
      fireEvent.click(within(inlineForm).getByRole("button", { name: "Save" }));

      expect(await within(inlineForm).findByText("Barcode already exists for this property.")).toBeVisible();
      const barcode = within(inlineForm).getByLabelText(/^Barcode\b/);
      expect(barcode).toHaveAttribute("aria-invalid", "true");
      expect(barcode).toHaveAccessibleDescription(/Barcode already exists for this property\./);
      expect(within(inlineForm).getByLabelText(/^Name\b/)).toHaveValue("Duplicate barcode");
      expect(barcode).toHaveValue("0123456789012");
      expect(screen.getByRole("table", { name: "Create inventory item" })).toBeInTheDocument();
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

  it("edits multiple stocktake rows, excludes unchanged rows, and commits once", async () => {
    const fake = installFetch([ITEMS[0]!, SECOND_ITEM, THIRD_ITEM]);
    try {
      render(<Harness />);
      await screen.findByText("Paper towels");

      fireEvent.click(screen.getByRole("button", { name: "Start stocktake" }));
      const title = await screen.findByText("Stocktake, Villa Rosa");
      const dialog = title.closest("dialog")!;
      const paperReason = within(dialog).getByLabelText("Paper towels reason");
      expect(paperReason).toBeDisabled();
      expect(
        within(dialog).getByRole("button", { name: "No changes to commit" }),
      ).toBeDisabled();

      fireEvent.change(within(dialog).getByLabelText("Paper towels observed count"), {
        target: { value: "8" },
      });
      expect(paperReason).toBeEnabled();
      fireEvent.change(paperReason, {
        target: { value: "loss" },
      });
      fireEvent.change(within(dialog).getByLabelText("Paper towels note"), {
        target: { value: "Shelf count from utility closet." },
      });
      fireEvent.change(within(dialog).getByLabelText("Dish soap observed count"), {
        target: { value: "5" },
      });
      fireEvent.change(within(dialog).getByLabelText("Dish soap reason"), {
        target: { value: "found" },
      });
      fireEvent.change(within(dialog).getByLabelText("Dish soap note"), {
        target: { value: "Two bottles under sink." },
      });
      fireEvent.change(within(dialog).getByLabelText("Laundry pods note"), {
        target: { value: "Checked laundry cabinet; no drift." },
      });
      expect(
        within(dialog).getByRole("button", { name: "Commit 2 changes" }),
      ).toBeEnabled();

      fireEvent.click(within(dialog).getByRole("button", { name: "Commit 2 changes" }));

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
        note: "Shelf count from utility closet.",
      });
      expect(
        fake.requests.find(
          (r) =>
            r.method === "PATCH" &&
            r.url === "/w/acme/api/v1/stocktakes/stock_1/lines/item_2",
        )?.body,
      ).toEqual({
        observed_on_hand: 5,
        reason: "found",
        note: "Two bottles under sink.",
      });
      expect(
        fake.requests.some(
          (r) =>
            r.method === "PATCH" &&
            r.url === "/w/acme/api/v1/stocktakes/stock_1/lines/item_3",
        ),
      ).toBe(false);
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

  it("blocks invalid stocktake observed counts and discards the draft", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);
      await screen.findByText("Paper towels");

      fireEvent.click(screen.getByRole("button", { name: "Start stocktake" }));
      const title = await screen.findByText("Stocktake, Villa Rosa");
      const dialog = title.closest("dialog")!;
      fireEvent.change(within(dialog).getByLabelText("Paper towels observed count"), {
        target: { value: "-1" },
      });

      expect(
        within(dialog).getByText("Observed count must be 0 or more."),
      ).toBeInTheDocument();
      expect(
        within(dialog).getByRole("button", { name: "No changes to commit" }),
      ).toBeDisabled();

      fireEvent.click(within(dialog).getByRole("button", { name: "Discard changes" }));

      expect(within(dialog).getByDisplayValue("10")).toBeInTheDocument();
      expect(
        within(dialog).queryByText("Observed count must be 0 or more."),
      ).not.toBeInTheDocument();
      expect(
        fake.requests.some(
          (r) =>
            r.method === "POST" &&
            r.url === "/w/acme/api/v1/properties/prop_1/stocktakes",
        ),
      ).toBe(false);
    } finally {
      fake.restore();
    }
  });

  it("keeps the stocktake sheet open and reports commit failures", async () => {
    const fake = installFetch(ITEMS.map((item) => ({ ...item })), PROPERTIES, {
      commitStatus: 500,
      commitBody: { detail: "Commit failed" },
    });
    try {
      render(<Harness />);
      await screen.findByText("Paper towels");

      fireEvent.click(screen.getByRole("button", { name: "Start stocktake" }));
      const title = await screen.findByText("Stocktake, Villa Rosa");
      const dialog = title.closest("dialog")!;
      fireEvent.change(within(dialog).getByLabelText("Paper towels observed count"), {
        target: { value: "8" },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Commit 1 change" }));

      expect(await within(dialog).findByText("Commit failed")).toBeInTheDocument();
      expect(within(dialog).getByText("Stocktake, Villa Rosa")).toBeInTheDocument();
      expect(within(dialog).getByDisplayValue("8")).toBeInTheDocument();

      fireEvent.click(within(dialog).getByRole("button", { name: "Discard changes" }));

      expect(within(dialog).queryByText("Commit failed")).not.toBeInTheDocument();
      expect(within(dialog).getByDisplayValue("10")).toBeInTheDocument();
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

      liveItems.push(SECOND_ITEM);
      fireEvent.click(screen.getByRole("button", { name: "Start stocktake" }));

      const sheet = await screen.findByText("Stocktake, Villa Rosa");
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
