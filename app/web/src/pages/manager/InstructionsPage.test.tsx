import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests, qk } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { installFetchRouteHandlers, type FetchRoute } from "@/test/helpers";
import InstructionsPage from "./InstructionsPage";

const originalShowModal = HTMLDialogElement.prototype.showModal;
const originalClose = HTMLDialogElement.prototype.close;

function instructionListPayload() {
  return {
    data: [
      {
        id: "ins_1",
        workspace_id: "ws_1",
        slug: "entry-code",
        title: "Entry code",
        scope: "property",
        property_id: "prop_1",
        property_ids: ["prop_1"],
        area_id: null,
        current_revision_id: "rev_1",
        tags: ["entry"],
        archived_at: null,
        created_by: "user_1",
        created_at: "2026-04-20T10:00:00Z",
        body_md: "Use the silver key.",
        version: 1,
        updated_at: "2026-04-21T10:00:00Z",
        area: null,
      },
    ],
    next_cursor: null,
    has_more: false,
  };
}

function propertyPayload() {
  return [
    {
      id: "prop_1",
      name: "Villa Rosa",
      city: "Porto",
      timezone: "Europe/Lisbon",
      color: "moss",
      kind: "str",
      areas: ["Kitchen"],
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
      city: "Porto",
      timezone: "Europe/Lisbon",
      color: "sky",
      kind: "str",
      areas: [],
      evidence_policy: "inherit",
      country: "PT",
      locale: "pt-PT",
      settings_override: {},
      client_org_id: null,
      owner_user_id: null,
    },
  ];
}

function areaListPayload() {
  return {
    data: [
      {
        id: "area_kitchen",
        property_id: "prop_1",
        unit_id: null,
        name: "Kitchen",
        kind: "room",
        order_hint: 0,
        parent_area_id: null,
        notes_md: "",
        created_at: "2026-04-20T10:00:00Z",
        updated_at: null,
        deleted_at: null,
      },
    ],
    next_cursor: null,
    has_more: false,
  };
}

function createdInstructionEnvelope() {
  return {
    instruction: {
      id: "ins_new",
      workspace_id: "ws_1",
      slug: "dishwasher-reset",
      title: "Dishwasher reset",
      scope: "area",
      property_id: "prop_1",
      property_ids: [],
      area_id: "area_kitchen",
      current_revision_id: "rev_new",
      tags: ["kitchen", "appliance"],
      archived_at: null,
      created_by: "user_1",
      created_at: "2026-05-09T10:00:00Z",
    },
    current_revision: {
      id: "rev_new",
      instruction_id: "ins_new",
      version: 1,
      body_md: "Hold the reset button for five seconds.",
      body_hash: "hash",
      author_id: "user_1",
      change_note: null,
      created_at: "2026-05-09T10:00:00Z",
    },
  };
}

function createdPropertyInstructionEnvelope() {
  return {
    instruction: {
      id: "ins_property",
      workspace_id: "ws_1",
      slug: "pool-rules",
      title: "Pool rules",
      scope: "property",
      property_id: "prop_1",
      property_ids: ["prop_1", "prop_2"],
      area_id: null,
      current_revision_id: "rev_property",
      tags: ["pool"],
      archived_at: null,
      created_by: "user_1",
      created_at: "2026-05-09T10:00:00Z",
    },
    current_revision: {
      id: "rev_property",
      instruction_id: "ins_property",
      version: 1,
      body_md: "Close the cover after service.",
      body_hash: "hash",
      author_id: "user_1",
      change_note: null,
      created_at: "2026-05-09T10:00:00Z",
    },
  };
}

function DetailRoute() {
  const params = useParams<{ iid: string }>();
  const location = useLocation();
  return <div>Instruction detail {params.iid}{location.search}</div>;
}

