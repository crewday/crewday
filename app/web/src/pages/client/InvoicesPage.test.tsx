import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import type { Me } from "@/types/api";
import InvoicesPage from "./InvoicesPage";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? "OK" : "Error",
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function mePayload(overrides: Partial<Me> = {}): Me {
  return {
    role: "client",
    theme: "system",
    agent_sidebar_collapsed: false,
    employee: {} as Me["employee"],
    manager_name: "Clara Client",
    today: "2026-04-30",
    now: "2026-04-30T12:00:00Z",
    user_id: "usr_client",
    agent_approval_mode: "strict",
    current_workspace_id: "ws_agency",
    available_workspaces: [],
    client_binding_org_ids: ["org_client"],
    is_deployment_admin: false,
    is_deployment_owner: false,
    ...overrides,
  };
}

interface TestInvoiceRow {
  id: string;
  organization_id: string;
  invoice_number: string;
  issued_at: string;
  due_at: string | null;
  total_cents: number;
  currency: string;
  status: string;
  proof_of_payment_file_ids: string[];
  pdf_url: string | null;
}

const invoiceRows: TestInvoiceRow[] = [
  {
    id: "invoice_1",
    organization_id: "org_client",
    invoice_number: "A-001",
    issued_at: "2026-04-29",
    due_at: "2026-05-29",
    total_cents: 42000,
    currency: "EUR",
    status: "approved",
    proof_of_payment_file_ids: [],
    pdf_url: null,
  },
  {
    id: "invoice_paid",
    organization_id: "org_client",
    invoice_number: "A-000",
    issued_at: "2026-04-01",
    due_at: null,
    total_cents: 125000,
    currency: "EUR",
    status: "paid",
    proof_of_payment_file_ids: ["proof_existing"],
    pdf_url: null,
  },
];

function withUploadedProof(rows: TestInvoiceRow[]): TestInvoiceRow[] {
  return rows.map((row) =>
    row.id === "invoice_1"
      ? { ...row, proof_of_payment_file_ids: ["proof_hash"] }
      : row,
  );
}

function installFetch(
  {
    role = "client",
    invoices = invoiceRows,
  }: {
    role?: Me["role"];
    invoices?: TestInvoiceRow[];
  } = {},
) {
  let proofUploaded = false;
  const requests: Array<{ url: string; method: string; body: BodyInit | null }> = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const method = init?.method ?? "GET";
    requests.push({ url: resolved, method, body: init?.body ?? null });
    if (resolved === "/api/v1/auth/me") {
      return jsonResponse({
        user_id: "usr_client",
        display_name: "Clara Client",
        email: "clara@example.com",
        available_workspaces: [],
        current_workspace_id: "ws_agency",
      });
    }
    if (resolved === "/api/v1/me/workspaces") {
      return jsonResponse([
        {
          workspace_id: "ws_agency",
          slug: "acme",
          name: "Acme Agency",
          current_role: role,
          last_seen_at: null,
          settings_override: {},
        },
      ]);
    }
    if (resolved === "/w/acme/api/v1/me") {
      return jsonResponse(mePayload({ role, client_binding_org_ids: role === "client" ? ["org_client"] : [] }));
    }
    if (resolved === "/w/acme/api/v1/client/portfolio?limit=500") {
      return jsonResponse({
        data: [
          {
            id: "prop_1",
            organization_id: "org_client",
            organization_name: "Luxe Guests",
            name: "Villa Cliente",
            kind: "str",
            address: "Porto",
            country: "PT",
            timezone: "Europe/Lisbon",
            default_currency: "EUR",
          },
        ],
        next_cursor: null,
        has_more: false,
      });
    }
    if (resolved === "/w/acme/api/v1/client/invoices?limit=500") {
      return jsonResponse({
        data: proofUploaded ? withUploadedProof(invoices) : invoices,
        next_cursor: null,
        has_more: false,
      });
    }
    if (resolved === "/w/acme/api/v1/billing/vendor-invoices/invoice_1/proof" && method === "POST") {
      proofUploaded = true;
      return jsonResponse({
        id: "invoice_1",
        status: "approved",
        proof_of_payment_file_ids: ["proof_hash"],
      });
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

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WorkspaceProvider>
          <InvoicesPage />
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

describe("<InvoicesPage>", () => {
  it("renders client portal invoice rows from the production endpoint", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("A-001")).toBeInTheDocument();
      expect(screen.getAllByText("Villa Cliente")).toHaveLength(2);
      expect(screen.getByText("€420.00")).toBeInTheDocument();
      expect(screen.getByText("approved")).toBeInTheDocument();
      expect(screen.getByText("A-000")).toBeInTheDocument();
      expect(screen.getByText("1 uploaded")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /mark paid/i })).toBeNull();
      expect(fake.requests).toContainEqual({
        url: "/w/acme/api/v1/client/invoices?limit=500",
        method: "GET",
        body: null,
      });
    } finally {
      fake.restore();
    }
  });

  it("uploads proof for approved invoices through multipart billing route", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      const file = new File(["proof"], "proof.pdf", { type: "application/pdf" });
      fireEvent.change(await screen.findByLabelText("Upload proof for A-001"), {
        target: { files: [file] },
      });

      await waitFor(() => {
        const upload = fake.requests.find((request) =>
          request.url === "/w/acme/api/v1/billing/vendor-invoices/invoice_1/proof"
        );
        expect(upload?.method).toBe("POST");
        expect(upload?.body).toBeInstanceOf(FormData);
      });
      await waitFor(() => {
        expect(screen.getAllByText("1 uploaded")).toHaveLength(2);
      });
    } finally {
      fake.restore();
    }
  });

  it("only renders upload controls for approved invoices", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByLabelText("Upload proof for A-001")).toBeInTheDocument();
      expect(screen.queryByLabelText("Upload proof for A-000")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("renders the empty state when the client has no invoices", async () => {
    const fake = installFetch({ invoices: [] });
    try {
      render(<Harness />);

      expect(await screen.findByText("No invoices billed to you yet.")).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("does not fetch client invoice data for non-client roles", async () => {
    const fake = installFetch({ role: "manager" });
    try {
      render(<Harness />);

      expect(await screen.findByText("No invoices billed to you yet.")).toBeInTheDocument();
      expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/client/invoices?limit=500")).toBe(false);
      expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/client/portfolio?limit=500")).toBe(false);
    } finally {
      fake.restore();
    }
  });
});
