import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { installFetchRouteHandlers } from "@/test/helpers";
import ExpensesApprovalsPage from "./ExpensesApprovalsPage";

function installFetch() {
  return installFetchRouteHandlers([
    {
      path: "/api/v1/auth/me",
      respond: {
        body: {
          user_id: "usr_1",
          display_name: "Mina",
          email: "mina@example.com",
          available_workspaces: [],
          current_workspace_id: "ws_1",
        },
      },
    },
    {
      path: "/api/v1/me/workspaces",
      respond: {
        body: [
          {
            workspace_id: "ws_1",
            slug: "acme",
            name: "Acme",
            current_role: "manager",
            last_seen_at: null,
            settings_override: {},
          },
        ],
      },
    },
    {
      path: "/w/acme/api/v1/expenses",
      respond: {
        body: {
          data: [
            {
              id: "claim_1",
              workspace_id: "ws_1",
              work_engagement_id: "eng_1",
              vendor: "Corner Market",
              purchased_at: "2026-04-29T10:00:00Z",
              currency: "EUR",
              total_amount_cents: 1299,
              category: "supplies",
              property_id: null,
              note_md: "Cleaning supplies",
              state: "submitted",
              submitted_at: "2026-04-29T11:00:00Z",
              decided_by: null,
              decided_at: null,
              decision_note_md: null,
              pay_period_id: null,
              created_at: "2026-04-29T10:00:00Z",
              deleted_at: null,
              attachments: [],
            },
            {
              id: "claim_2",
              workspace_id: "ws_1",
              work_engagement_id: "eng_2",
              vendor: "Stationers",
              purchased_at: "2026-05-02T16:30:00Z",
              currency: "EUR",
              total_amount_cents: 899,
              category: "supplies",
              property_id: null,
              note_md: "Printer paper",
              state: "approved",
              submitted_at: "2026-05-02T17:00:00Z",
              decided_by: "usr_1",
              decided_at: "2026-05-02T18:00:00Z",
              decision_note_md: null,
              pay_period_id: null,
              created_at: "2026-05-02T16:30:00Z",
              deleted_at: null,
              attachments: [],
            },
          ],
          next_cursor: null,
          has_more: false,
        },
      },
    },
  ]);
}

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WorkspaceProvider>
          <ExpensesApprovalsPage />
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  });
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<ExpensesApprovalsPage>", () => {
  it("keeps field editing disabled and exports the visible expense date range", async () => {
    const fake = installFetch();
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    try {
      render(<Harness />);

      expect(await screen.findByText("Corner Market")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Edit fields" })).toBeDisabled();
      expect(screen.getByText("Manager field editing is not implemented yet.")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "More actions" }));
      fireEvent.click(screen.getByRole("menuitem", { name: /Export CSV/ }));
      expect(open).toHaveBeenCalledWith(
        "/w/acme/api/v1/payroll/exports/expense-ledger.csv?since=2026-04-29T00%3A00%3A00.000Z&until=2026-05-03T00%3A00%3A00.000Z",
        "_blank",
        "noopener,noreferrer",
      );
    } finally {
      fake.restore();
    }
  });
});
