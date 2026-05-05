import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import AssetDetailPage from "./AssetDetailPage";
import appSource from "../../App.tsx?raw";
import type { AssetAction, AssetDetailPayload } from "@/types/api";
import { jsonResponse } from "@/test/helpers";

const ACTION: AssetAction = {
  id: "act_filter",
  asset_id: "asset_1",
  key: "filter",
  label: "Clean filter",
  interval_days: 30,
  last_performed_at: "2026-04-01T09:00:00Z",
  next_due_on: "2026-05-15",
  linked_task_id: "task_1",
  linked_schedule_id: null,
  description: "Rinse and dry",
  estimated_duration_minutes: 20,
};

const DETAIL: AssetDetailPayload = {
  asset: {
    id: "asset_1",
    property_id: "prop_1",
    asset_type_id: "type_1",
    name: "Pool pump",
    area: "Pump room",
    condition: "fair",
    status: "active",
    make: "Aqua",
    model: "P2",
    serial_number: "SN-123",
    installed_on: "2024-05-01",
    purchased_on: "2024-04-20",
    purchase_price_cents: 129900,
    purchase_currency: "EUR",
    purchase_vendor: "Pool Co",
    warranty_expires_on: "2027-04-20",
    expected_lifespan_years: 8,
    guest_visible: false,
    guest_instructions: null,
    notes: null,
    qr_token: "qr_pool",
  },
  asset_type: {
    id: "type_1",
    key: "pool_pump",
    name: "Pool pump",
    category: "pool",
    icon_name: "Waves",
    default_actions: [],
    default_lifespan_years: 8,
  },
  property: {
    id: "prop_1",
    name: "Villa Rosa",
    city: "Porto",
    timezone: "Europe/Lisbon",
    color: "moss",
    kind: "str",
    areas: ["Pump room"],
    evidence_policy: "inherit",
    country: "PT",
    locale: "pt-PT",
    settings_override: {},
    client_org_id: null,
    owner_user_id: null,
  },
  actions: [ACTION],
  documents: [
    {
      id: "doc_1",
      asset_id: "asset_1",
      property_id: "prop_1",
      kind: "manual",
      title: "Pump manual",
      filename: "pump.pdf",
      size_kb: 418,
      uploaded_at: "2026-04-29T10:00:00Z",
      expires_on: null,
      amount_cents: null,
      amount_currency: null,
      extraction_status: "succeeded",
      extracted_at: "2026-04-29T10:05:00Z",
    },
  ],
  linked_tasks: [
    {
      id: "task_1",
      title: "Pump monthly service",
      property_id: "prop_1",
      area: "Pump room",
      assignee_id: "user_1",
      scheduled_start: "2026-05-15T09:00:00Z",
      estimated_minutes: 30,
      priority: "normal",
      status: "scheduled",
      checklist: [],
      photo_evidence: "disabled",
      evidence_policy: "inherit",
      instructions_ids: [],
      template_id: null,
      schedule_id: null,
      turnover_bundle_id: null,
      asset_id: "asset_1",
      settings_override: {},
      assigned_user_id: "user_1",
      workspace_id: "ws_1",
      created_by: "user_1",
      is_personal: false,
    },
  ],
};

function installFetch({ failDetail = false } = {}) {
  const requests: Array<{ url: string; method: string }> = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const method = init?.method ?? "GET";
    requests.push({ url: resolved, method });
    if (resolved === "/w/acme/api/v1/assets/asset_1" && method === "GET") {
      if (failDetail) {
        return jsonResponse({ detail: "missing" }, 404);
      }
      return jsonResponse(DETAIL);
    }
    if (resolved === "/w/acme/api/v1/assets/asset_1/actions/act_filter/complete" && method === "POST") {
      return jsonResponse({ ...ACTION, last_performed_at: "2026-04-30T10:00:00Z" }, 201);
    }
    throw new Error(`Unexpected fetch call: ${resolved}`);
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    requests,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function Harness({ initial = "/asset/asset_1" }: { initial?: string }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <WorkspaceProvider>
        <MemoryRouter initialEntries={[initial]}>
          <Routes>
            <Route path="/asset/:aid" element={<AssetDetailPage />} />
            <Route path="/w/:slug/asset/:aid" element={<AssetDetailPage />} />
          </Routes>
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

describe("<AssetDetailPage>", () => {
  it("wraps manager asset detail routes in the scope-view permission guard before the manager shell", () => {
    expect(appSource).toMatch(
      /<Route element={<RequirePermission actionKey="scope\.view" \/>}>\s*<Route element={<ManagerLayout \/>}>\s*<Route path="\/asset\/:aid" element={<AssetDetailPage \/>} \/>[\s\S]*?<Route path="\/w\/:slug\/asset\/:aid" element={<AssetDetailPage \/>} \/>/,
    );
  });

  it("renders the promoted mock structure from the production asset endpoint", async () => {
    const fake = installFetch();
    try {
      render(<Harness initial="/w/acme/asset/asset_1" />);

      expect(screen.getByText(/Loading/)).toBeInTheDocument();
      expect(await screen.findByText("Villa Rosa / Pump room")).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "Details" })).toBeInTheDocument();
      expect(screen.getByText("SN-123")).toBeInTheDocument();
      expect(screen.getByText("1299.00 EUR")).toBeInTheDocument();
      expect(screen.getByText("Clean filter")).toBeInTheDocument();

      fireEvent.click(screen.getByText("Documents"));
      expect(screen.getByText("Pump manual")).toBeInTheDocument();
      expect(screen.getByText("418 KB")).toBeInTheDocument();

      fireEvent.click(screen.getByText("History"));
      expect(screen.getByText("Pump monthly service")).toBeInTheDocument();
      expect(fake.requests[0]).toEqual({ url: "/w/acme/api/v1/assets/asset_1", method: "GET" });
    } finally {
      fake.restore();
    }
  });

  it("completes an action through the asset completion endpoint", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);
      await screen.findByText("Villa Rosa / Pump room");

      fireEvent.click(screen.getByText("Actions"));
      fireEvent.click(screen.getByRole("button", { name: "Mark done" }));

      await waitFor(() => {
        expect(fake.requests).toEqual(
          expect.arrayContaining([
            { url: "/w/acme/api/v1/assets/asset_1/actions/act_filter/complete", method: "POST" },
          ]),
        );
      });
    } finally {
      fake.restore();
    }
  });

  it("renders the mock failed-load state when the detail query fails", async () => {
    const fake = installFetch({ failDetail: true });
    try {
      render(<Harness />);
      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });
});
