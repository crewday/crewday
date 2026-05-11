import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useParams } from "react-router-dom";
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

function DetailRoute() {
  const params = useParams<{ iid: string }>();
  return <div>Instruction detail {params.iid}</div>;
}

function renderInstructions(routes: FetchRoute[] = []) {
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
        <MemoryRouter initialEntries={["/w/acme/instructions"]}>
          <Routes>
            <Route path="/w/:slug/instructions" element={<InstructionsPage />} />
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
  await within(container).findByText(query);
  fireEvent.keyDown(input, { key: "Enter" });
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
  it("opens the create flow and creates an area-scoped instruction", async () => {
    const { requests, queryClient } = renderInstructions([
      {
        path: "/w/acme/api/v1/instructions",
        method: "POST",
        respond: { status: 201, body: createdInstructionEnvelope() },
      },
    ]);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    fireEvent.click(await screen.findByRole("button", { name: "+ New instruction" }));
    const dialog = screen.getByRole("dialog", { name: "Create instruction" });
    expect(within(dialog).getByRole("button", { name: "Create" })).toBeDisabled();

    fireEvent.change(within(dialog).getByLabelText(/^Title\b/), {
      target: { value: "Dishwasher reset" },
    });
    fireEvent.change(within(dialog).getByLabelText(/^Markdown\b/), {
      target: { value: "Hold the reset button for five seconds." },
    });
    fireEvent.change(within(dialog).getByLabelText(/^Scope\b/), { target: { value: "area" } });
    await chooseSearchableOption(dialog, /^Property\b/, "Villa Rosa");

    const area = await within(dialog).findByRole("combobox", { name: /^Area\b/ });
    await waitFor(() => expect(area).not.toBeDisabled());
    await chooseSearchableOption(dialog, /^Area\b/, "Kitchen");
    fireEvent.change(within(dialog).getByLabelText(/^Tags\b/), {
      target: { value: "kitchen, appliance" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));

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

  it("keeps incomplete property-scoped input in the dialog without posting", async () => {
    const { requests } = renderInstructions([
      {
        path: "/w/acme/api/v1/instructions",
        method: "POST",
        respond: { status: 201, body: createdInstructionEnvelope() },
      },
    ]);

    fireEvent.click(await screen.findByRole("button", { name: "+ New instruction" }));
    const dialog = screen.getByRole("dialog", { name: "Create instruction" });

    fireEvent.change(within(dialog).getByLabelText(/^Title\b/), {
      target: { value: "Pool rules" },
    });
    fireEvent.change(within(dialog).getByLabelText(/^Markdown\b/), {
      target: { value: "Close the cover after service." },
    });
    fireEvent.change(within(dialog).getByLabelText(/^Scope\b/), { target: { value: "property" } });

    const createButton = within(dialog).getByRole("button", { name: "Create" });
    expect(createButton).toBeDisabled();
    fireEvent.submit(createButton.closest("form")!);

    await waitFor(() => {
      expect(
        requests.some(
          (request) =>
            request.path === "/w/acme/api/v1/instructions" && request.method === "POST",
        ),
      ).toBe(false);
    });
    expect(screen.getByRole("dialog", { name: "Create instruction" })).toBeInTheDocument();
  });
});
