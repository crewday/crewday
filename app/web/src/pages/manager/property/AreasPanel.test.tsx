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

function renderAreasPanel(initialAreas: TestArea[] = []) {
  let areas = [...initialAreas];
  const routes: FetchRoute[] = [
    {
      path: "/w/acme/api/v1/properties/prop_1/areas",
      respond: () => ({ body: { data: areas, next_cursor: null, has_more: false } }),
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
  ];
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const fetchEnv = installFetchRouteHandlers(routes);
  const view = render(
    <QueryClientProvider client={queryClient}>
      <AreasPanel propertyId="prop_1" />
    </QueryClientProvider>,
  );
  return { ...view, ...fetchEnv };
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
    renderAreasPanel();

    expect(await screen.findByRole("table", { name: "Property areas" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New area" })).not.toBeInTheDocument();
    const createRow = screen.getByLabelText("New area");

    expect(createRow).toHaveClass("inline-table-form__group--trailing-create", "is-editing");
    expect(within(createRow).getByLabelText("Name")).toBeInTheDocument();
    expect(within(createRow).getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.queryByRole("heading", { name: "No areas yet" })).not.toBeInTheDocument();
  });

  it("creates from the default row and leaves a fresh empty row ready", async () => {
    const { requests } = renderAreasPanel();

    const createRow = await screen.findByLabelText("New area");
    fireEvent.change(within(createRow).getByLabelText("Name"), {
      target: { value: "Pool" },
    });
    fireEvent.change(within(createRow).getByLabelText("Kind"), {
      target: { value: "outdoor" },
    });
    fireEvent.change(within(createRow).getByLabelText("Order"), {
      target: { value: "3" },
    });
    fireEvent.click(within(createRow).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Pool")).toBeInTheDocument();
    const createRequest = requests.find((request) => request.method === "POST");
    expect(createRequest?.path).toBe("/w/acme/api/v1/properties/prop_1/areas");
    expect(createRequest?.body).toMatchObject({
      name: "Pool",
      kind: "outdoor",
      order_hint: 3,
      parent_area_id: null,
    });

    await waitFor(() => {
      expect(within(screen.getByLabelText("New area")).getByLabelText("Name")).toHaveValue("");
    });
  });
});