function renderInstructions(routes: FetchRoute[] = [], initial = "/w/acme/instructions") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const fetchEnv = installFetchRouteHandlers([
    {
      path: "/w/acme/api/v1/instructions",
      method: "GET",
      respond: { body: instructionListPayload() },
    },
    {
      path: "/w/acme/api/v1/properties",
      method: "GET",
      respond: { body: propertyPayload() },
    },
    {
      path: "/w/acme/api/v1/properties/prop_1/areas",
      method: "GET",
      respond: { body: areaListPayload() },
    },
    ...routes,
  ]);
  const view = render(
    <QueryClientProvider client={queryClient}>
      <WorkspaceProvider>
        <MemoryRouter initialEntries={[initial]}>
          <Routes>
            <Route path="/w/:slug/instructions" element={<InstructionsPage />} />
            <Route path="/w/:slug/property/:pid/instructions" element={<InstructionsPage />} />
            <Route path="/w/:slug/instructions/:iid" element={<DetailRoute />} />
          </Routes>
        </MemoryRouter>
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
  return { ...view, ...fetchEnv, queryClient };
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

function openTokenPicker(container: HTMLElement, label: RegExp): HTMLElement {
  const input = within(container).getByRole("combobox", { name: label });
  fireEvent.focus(input);
  return input;
}

function selectTokenOption(container: HTMLElement, inputLabel: RegExp, optionName: string): void {
  openTokenPicker(container, inputLabel);
  fireEvent.mouseDown(within(container).getByRole("option", { name: optionName }));
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

describe("<InstructionsPage>", () => {
  it("creates an area-scoped instruction from the inline create row", async () => {
    const { requests, queryClient } = renderInstructions([
      {
        path: "/w/acme/api/v1/instructions",
        method: "POST",
        respond: { status: 201, body: createdInstructionEnvelope() },
      },
    ]);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    const row = await screen.findByLabelText("New instruction");
    expect(within(row).getByRole("button", { name: "Save" })).toBeDisabled();

    fireEvent.change(within(row).getByLabelText(/^Instruction title\b/), {
      target: { value: "Dishwasher reset" },
    });
    fireEvent.change(within(row).getByLabelText(/^Markdown\b/), {
      target: { value: "Hold the reset button for five seconds." },
    });
    fireEvent.change(within(row).getByLabelText(/^Instruction scope\b/), { target: { value: "area" } });
    await chooseSearchableOption(row, /^Instruction property\b/, "Villa Rosa");

    const area = await within(row).findByRole("combobox", { name: /^Instruction area\b/ });
    await waitFor(() => expect(area).not.toBeDisabled());
    await chooseSearchableOption(row, /^Instruction area\b/, "Kitchen");
    let tagInput = within(row).getByLabelText("Add instruction tag");
    fireEvent.change(tagInput, { target: { value: "kitchen" } });
    await within(row).findByText('Press Enter to add "kitchen"');
    tagInput = within(row).getByLabelText("Add instruction tag");
    fireEvent.keyDown(tagInput, { key: "Enter" });
    tagInput = within(row).getByLabelText("Add instruction tag");
    fireEvent.change(tagInput, { target: { value: "appliance" } });
    await within(row).findByText('Press Enter to add "appliance"');
    tagInput = within(row).getByLabelText("Add instruction tag");
    fireEvent.keyDown(tagInput, { key: "Enter" });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    await screen.findByText("Instruction detail ins_new");
    const post = requests.find(
      (request) =>
        request.path === "/w/acme/api/v1/instructions" && request.method === "POST",
    );
    expect(post?.body).toEqual({
      slug: "dishwasher-reset",
      title: "Dishwasher reset",
      body_md: "Hold the reset button for five seconds.",
      scope: "area",
      property_id: "prop_1",
      area_id: "area_kitchen",
      tags: ["kitchen", "appliance"],
      change_note: null,
    });
    expect(queryClient.getQueryData(qk.instruction("ins_new"))).toMatchObject({
      id: "ins_new",
      title: "Dishwasher reset",
      area: "area_kitchen",
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: qk.instructions() });
  });

  it("keeps incomplete property-scoped input in the inline row without posting", async () => {
    const { requests } = renderInstructions([
      {
        path: "/w/acme/api/v1/instructions",
        method: "POST",
        respond: { status: 201, body: createdInstructionEnvelope() },
      },
    ]);

    const row = await screen.findByLabelText("New instruction");

    fireEvent.change(within(row).getByLabelText(/^Instruction title\b/), {
      target: { value: "Pool rules" },
    });
    fireEvent.change(within(row).getByLabelText(/^Markdown\b/), {
      target: { value: "Close the cover after service." },
    });
    fireEvent.change(within(row).getByLabelText(/^Instruction scope\b/), { target: { value: "property" } });

    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(
        requests.some(
          (request) =>
            request.path === "/w/acme/api/v1/instructions" && request.method === "POST",
        ),
      ).toBe(false);
    });
    expect(screen.getByText("Select at least one property.")).toBeInTheDocument();
  });

  it("creates a property-scoped instruction with multiple property ids", async () => {
    const { requests } = renderInstructions([
      {
        path: "/w/acme/api/v1/instructions",
        method: "POST",
        respond: { status: 201, body: createdPropertyInstructionEnvelope() },
      },
    ]);

    const row = await screen.findByLabelText("New instruction");
    fireEvent.change(within(row).getByLabelText(/^Instruction title\b/), {
      target: { value: "Pool rules" },
    });
    fireEvent.change(within(row).getByLabelText(/^Markdown\b/), {
      target: { value: "Close the cover after service." },
    });
    fireEvent.change(within(row).getByLabelText(/^Instruction scope\b/), { target: { value: "property" } });
    expect(within(row).queryByRole("option", { name: "Villa Rosa" })).not.toBeInTheDocument();
    selectTokenOption(row, /^Filter properties\b/, "Villa Rosa");
    selectTokenOption(row, /^Filter properties\b/, "Casa Azul");
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    await screen.findByText("Instruction detail ins_property");
    const post = requests.find(
      (request) =>
        request.path === "/w/acme/api/v1/instructions" && request.method === "POST",
    );
    expect(post?.body).toMatchObject({
      scope: "property",
      property_id: "prop_1",
      property_ids: ["prop_1", "prop_2"],
      area_id: null,
    });
  });

  it("loads property-tab instructions through the property-scoped list filter", async () => {
    const { requests } = renderInstructions([
      {
        path: "/w/acme/api/v1/instructions?property_id=prop_1",
        method: "GET",
        respond: { body: instructionListPayload() },
      },
    ], "/w/acme/property/prop_1/instructions");

    await screen.findByText("Entry code");
    const relatedPages = screen.getByRole("navigation", { name: "Related property pages" });
    expect(within(relatedPages).getByRole("link", { name: "Instructions" })).toHaveAttribute("aria-current", "page");
    expect(
      requests.some(
        (request) =>
          request.path === "/w/acme/api/v1/instructions?property_id=prop_1" &&
          request.method === "GET",
      ),
    ).toBe(true);
  });

  it("defaults property-tab inline create to the current property and preserves detail context", async () => {
    const { requests } = renderInstructions([
      {
        path: "/w/acme/api/v1/instructions?property_id=prop_1",
        method: "GET",
        respond: { body: instructionListPayload() },
      },
      {
        path: "/w/acme/api/v1/instructions",
        method: "POST",
        respond: { status: 201, body: createdPropertyInstructionEnvelope() },
      },
    ], "/w/acme/property/prop_1/instructions");

    const row = await screen.findByLabelText("New instruction");
    expect(within(row).getByLabelText(/^Instruction scope\b/)).toHaveValue("property");
    expect(within(row).getByText("Villa Rosa")).toBeInTheDocument();
    expect(within(row).queryByRole("option", { name: "Casa Azul" })).not.toBeInTheDocument();

    fireEvent.change(within(row).getByLabelText(/^Instruction title\b/), {
      target: { value: "Pool rules" },
    });
    fireEvent.change(within(row).getByLabelText(/^Markdown\b/), {
      target: { value: "Close the cover after service." },
    });
    selectTokenOption(row, /^Filter properties\b/, "Casa Azul");
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));

    await screen.findByText("Instruction detail ins_property?property_id=prop_1");
    const post = requests.find(
      (request) =>
        request.path === "/w/acme/api/v1/instructions" && request.method === "POST",
    );
    expect(post?.body).toMatchObject({
      scope: "property",
      property_id: "prop_1",
      property_ids: ["prop_1", "prop_2"],
      area_id: null,
    });
  });

  it("keeps top-level instructions unfiltered and links detail without property context", async () => {
    const { requests } = renderInstructions();

    const rowLink = await screen.findByRole("link", { name: /Entry code/ });
    expect(rowLink).toHaveAttribute("href", "/w/acme/instructions/ins_1");
    expect(
      requests.some(
        (request) =>
          request.path === "/w/acme/api/v1/instructions" &&
          request.method === "GET",
      ),
    ).toBe(true);
    expect(
      requests.some((request) => request.path.includes("/instructions?property_id=")),
    ).toBe(false);
  });
});
