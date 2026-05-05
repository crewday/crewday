import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { installFetchRouteHandlers } from "@/test/helpers";
import PayPage from "./PayPage";

type PeriodState = "open" | "locked" | "paid";

function payslipPayload(status = "draft") {
  return {
    id: "slip_1",
    pay_period_id: "period_1",
    user_id: "emp_1",
    currency: "EUR",
    shift_hours_decimal: "32",
    overtime_hours_decimal: "0",
    gross: { cents: 80000, currency: "EUR" },
    net: { cents: 70000, currency: "EUR" },
    status,
  };
}

function periodPayload(state: PeriodState, id = "period_1") {
  return {
    id,
    workspace_id: "ws_1",
    starts_at: "2026-04-01T00:00:00Z",
    ends_at: "2026-05-01T00:00:00Z",
    state,
    locked_at: state === "open" ? null : "2026-05-01T10:00:00Z",
    locked_by: state === "open" ? null : "usr_1",
    created_at: "2026-04-01T00:00:00Z",
  };
}

interface InstallFetchOptions {
  periods?: ReturnType<typeof periodPayload>[];
  lockStatus?: number;
  lockBody?: unknown;
  onLock?: () => void;
}

function installFetch(options: InstallFetchOptions = {}) {
  const periods = options.periods ?? [periodPayload("open")];
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
          data: [payslipPayload()],
        },
      },
    },
    {
      path: "/w/acme/api/v1/payroll/pay-periods",
      respond: () => ({ body: { data: periods } }),
    },
    {
      path: "/w/acme/api/v1/payroll/pay-periods/period_1/lock",
      method: "POST",
      respond: () => {
        options.onLock?.();
        return {
          status: options.lockStatus ?? 200,
          body: options.lockBody ?? periodPayload("locked"),
        };
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
  it("disables period close when no open period is identifiable and exports the current period CSV", async () => {
    const fake = installFetch({ periods: [periodPayload("locked")] });
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    try {
      render(<Harness />);

      expect(await screen.findByText("Maya Santos")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Close period" })).toBeDisabled();
      expect(screen.getByText("No open payroll period is available.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Preview PDF" })).toBeEnabled();

      fireEvent.click(screen.getByRole("button", { name: "More actions" }));
      fireEvent.click(screen.getByRole("menuitem", { name: /Export CSV/ }));
      expect(open).toHaveBeenCalledWith(
        "/w/acme/api/v1/payroll/exports/payslips.csv?period_id=period_1",
        "_blank",
        "noopener,noreferrer",
      );
    } finally {
      fake.restore();
    }
  });

  it("shows server blockers in the close period dialog", async () => {
    const fake = installFetch({
      lockStatus: 409,
      lockBody: {
        type: "https://crewday.dev/errors/conflict",
        title: "Conflict",
        detail: "Bookings still need approval before this period can close.",
      },
    });
    try {
      render(<Harness />);

      expect(await screen.findByText("Maya Santos")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Close period" }));
      const dialog = screen.getByRole("dialog", { name: "Close pay period" });
      fireEvent.click(within(dialog).getByRole("button", { name: "Close period" }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Bookings still need approval before this period can close.",
      );
      expect(fake.requests).toContainEqual(
        expect.objectContaining({
          path: "/w/acme/api/v1/payroll/pay-periods/period_1/lock",
          method: "POST",
        }),
      );
    } finally {
      fake.restore();
    }
  });

  it("locks the open period and refreshes pay data", async () => {
    const periods = [periodPayload("open")];
    const fake = installFetch({
      periods,
      onLock: () => {
        periods[0] = periodPayload("locked");
      },
    });
    try {
      render(<Harness />);

      expect(await screen.findByText("Maya Santos")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Close period" }));
      const dialog = screen.getByRole("dialog", { name: "Close pay period" });
      fireEvent.click(within(dialog).getByRole("button", { name: "Close period" }));

      expect(await screen.findByRole("status")).toHaveTextContent("Period locked. Pay data refreshed.");
      await waitFor(() => {
        expect(screen.getByRole("button", { name: "Close period" })).toBeDisabled();
      });
      expect(fake.requests).toContainEqual(
        expect.objectContaining({
          path: "/w/acme/api/v1/payroll/pay-periods/period_1/lock",
          method: "POST",
        }),
      );
      expect(
        fake.requests.filter((request) => request.path === "/w/acme/api/v1/payroll/pay-periods"),
      ).toHaveLength(2);
    } finally {
      fake.restore();
    }
  });

  it("opens the workspace-scoped payslip PDF URL with safe navigation flags", async () => {
    const fake = installFetch();
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    try {
      render(<Harness />);

      expect(await screen.findByText("Maya Santos")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Preview PDF" }));
      expect(open).toHaveBeenCalledWith(
        "/w/acme/api/v1/payroll/payslips/slip_1/pdf",
        "_blank",
        "noopener,noreferrer",
      );
    } finally {
      fake.restore();
    }
  });
});
