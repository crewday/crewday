import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import {
  __resetQueryKeyGetterForTests,
  registerQueryKeyWorkspaceGetter,
} from "@/lib/queryKeys";
import { installFetchRouteHandlers, type FetchRoute } from "@/test/helpers";
import AreasPanel from "./AreasPanel";

interface TestArea {
  id: string;
  property_id: string;
  unit_id: string | null;
  name: string;
  kind: "indoor_room" | "outdoor" | "service";
  order_hint: number;
  parent_area_id: string | null;
  notes_md: string;
}

interface RenderAreasPanelOptions {
  failPatchAreaIds?: readonly string[];
  stalePatchAreaIds?: readonly string[];
}

function orderedAreas(areas: readonly TestArea[]): TestArea[] {
  return [...areas].sort(
    (left, right) => left.order_hint - right.order_hint || left.id.localeCompare(right.id),
  );
}

function renderedRowLabels(container: HTMLElement): (string | null)[] {
  return Array.from(container.querySelectorAll("[data-inline-table-row-group]")).map((row) =>
    row.getAttribute("aria-label"),
  );
}

function renderAreasPanel(initialAreas: TestArea[] = [], options: RenderAreasPanelOptions = {}) {
  let areas = [...initialAreas];
  const failPatchAreaIds = new Set(options.failPatchAreaIds ?? []);
  const stalePatchAreaIds = new Set(options.stalePatchAreaIds ?? []);
  const routes: FetchRoute[] = [
    {
      path: "/w/acme/api/v1/properties/prop_1/areas",
      respond: () => ({ body: { data: orderedAreas(areas), next_cursor: null, has_more: false } }),
    },
    {
      path: "/w/acme/api/v1/properties/prop_1/areas",
      method: "POST",
      respond: ({ body }) => {
        const draft = body as Omit<TestArea, "id" | "property_id">;
        const area: TestArea = {
          id: "area_" + String(areas.length + 1),
          property_id: "prop_1",
          unit_id: draft.unit_id,
          name: draft.name,
          kind: draft.kind,
          order_hint: draft.order_hint,
          parent_area_id: draft.parent_area_id,
          notes_md: draft.notes_md,
        };
        areas = [...areas, area];
        return { status: 201, body: area };
      },
    },
    ...initialAreas.map((area): FetchRoute => ({
      path: "/w/acme/api/v1/areas/" + area.id,
      method: "PATCH",
      respond: ({ body }) => {
        if (failPatchAreaIds.has(area.id)) {
          return { status: 500, body: { detail: "Reorder failed." } };
        }
        const draft = body as Omit<TestArea, "id" | "property_id">;
        let patched: TestArea | null = null;
        areas = areas.map((current) => {
          if (current.id !== area.id) return current;
          patched = {
            ...current,
            unit_id: draft.unit_id,
            name: draft.name,
            kind: draft.kind,
            order_hint: draft.order_hint,
            parent_area_id: draft.parent_area_id,
            notes_md: draft.notes_md,
          };
          return stalePatchAreaIds.has(area.id) ? current : patched;
        });
        return patched ? { body: patched } : { status: 404, body: { detail: "Area not found" } };
      },
    })),
  ];
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const fetchEnv = installFetchRouteHandlers(routes);
  const view = render(
    <QueryClientProvider client={queryClient}>
      <AreasPanel propertyId="prop_1" />
    </QueryClientProvider>,
  );
  return {
    ...view,
    ...fetchEnv,
    setAreas(nextAreas: TestArea[]) {
      areas = [...nextAreas];
    },
    queryClient,
  };
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  registerWorkspaceSlugGetter(() => "acme");
  registerQueryKeyWorkspaceGetter(() => "acme");
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
});

