import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import type { Me } from "@/types/api";
import QuotesPage from "./QuotesPage";
import { installFetchRouteHandlers } from "@/test/helpers";

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

interface TestQuoteRow {
  id: string;
  organization_id: string;
  property_id: string;
  title: string;
  total_cents: number;
  currency: string;
  status: string;
  sent_at: string | null;
  decided_at: string | null;
  accept_url: string | null;
}

const quoteRows: TestQuoteRow[] = [
  {
    id: "quote_1",
    organization_id: "org_client",
    property_id: "prop_1",
    title: "Replace terrace rail",
    total_cents: 125000,
    currency: "EUR",
    status: "sent",
    sent_at: "2026-04-29T09:00:00Z",
    decided_at: null,
    accept_url: "/w/acme/api/v1/billing/quotes/quote_1/accept",
  },
  {
    id: "quote_2",
    organization_id: "org_client",
    property_id: "prop_1",
    title: "Pool pump repair",
    total_cents: 42000,
    currency: "EUR",
    status: "accepted",
    sent_at: "2026-04-28T09:00:00Z",
    decided_at: "2026-04-30T10:00:00Z",
    accept_url: null,
  },
];

function installFetch(
  {
    role = "client",
    quotes = quoteRows,
    quotesAfterDecision,
    pendingApproval = false,
  }: {
    role?: Me["role"];
    quotes?: typeof quoteRows;
    quotesAfterDecision?: typeof quoteRows;
    pendingApproval?: boolean;
  } = {},
) {
  // code-health: ignore[nloc] Route fixtures stay local; shared fetch mechanics live in test/helpers.
  let quoteListCalls = 0;
  const env = installFetchRouteHandlers([
    {
      path: "/api/v1/auth/me",
      respond: {
        body: {
          user_id: "usr_client",
          display_name: "Clara Client",
          email: "clara@example.com",
          available_workspaces: [],
          current_workspace_id: "ws_agency",
        },
      },
    },
    {
      path: "/api/v1/me/workspaces",
      respond: () => ({
        body: [{
          workspace_id: "ws_agency",
          slug: "acme",
          name: "Acme Agency",
          current_role: role,
          last_seen_at: null,
          settings_override: {},
        }],
      }),
    },
    {
      path: "/w/acme/api/v1/me",
      respond: () => ({
        body: mePayload({ role, client_binding_org_ids: role === "client" ? ["org_client"] : [] }),
      }),
    },
    {
      path: "/w/acme/api/v1/client/portfolio?limit=500",
      respond: {
        body: {
          data: [{
            id: "prop_1",
            organization_id: "org_client",
            organization_name: "Luxe Guests",
            name: "Villa Cliente",
            kind: "str",
            address: "Porto",
            country: "PT",
            timezone: "Europe/Lisbon",
            default_currency: "EUR",
          }],
          next_cursor: null,
          has_more: false,
        },
      },
    },
    {
      path: "/w/acme/api/v1/client/quotes?limit=500",
      respond: () => {
        quoteListCalls += 1;
        return {
          body: {
            data: quoteListCalls > 1 && quotesAfterDecision ? quotesAfterDecision : quotes,
            next_cursor: null,
            has_more: false,
          },
        };
      },
    },
    {
      path: "/w/acme/api/v1/billing/quotes/quote_1/accept",
      method: "POST",
      respond: () => ({
        body: pendingApproval
          ? { status: "pending_approval", approval_request_id: "approval_1" }
          : { ...quoteRows[0]!, status: "accepted", decided_at: "2026-04-30T12:00:00Z" },
      }),
    },
    {
      path: "/w/acme/api/v1/billing/quotes/quote_1/reject",
      method: "POST",
      respond: {
        body: { ...quoteRows[0]!, status: "rejected", decided_at: "2026-04-30T12:00:00Z" },
      },
    },
  ]);
  return {
    get requests() {
      return env.requests.map(({ url, method, body }) => ({ url, method, body }));
    },
    restore: env.restore,
  };
}

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WorkspaceProvider>
          <QuotesPage />
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

describe("<QuotesPage>", () => {
  it("renders client portal quote rows from the production endpoint", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Replace terrace rail")).toBeInTheDocument();
      expect(screen.getAllByText("Villa Cliente")).toHaveLength(2);
      expect(screen.getByText("€1,250.00")).toBeInTheDocument();
      expect(screen.getByText("sent")).toBeInTheDocument();
      expect(screen.getByText("Pool pump repair")).toBeInTheDocument();
      expect(screen.getByText("accepted")).toBeInTheDocument();
      expect(fake.requests).toContainEqual({
        url: "/w/acme/api/v1/client/quotes?limit=500",
        method: "GET",
        body: null,
      });
      expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/work_orders")).toBe(false);
      expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/quotes")).toBe(false);
    } finally {
      fake.restore();
    }
  });

  it("accepts a sent quote through its scoped accept URL", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "Accept" }));

      await waitFor(() => {
        expect(fake.requests).toContainEqual({
          url: "/w/acme/api/v1/billing/quotes/quote_1/accept",
          method: "POST",
          body: null,
        });
      });
    } finally {
      fake.restore();
    }
  });

  it("rejects a sent quote through the billing route", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "Reject" }));

      await waitFor(() => {
        expect(fake.requests).toContainEqual({
          url: "/w/acme/api/v1/billing/quotes/quote_1/reject",
          method: "POST",
          body: null,
        });
      });
    } finally {
      fake.restore();
    }
  });

  it("shows pending approval when the server returns an approval request", async () => {
    const fake = installFetch({ pendingApproval: true });
    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "Accept" }));

      expect(await screen.findByText("pending approval")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("does not let pending approval state mask a resolved quote refetch", async () => {
    const fake = installFetch({
      pendingApproval: true,
      quotes: [quoteRows[0]!],
      quotesAfterDecision: [
        {
          ...quoteRows[0]!,
          status: "accepted",
          decided_at: "2026-04-30T12:00:00Z",
          accept_url: null,
        },
      ],
    });
    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "Accept" }));

      expect(await screen.findByText("accepted")).toBeInTheDocument();
      expect(screen.queryByText("pending approval")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("renders the empty state when there are no open quotes", async () => {
    const fake = installFetch({ quotes: [] });
    try {
      render(<Harness />);

      expect(await screen.findByText("No open quotes.")).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("does not fetch client quote data for non-client roles", async () => {
    const fake = installFetch({ role: "manager" });
    try {
      render(<Harness />);

      expect(await screen.findByText("No open quotes.")).toBeInTheDocument();
      expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/client/quotes?limit=500")).toBe(false);
      expect(fake.requests.some((request) => request.url === "/w/acme/api/v1/client/portfolio?limit=500")).toBe(false);
    } finally {
      fake.restore();
    }
  });
});
