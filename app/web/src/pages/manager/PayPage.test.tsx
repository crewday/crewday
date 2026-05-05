import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { installFetchRouteHandlers } from "@/test/helpers";
import PayPage from "./PayPage";

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
      path: "/w/acme/api/v1/payroll/payslips",
      respond: {
        body: {
          data: [
            {
              id: "slip_1",
              pay_period_id: "period_1",
              user_id: "emp_1",
              currency: "EUR",
              shift_hours_decimal: "32",
              overtime_hours_decimal: "0",
              gross: { cents: 80000, currency: "EUR" },
              net: { cents: 70000, currency: "EUR" },
              status: "draft",
            },
          ],
        },
      },
    },
    {
      path: "/w/acme/api/v1/employees",
      respond: {
        body: [
          {
            id: "emp_1",
            name: "Maya Santos",
            roles: ["housekeeper"],
            properties: [],
            avatar_initials: "MS",
            avatar_file_id: null,
            avatar_url: null,
            phone: "+351 555 0100",
            email: "maya@example.com",
            started_on: "2026-01-01",
            capabilities: {},
            workspaces: ["ws_1"],
            villas: [],
            language: "en",
            weekly_availability: {},
            evidence_policy: "inherit",
            preferred_locale: null,
            settings_override: {},
          },
        ],
      },
    },
    {
      path: "/w/acme/api/v1/expenses/pending_reimbursement",
      respond: {
        body: {
          totals_by_currency: [],
          by_user: [],
          claims: [],
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
          <PayPage />
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

describe("<PayPage>", () => {
  it("marks missing payroll actions as unavailable", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Maya Santos")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Close period" })).toBeDisabled();
      expect(screen.getByText("Period close is not implemented yet.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Preview PDF" })).toBeDisabled();
      expect(screen.getByText("Payslip PDF preview is not implemented yet.")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "More actions" }));
      expect(screen.getByRole("menuitem", { name: /Export CSV/ })).toBeDisabled();
      expect(screen.getByText("Payroll export is not implemented yet.")).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });
});
