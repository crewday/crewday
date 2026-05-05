import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests, qk } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import InstructionDetailPage from "./InstructionDetailPage";
import { jsonResponse } from "@/test/helpers";

function revisionEnvelope(
  body = "Use the silver key.",
  version = body.includes("brass") ? 2 : 1,
): unknown {
  return {
    id: `rev_${version}`,
    instruction_id: "ins_1",
    version,
    body_md: body,
    body_hash: "hash",
    author_id: "user_1",
    change_note: version > 1 ? "updated key material" : null,
    created_at: version > 1 ? "2026-04-22T10:00:00Z" : "2026-04-21T10:00:00Z",
  };
}

function instructionEnvelope(body = "Use the silver key."): unknown {
  return {
    instruction: {
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
    },
    current_revision: revisionEnvelope(body),
  };
}

interface FetchOptions {
  instructionStatus?: number;
  never?: boolean;
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
      areas: ["Entry"],
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
        id: "area_1",
        property_id: "prop_1",
        unit_id: null,
        name: "Entry",
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

function installFetch(options: FetchOptions = {}) {
  const requests: Array<{ url: string; init: RequestInit | undefined }> = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    requests.push({ url: resolved, init });
    if (options.never) {
      return new Promise<Response>(() => undefined);
    }
    if (resolved === "/w/acme/api/v1/instructions/ins_1" && init?.method === "PATCH") {
      return jsonResponse(instructionEnvelope("Use the brass key."));
    }
    if (resolved === "/w/acme/api/v1/instructions/ins_1") {
      if (options.instructionStatus) {
        return jsonResponse({ type: "not_found", title: "Not found" }, options.instructionStatus);
      }
      return jsonResponse(instructionEnvelope());
    }
    if (resolved === "/w/acme/api/v1/properties") {
      return jsonResponse(propertyPayload());
    }
    if (resolved === "/w/acme/api/v1/properties/prop_1/areas") {
      return jsonResponse(areaListPayload());
    }
    if (resolved === "/w/acme/api/v1/instructions/ins_1/versions") {
      return jsonResponse({
        data: [
          revisionEnvelope("Use the brass key.", 2),
          revisionEnvelope("Use the silver key.", 1),
        ],
        next_cursor: null,
        has_more: false,
      });
    }
    return jsonResponse({ type: "not_found", title: "Not found" }, 404);
  });
  globalThis.fetch = spy as unknown as typeof fetch;
  return {
    requests,
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

function makeQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function Harness({ queryClient = makeQueryClient() }: { queryClient?: QueryClient }) {
  // code-health: ignore[nloc] Instruction detail route harness keeps providers and route setup local to the test.
  return (
    <QueryClientProvider client={queryClient}>
      <WorkspaceProvider>
        <MemoryRouter initialEntries={["/instructions/ins_1"]}>
          <Routes>
            <Route path="/instructions/:iid" element={<InstructionDetailPage />} />
          </Routes>
        </MemoryRouter>
      </WorkspaceProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  if (!HTMLDialogElement.prototype.showModal) {
    HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    };
  }
  if (!HTMLDialogElement.prototype.close) {
    HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
      this.removeAttribute("open");
    };
  }
  vi.spyOn(HTMLDialogElement.prototype, "showModal").mockImplementation(function showModal(
    this: HTMLDialogElement,
  ) {
    this.setAttribute("open", "");
  });
  vi.spyOn(HTMLDialogElement.prototype, "close").mockImplementation(function close(
    this: HTMLDialogElement,
  ) {
    this.removeAttribute("open");
  });
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<InstructionDetailPage>", () => {
  it("shows the loading state while instruction data is pending", () => {
    const fake = installFetch({ never: true });
    try {
      render(<Harness />);

      expect(screen.getByText(/Loading/)).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("shows the standard failed state when the instruction request fails", async () => {
    const fake = installFetch({ instructionStatus: 404 });
    try {
      render(<Harness />);

      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("renders the instruction detail and saves a body edit", async () => {
    const fake = installFetch();
    const queryClient = makeQueryClient();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    try {
      render(<Harness queryClient={queryClient} />);

      expect(await screen.findByRole("heading", { name: "Entry code" })).toBeInTheDocument();
      expect(screen.getByText("Use the silver key.")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Edit" }));
      const markdown = screen.getByLabelText("Markdown");
      fireEvent.change(markdown, { target: { value: "Use the brass key." } });
      fireEvent.change(screen.getByLabelText("Change note"), {
        target: { value: "updated key material" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(screen.getByText("Use the brass key.")).toBeInTheDocument();
      });
      const patch = fake.requests.find(
        (request) =>
          request.url === "/w/acme/api/v1/instructions/ins_1" &&
          request.init?.method === "PATCH",
      );
      expect(patch).toBeDefined();
      expect(JSON.parse(String(patch?.init?.body))).toMatchObject({
        body_md: "Use the brass key.",
        change_note: "updated key material",
      });
      expect(invalidate).toHaveBeenCalledWith({ queryKey: qk.instructions() });
      expect(invalidate).toHaveBeenCalledWith({ queryKey: qk.instructionVersions("ins_1") });
    } finally {
      fake.restore();
    }
  });

  it("sends a selected area id when changing scope to area", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByRole("heading", { name: "Entry code" })).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Edit" }));
      fireEvent.change(screen.getByLabelText("Scope"), { target: { value: "area" } });

      const area = await screen.findByLabelText("Area");
      await waitFor(() => expect(area).not.toBeDisabled());
      fireEvent.change(area, { target: { value: "area_1" } });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(screen.getByText("Use the brass key.")).toBeInTheDocument();
      });
      const patch = fake.requests.find(
        (request) =>
          request.url === "/w/acme/api/v1/instructions/ins_1" &&
          request.init?.method === "PATCH",
      );
      expect(JSON.parse(String(patch?.init?.body))).toMatchObject({
        scope: "area",
        property_id: "prop_1",
        area_id: "area_1",
      });
    } finally {
      fake.restore();
    }
  });

  it("opens the version history drawer from the overflow action", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByRole("heading", { name: "Entry code" })).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "More actions" }));
      fireEvent.click(screen.getByRole("menuitem", { name: "View revisions" }));

      expect(await screen.findByRole("dialog", { name: "Instruction history" })).toBeInTheDocument();
      expect(await screen.findByText("Use the brass key.")).toBeInTheDocument();
      expect(screen.getByText("updated key material")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Close instruction history" })).toBeInTheDocument();

      fireEvent.keyDown(window, { key: "Escape" });
      await waitFor(() => {
        expect(screen.queryByRole("dialog", { name: "Instruction history" })).not.toBeInTheDocument();
      });
    } finally {
      fake.restore();
    }
  });
});
