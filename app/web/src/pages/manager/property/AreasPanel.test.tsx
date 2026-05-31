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

  it("disables reorder dragging while the create row has unsaved input", async () => {
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
    expect(poolRow).toHaveAttribute("draggable", "true");
    expect(screen.getByLabelText("Drag Pool to reorder")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /move .* up/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /move .* down/i })).toBeNull();

    const createRow = screen.getByLabelText("New area");
    fireEvent.change(within(createRow).getByLabelText("Name"), {
      target: { value: "Pantry" },
    });

    expect(poolRow).not.toHaveAttribute("draggable");
    expect(screen.getByLabelText("Drag Pool to reorder")).toBeInTheDocument();

    fireEvent.click(within(createRow).getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(poolRow).toHaveAttribute("draggable", "true");
    });
  });

  it("persists area reorders through hidden order hints", async () => {
    const { container, requests } = renderAreasPanel([
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
    dragAreaAfter("Kitchen", "Pool");

    await waitFor(() => {
      expect(requests.filter((request) => request.method === "PATCH")).toHaveLength(2);
    });
    expect(
      requests.find((request) => request.path === "/w/acme/api/v1/areas/area_2")?.body,
    ).toMatchObject({
      order_hint: 0,
    });
    expect(
      requests.find((request) => request.path === "/w/acme/api/v1/areas/area_1")?.body,
    ).toMatchObject({
      order_hint: 1,
    });

    await waitFor(() => {
      const rowLabels = Array.from(container.querySelectorAll("[data-inline-table-row-group]")).map((row) =>
        row.getAttribute("aria-label"),
      );
      expect(rowLabels.slice(0, 2)).toEqual(["Pool", "Kitchen"]);
    });
  });

  it("persists drag reorders through the shared row affordance", async () => {
    const { container, requests } = renderAreasPanel([
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
    const kitchenRow = screen.getByLabelText("Kitchen");
    const poolRow = screen.getByLabelText("Pool");
    const transfer = dataTransfer();
    fireEvent.dragStart(kitchenRow, { dataTransfer: transfer });
    fireEvent.dragOver(poolRow, { dataTransfer: transfer });
    fireEvent.drop(poolRow, { dataTransfer: transfer });
    fireEvent.dragEnd(kitchenRow, { dataTransfer: transfer });

    await waitFor(() => {
      expect(requests.filter((request) => request.method === "PATCH")).toHaveLength(2);
    });
    expect(
      requests.find((request) => request.path === "/w/acme/api/v1/areas/area_2")?.body,
    ).toMatchObject({
      order_hint: 0,
    });
    expect(
      requests.find((request) => request.path === "/w/acme/api/v1/areas/area_1")?.body,
    ).toMatchObject({
      order_hint: 1,
    });

    await waitFor(() => {
      const rowLabels = Array.from(container.querySelectorAll("[data-inline-table-row-group]")).map((row) =>
        row.getAttribute("aria-label"),
      );
      expect(rowLabels.slice(0, 2)).toEqual(["Pool", "Kitchen"]);
    });
  });

  it("refetches the canonical area order when a reorder patch fails", async () => {
    const { container, requests } = renderAreasPanel(
      [
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
      ],
      { failPatchAreaIds: ["area_1"] },
    );

    await screen.findByText("Kitchen");
    dragAreaAfter("Kitchen", "Pool");

    expect(await screen.findByText("Reorder failed.")).toBeInTheDocument();
    await waitFor(() => {
      expect(
        requests.filter(
          (request) => request.method === "GET" && request.path === "/w/acme/api/v1/properties/prop_1/areas",
        ).length,
      ).toBeGreaterThan(1);
    });
    const rowLabels = Array.from(container.querySelectorAll("[data-inline-table-row-group]")).map((row) =>
      row.getAttribute("aria-label"),
    );
    expect(rowLabels.slice(0, 2)).toEqual(["Kitchen", "Pool"]);
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
});

function dragAreaAfter(sourceLabel: string, targetLabel: string): void {
  const sourceRow = screen.getByLabelText(sourceLabel);
  const targetRow = screen.getByLabelText(targetLabel);
  const transfer = dataTransfer();
  fireEvent.dragStart(sourceRow, { dataTransfer: transfer });
  fireEvent.dragOver(targetRow, { dataTransfer: transfer });
  fireEvent.drop(targetRow, { dataTransfer: transfer });
  fireEvent.dragEnd(sourceRow, { dataTransfer: transfer });
}

function dataTransfer(): DataTransfer {
  return {
    effectAllowed: "",
    dropEffect: "",
    setData() {},
    getData() {
      return "";
    },
  } as unknown as DataTransfer;
}

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