describe("<AreasPanel>", () => {
  it("shows an editable create row by default without a separate add button", async () => {
    renderAreasPanel([
      areaFixture({
        id: "area_1",
        name: "Storage",
        order_hint: 27,
      }),
    ]);

    expect(await screen.findByRole("table", { name: "Property areas" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Order" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New area" })).not.toBeInTheDocument();
    expect(within(screen.getByLabelText("Storage")).queryByText("27")).not.toBeInTheDocument();
    const createRow = screen.getByLabelText("New area");

    expect(createRow).toHaveClass("inline-table-form__group--trailing-create", "is-editing");
    expect(within(createRow).getByLabelText("Name")).toBeInTheDocument();
    expect(within(createRow).queryByLabelText("Order")).not.toBeInTheDocument();
    expect(within(createRow).getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.queryByRole("heading", { name: "No areas yet" })).not.toBeInTheDocument();
  });

  it("creates from the default row and leaves a fresh empty row ready", async () => {
    const { requests } = renderAreasPanel([
      areaFixture({
        id: "area_1",
        name: "Kitchen",
        order_hint: 2,
      }),
      areaFixture({
        id: "area_2",
        name: "Terrace",
        kind: "outdoor",
        order_hint: 7,
      }),
    ]);

    const createRow = await screen.findByLabelText("New area");
    fireEvent.change(within(createRow).getByLabelText("Name"), {
      target: { value: "Pool" },
    });
    fireEvent.change(within(createRow).getByLabelText("Kind"), {
      target: { value: "outdoor" },
    });
    fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Pool")).toBeInTheDocument();
    const createRequest = requests.find((request) => request.method === "POST");
    expect(createRequest?.path).toBe("/w/acme/api/v1/properties/prop_1/areas");
    expect(createRequest?.body).toMatchObject({
      name: "Pool",
      kind: "outdoor",
      order_hint: 8,
      parent_area_id: null,
    });

    await waitFor(() => {
      expect(within(screen.getByLabelText("New area")).getByLabelText("Name")).toHaveValue("");
    });
  });

  it("renders areas as a stable pre-order tree and suppresses flat reorder dragging", async () => {
    const { container } = renderAreasPanel([
      areaFixture({
        id: "area_bath",
        name: "Master bathroom",
        order_hint: 0,
        parent_area_id: "area_bedroom",
      }),
      areaFixture({
        id: "area_floor",
        name: "Floor 1",
        order_hint: 2,
      }),
      areaFixture({
        id: "area_bedroom",
        name: "Master bedroom",
        order_hint: 1,
        parent_area_id: "area_floor",
      }),
      areaFixture({
        id: "area_patio",
        name: "Patio",
        kind: "outdoor",
        order_hint: 0,
      }),
    ]);

    expect(await screen.findByLabelText("Floor 1")).toBeInTheDocument();
    const rowLabels = renderedRowLabels(container);
    expect(rowLabels.slice(0, 4)).toEqual(["Patio", "Floor 1", "Master bedroom", "Master bathroom"]);
    expect(within(screen.getByLabelText("Floor 1")).getByText(/Level 1, has child rows/)).toHaveClass("sr-only");
    expect(within(screen.getByLabelText("Master bedroom")).getByText(/Level 2, has child rows/)).toHaveClass(
      "sr-only",
    );
    expect(within(screen.getByLabelText("Master bathroom")).getByText(/Level 3, last child/)).toHaveClass("sr-only");

    const bedroomRow = screen.getByLabelText("Master bedroom");
    expect(bedroomRow).not.toHaveAttribute("draggable");
    expect(screen.queryByLabelText("Drag Master bedroom to reorder")).not.toBeInTheDocument();
  });

  it("keeps reorder disabled while editing sibling rows in the tree table", async () => {
    renderAreasPanel([
      areaFixture({
        id: "area_1",
        name: "Kitchen",
        order_hint: 0,
      }),
      areaFixture({
        id: "area_2",
        name: "Pool",
        kind: "outdoor",
        order_hint: 1,
      }),
    ]);

    await screen.findByText("Kitchen");
    const poolRow = screen.getByLabelText("Pool");
    expect(poolRow).not.toHaveAttribute("draggable");
    expect(screen.queryByLabelText("Drag Pool to reorder")).not.toBeInTheDocument();
    expect(within(poolRow).getByRole("button", { name: "Move Pool up" })).toBeEnabled();
    expect(within(poolRow).getByRole("button", { name: "Move Pool down" })).toBeDisabled();

    const kitchenRow = screen.getByLabelText("Kitchen");
    fireEvent.click(within(kitchenRow).getByRole("button", { name: "Edit" }));

    expect(poolRow).not.toHaveAttribute("draggable");
    expect(screen.queryByLabelText("Drag Pool to reorder")).not.toBeInTheDocument();
    expect(within(poolRow).getByRole("button", { name: "Move Pool up" })).toBeDisabled();

    fireEvent.click(within(kitchenRow).getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(within(poolRow).getByRole("button", { name: "Move Pool up" })).toBeEnabled();
    });
  });

  it("reorders property-level sibling areas without changing parent scopes", async () => {
    const { container, requests } = renderAreasPanel([
      areaFixture({
        id: "area_floor_1",
        name: "Floor 1",
        order_hint: 0,
      }),
      areaFixture({
        id: "area_room",
        name: "Bedroom",
        order_hint: 0,
        parent_area_id: "area_floor_1",
      }),
      areaFixture({
        id: "area_floor_2",
        name: "Floor 2",
        order_hint: 1,
      }),
      areaFixture({
        id: "area_patio",
        name: "Patio",
        kind: "outdoor",
        order_hint: 2,
      }),
    ]);

    const floor2Row = await screen.findByLabelText("Floor 2");
    fireEvent.click(within(floor2Row).getByRole("button", { name: "Move Floor 2 up" }));

    await waitFor(() => {
      expect(renderedRowLabels(container).slice(0, 4)).toEqual(["Floor 2", "Floor 1", "Bedroom", "Patio"]);
    });
    await waitFor(() => {
      expect(
        requests.filter(
          (request) => request.method === "GET" && request.path === "/w/acme/api/v1/properties/prop_1/areas",
        ).length,
      ).toBeGreaterThan(1);
    });

    const patchRequests = requests.filter((request) => request.method === "PATCH");
    expect(patchRequests.map((request) => request.path)).toEqual([
      "/w/acme/api/v1/areas/area_floor_2",
      "/w/acme/api/v1/areas/area_floor_1",
      "/w/acme/api/v1/areas/area_patio",
    ]);
    expect(patchRequests.map((request) => request.body)).toMatchObject([
      { order_hint: 0, parent_area_id: null },
      { order_hint: 1, parent_area_id: null },
      { order_hint: 2, parent_area_id: null },
    ]);
    expect(patchRequests.some((request) => request.path.endsWith("/area_room"))).toBe(false);
  });

  it("reorders nested sibling areas without moving them out of their parent", async () => {
    const { container, requests } = renderAreasPanel([
      areaFixture({
        id: "area_floor",
        name: "Floor 1",
        order_hint: 0,
      }),
      areaFixture({
        id: "area_bedroom",
        name: "Bedroom",
        order_hint: 0,
        parent_area_id: "area_floor",
      }),
      areaFixture({
        id: "area_closet",
        name: "Closet",
        order_hint: 1,
        parent_area_id: "area_floor",
      }),
      areaFixture({
        id: "area_patio",
        name: "Patio",
        kind: "outdoor",
        order_hint: 1,
      }),
    ]);

    const closetRow = await screen.findByLabelText("Closet");
    fireEvent.click(within(closetRow).getByRole("button", { name: "Move Closet up" }));

    await waitFor(() => {
      expect(renderedRowLabels(container).slice(0, 4)).toEqual(["Floor 1", "Closet", "Bedroom", "Patio"]);
    });
    await waitFor(() => {
      expect(
        requests.filter(
          (request) => request.method === "GET" && request.path === "/w/acme/api/v1/properties/prop_1/areas",
        ).length,
      ).toBeGreaterThan(1);
    });

    const patchRequests = requests.filter((request) => request.method === "PATCH");
    expect(patchRequests.map((request) => request.path)).toEqual([
      "/w/acme/api/v1/areas/area_closet",
      "/w/acme/api/v1/areas/area_bedroom",
    ]);
    expect(patchRequests.map((request) => request.body)).toMatchObject([
      { order_hint: 0, parent_area_id: "area_floor" },
      { order_hint: 1, parent_area_id: "area_floor" },
    ]);
    expect(patchRequests.some((request) => request.path.endsWith("/area_floor"))).toBe(false);
    expect(patchRequests.some((request) => request.path.endsWith("/area_patio"))).toBe(false);
  });

  it("allows multi-level parent edits while excluding self and descendants", async () => {
    const { requests } = renderAreasPanel([
      areaFixture({
        id: "area_floor",
        name: "Floor 1",
        order_hint: 0,
      }),
      areaFixture({
        id: "area_bedroom",
        name: "Bedroom",
        order_hint: 1,
        parent_area_id: "area_floor",
      }),
      areaFixture({
        id: "area_bath",
        name: "Bathroom",
        order_hint: 2,
        parent_area_id: "area_bedroom",
      }),
      areaFixture({
        id: "area_patio",
        name: "Patio",
        kind: "outdoor",
        order_hint: 3,
      }),
    ]);

    const floorRow = await screen.findByLabelText("Floor 1");
    fireEvent.click(within(floorRow).getByRole("button", { name: "Edit" }));
    const parentInput = within(floorRow).getByRole("combobox", { name: /^Parent\b/ });
    fireEvent.focus(parentInput);
    fireEvent.change(parentInput, { target: { value: "bath" } });
    expect(await screen.findByText("No parent areas")).toBeInTheDocument();
    fireEvent.change(parentInput, { target: { value: "patio" } });
    fireEvent.mouseDown(await screen.findByRole("option", { name: /Patio/ }));
    fireEvent.click(within(floorRow).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(requests.find((request) => request.path === "/w/acme/api/v1/areas/area_floor")?.body).toMatchObject({
        parent_area_id: "area_patio",
      });
    });
  });

  it("keeps a saved area draft visible across a stale refetch", async () => {
    const { requests } = renderAreasPanel(
      [
        areaFixture({
          id: "area_1",
          name: "Old pantry",
          order_hint: 0,
        }),
      ],
      { stalePatchAreaIds: ["area_1"] },
    );

    const areaRow = await screen.findByLabelText("Old pantry");
    fireEvent.click(within(areaRow).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(areaRow).getByLabelText("Name"), {
      target: { value: "Fresh pantry" },
    });
    fireEvent.click(within(areaRow).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(
        requests.filter(
          (request) => request.method === "GET" && request.path === "/w/acme/api/v1/properties/prop_1/areas",
        ).length,
      ).toBeGreaterThan(1);
    });
    expect(screen.getByLabelText("Fresh pantry")).toBeInTheDocument();
    expect(screen.queryByLabelText("Old pantry")).not.toBeInTheDocument();

    const savedRow = screen.getByLabelText("Fresh pantry");
    fireEvent.click(within(savedRow).getByRole("button", { name: "Edit" }));
    expect(within(savedRow).getByLabelText("Name")).toHaveValue("Fresh pantry");
  });

  it("disambiguates duplicate parent names with path labels and counts all descendants on delete", async () => {
    renderAreasPanel([
      areaFixture({
        id: "area_floor_1",
        name: "Floor 1",
        order_hint: 0,
      }),
      areaFixture({
        id: "area_bedroom_1",
        name: "Bedroom",
        order_hint: 0,
        parent_area_id: "area_floor_1",
      }),
      areaFixture({
        id: "area_bath",
        name: "Bathroom",
        order_hint: 0,
        parent_area_id: "area_bedroom_1",
      }),
      areaFixture({
        id: "area_floor_2",
        name: "Floor 2",
        order_hint: 1,
      }),
      areaFixture({
        id: "area_bedroom_2",
        name: "Bedroom",
        order_hint: 0,
        parent_area_id: "area_floor_2",
      }),
    ]);

    const createRow = await screen.findByLabelText("New area");
    const parentInput = within(createRow).getByRole("combobox", { name: /^Parent\b/ });
    fireEvent.focus(parentInput);
    fireEvent.change(parentInput, { target: { value: "bedroom" } });
    expect((await screen.findAllByText("Floor 1 / Bedroom")).some((node) => node.closest("[role='option']"))).toBe(
      true,
    );
    expect(screen.getAllByText("Floor 2 / Bedroom").some((node) => node.closest("[role='option']"))).toBe(true);

    const floorRow = screen.getByLabelText("Floor 1");
    fireEvent.click(within(floorRow).getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("alertdialog", { name: "Delete area?" });
    expect(dialog).toHaveTextContent("Delete Floor 1? This will also delete 2 descendant areas.");
  });

  it("falls stale parent references back to roots while preserving descendants", async () => {
    const { container } = renderAreasPanel([
      areaFixture({
        id: "area_orphan",
        name: "Legacy wing",
        order_hint: 0,
        parent_area_id: "area_missing",
      }),
      areaFixture({
        id: "area_closet",
        name: "Supply closet",
        order_hint: 0,
        parent_area_id: "area_orphan",
      }),
      areaFixture({
        id: "area_lobby",
        name: "Lobby",
        order_hint: 1,
      }),
    ]);

    expect(await screen.findByLabelText("Legacy wing")).toBeInTheDocument();
    const rowLabels = renderedRowLabels(container);
    expect(rowLabels.slice(0, 3)).toEqual(["Legacy wing", "Supply closet", "Lobby"]);
    expect(within(screen.getByLabelText("Legacy wing")).getByText("Property-level")).toBeInTheDocument();
    expect(within(screen.getByLabelText("Supply closet")).getByText("Legacy wing")).toBeInTheDocument();
  });

  it("keeps parent options cycle-safe when stale data already contains a cycle", async () => {
    renderAreasPanel([
      areaFixture({
        id: "area_a",
        name: "Cycle A",
        order_hint: 0,
        parent_area_id: "area_b",
      }),
      areaFixture({
        id: "area_b",
        name: "Cycle B",
        order_hint: 1,
        parent_area_id: "area_a",
      }),
      areaFixture({
        id: "area_safe",
        name: "Safe parent",
        order_hint: 2,
      }),
    ]);

    const cycleBRow = await screen.findByLabelText("Cycle B");
    fireEvent.click(within(cycleBRow).getByRole("button", { name: "Edit" }));
    const parentInput = within(cycleBRow).getByRole("combobox", { name: /^Parent\b/ });
    fireEvent.focus(parentInput);
    fireEvent.change(parentInput, { target: { value: "Cycle A" } });
    expect(await screen.findByText("No parent areas")).toBeInTheDocument();
    fireEvent.change(parentInput, { target: { value: "Safe parent" } });
    expect(await screen.findByRole("option", { name: /Safe parent/ })).toBeInTheDocument();
  });
});

function areaFixture(overrides: Partial<TestArea>): TestArea {
  return {
    id: "area_1",
    property_id: "prop_1",
    unit_id: null,
    name: "Kitchen",
    kind: "indoor_room",
    order_hint: 0,
    parent_area_id: null,
    notes_md: "",
    ...overrides,
  };
}
