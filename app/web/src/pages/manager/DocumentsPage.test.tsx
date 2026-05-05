import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import DocumentsPage from "./DocumentsPage";
import appSource from "../../App.tsx?raw";
import type { Asset, AssetDocument, Property } from "@/types/api";
import { jsonResponse } from "@/test/helpers";

const DOCUMENTS: AssetDocument[] = [
  {
    id: "doc_1",
    asset_id: "asset_1",
    property_id: "prop_1",
    kind: "manual",
    title: "Pool pump manual",
    filename: "pump.pdf",
    size_kb: 418,
    uploaded_at: "2026-04-29T10:00:00Z",
    expires_on: null,
    amount_cents: null,
    amount_currency: null,
    extraction_status: "succeeded",
    extracted_at: "2026-04-29T10:05:00Z",
  },
  {
    id: "doc_2",
    asset_id: null,
    property_id: "prop_2",
    kind: "warranty",
    title: "Boiler warranty",
    filename: "boiler.pdf",
    size_kb: 96,
    uploaded_at: "2026-04-28T10:00:00Z",
    expires_on: "2026-05-01",
    amount_cents: 12900,
    amount_currency: "EUR",
    extraction_status: "failed",
    extracted_at: null,
  },
];

const ASSETS: Asset[] = [
  {
    id: "asset_1",
    property_id: "prop_1",
    asset_type_id: null,
    name: "Pool pump",
    area: "Pool",
    condition: "good",
    status: "active",
    make: "Aqua",
    model: "P2",
    serial_number: null,
    installed_on: null,
    purchased_on: null,
    purchase_price_cents: null,
    purchase_currency: null,
    purchase_vendor: null,
    warranty_expires_on: null,
    expected_lifespan_years: null,
    guest_visible: false,
    guest_instructions: null,
    notes: null,
    qr_token: "qr_1",
  },
];

const PROPERTIES: Property[] = [
  {
    id: "prop_1",
    name: "Villa Rosa",
    city: "Porto",
    timezone: "Europe/Lisbon",
    color: "moss",
    kind: "str",
    areas: ["Pool"],
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
    city: "Lisbon",
    timezone: "Europe/Lisbon",
    color: "sky",
    kind: "residence",
    areas: ["Utility"],
    evidence_policy: "inherit",
    country: "PT",
    locale: "pt-PT",
    settings_override: {},
    client_org_id: null,
    owner_user_id: null,
  },
];

function installFetch() {
  const calls: string[] = [];
  const requests: Array<{ url: string; method: string }> = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const method = init?.method ?? "GET";
    calls.push(resolved);
    requests.push({ url: resolved, method });
    if (resolved === "/w/acme/api/v1/documents") {
      return jsonResponse({ data: DOCUMENTS });
    }
    if (resolved === "/w/acme/api/v1/assets") {
      return jsonResponse({ data: ASSETS });
    }
    if (resolved === "/w/acme/api/v1/properties") {
      return jsonResponse(PROPERTIES);
    }
    if (resolved === "/w/acme/api/v1/documents/doc_1/extraction" && method === "GET") {
      return jsonResponse({
        document_id: "doc_1",
        status: "succeeded",
        extractor: "pypdf",
        body_preview: "Page one text",
        page_count: 2,
        token_count: 1234,
        has_secret_marker: true,
        last_error: null,
        extracted_at: "2026-04-29T10:05:00Z",
      });
    }
    if (resolved === "/w/acme/api/v1/documents/doc_1/extraction/pages/1") {
      return jsonResponse({
        page: 1,
        char_start: 0,
        char_end: 32,
        body: "Pump must be isolated before service.",
        more_pages: true,
      });
    }
    if (resolved === "/w/acme/api/v1/documents/doc_1/extraction/retry" && method === "POST") {
      return jsonResponse(null, 202);
    }
    throw new Error(`Unexpected fetch call: ${resolved}`);
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    requests,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
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
          <DocumentsPage />
        </WorkspaceProvider>
      </MemoryRouter>
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

describe("<DocumentsPage>", () => {
  it("wraps the real documents route in the document-management permission guard", () => {
    expect(appSource).toMatch(
      /<Route element={<RequirePermission actionKey="assets\.manage_documents" \/>}>\s*<Route element={<ManagerLayout \/>}>\s*<Route path="\/documents" element={<DocumentsPage \/>} \/>/,
    );
  });

  it("renders the promoted mock table from production endpoints", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);
      expect(await screen.findByText("Pool pump manual")).toBeInTheDocument();
      expect(screen.getByText("Manuals, warranties, invoices, and permits across all properties.")).toBeInTheDocument();
      expect(screen.getByText("pump.pdf")).toBeInTheDocument();
      expect(screen.getByText("Pool pump")).toBeInTheDocument();
      expect(screen.getByText("01 May 2026")).toBeInTheDocument();
      expect(screen.getByText("129.00 EUR")).toBeInTheDocument();
      expect(fake.calls).not.toContain("/w/acme/api/v1/documents/doc_1/extraction");

      const warrantyFilter = screen.getAllByText("warranty")[0];
      if (!warrantyFilter) throw new Error("Missing warranty filter");
      fireEvent.click(warrantyFilter);
      expect(screen.queryByText("Pool pump manual")).toBeNull();
      expect(screen.getByText("Boiler warranty")).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("loads extraction metadata and text pages lazily, then retries extraction", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);
      await screen.findByText("Pool pump manual");
      expect(fake.calls).not.toContain("/w/acme/api/v1/documents/doc_1/extraction");
      expect(fake.calls).not.toContain("/w/acme/api/v1/documents/doc_1/extraction/pages/1");

      fireEvent.click(screen.getByText("indexed"));
      expect(await screen.findByText("pypdf")).toBeInTheDocument();
      expect(screen.getByText("1,234")).toBeInTheDocument();
      expect(screen.getByText(/Extraction found a value that looks like a password or access code/)).toBeInTheDocument();
      expect(fake.calls).not.toContain("/w/acme/api/v1/documents/doc_1/extraction/pages/1");

      fireEvent.click(screen.getByText("Extracted text"));
      expect(await screen.findByText("Pump must be isolated before service.")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await waitFor(() => {
        expect(fake.requests).toEqual(
          expect.arrayContaining([
            { url: "/w/acme/api/v1/documents/doc_1/extraction/retry", method: "POST" },
          ]),
        );
      });
    } finally {
      fake.restore();
    }
  });
});
