import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { fetchJson } from "@/lib/api";
import type { FetchOpts } from "@/lib/api";
import { NavHistoryProvider } from "@/context/NavHistoryContext";
import SettingsPage from "./SettingsPage";

vi.mock("@/lib/api", () => ({
  fetchJson: vi.fn(),
}));

vi.mock("@/components/AgentPreferencesPanel", () => ({
  default: ({
    scope,
    title,
    subtitle,
  }: {
    scope: string;
    title: string;
    subtitle: string;
  }) => (
    <section aria-label={title} data-scope={scope}>
      <h2>{title}</h2>
      <p>{subtitle}</p>
    </section>
  ),
}));

const fetchJsonMock = vi.mocked(fetchJson);

beforeEach(() => {
  fetchJsonMock.mockImplementation(async (path: string, opts?: FetchOpts) => {
    if (path === "/api/v1/settings" && opts?.method === "PATCH") {
      return workspaceSettings(opts.body as Record<string, unknown>);
    }
    if (path === "/api/v1/settings") return workspaceSettings();
    if (path === "/api/v1/settings/catalog") return settingsCatalog();
    if (path === "/api/v1/properties") return [];
    if (path === "/api/v1/employees") return [];
    if (path === "/api/v1/workspace/usage") return workspaceUsage();
    throw new Error("Unscripted fetch: " + path);
  });
});

afterEach(() => {
  cleanup();
  fetchJsonMock.mockReset();
});

function renderSettings(): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/w/acme/settings"]}>
        <NavHistoryProvider>
          <SettingsPage />
        </NavHistoryProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("SettingsPage", () => {
  it("renders workspace-owned settings without personal agent controls", async () => {
    render(renderSettings());

    expect(screen.getByRole("heading", { name: "Workspace settings" })).toBeInTheDocument();
    expect(screen.getByText(/Workspace-wide configuration only/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "My profile" })).toHaveAttribute("href", "/w/acme/me");

    const workspacePrefs = await screen.findByLabelText("Agent preferences — Workspace");
    expect(workspacePrefs).toBeInTheDocument();
    expect(workspacePrefs).toHaveAttribute("data-scope", "workspace");

    expect(screen.queryByRole("heading", { name: "Agent approval mode" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Agent preferences — You")).not.toBeInTheDocument();

    await waitFor(() => {
      expect(fetchJsonMock).toHaveBeenCalledWith("/api/v1/settings");
      expect(fetchJsonMock).not.toHaveBeenCalledWith("/api/v1/me/agent_approval_mode");
      expect(fetchJsonMock).not.toHaveBeenCalledWith("/api/v1/agent_preferences/me");
    });
  });

  it("renders visible advanced-setting help, readable control labels, and scope labels", async () => {
    render(renderSettings());

    expect(await screen.findByText("Whether tasks require photo or file evidence.")).toBeInTheDocument();
    expect(screen.getByText("How booking pay is computed by default.")).toBeInTheDocument();
    expect(screen.getByText("Can be overridden at: workspace, property, unit, work engagement, task")).toBeInTheDocument();
    expect(screen.getAllByText("Can be overridden at: workspace, work engagement").length).toBeGreaterThan(0);

    expect(within(screen.getByLabelText("Evidence policy")).getByRole("option", { name: "Required" })).toHaveValue("require");
    expect(within(screen.getByLabelText("Booking pay basis")).getByRole("option", { name: "Actual worked time" })).toHaveValue("actual");
    expect(within(screen.getByLabelText("Auto-assign tasks")).getByRole("option", { name: "Yes" })).toHaveValue("true");
    expect(within(screen.getByLabelText("Auto-assign tasks")).getByRole("option", { name: "No" })).toHaveValue("false");
  });

  it("keeps PATCH values as API enum and boolean values", async () => {
    render(renderSettings());

    const payBasis = await screen.findByLabelText("Booking pay basis");
    fireEvent.change(payBasis, { target: { value: "actual" } });
    const payForm = payBasis.closest("form");
    expect(payForm).not.toBeNull();
    fireEvent.click(within(payForm as HTMLFormElement).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(fetchJsonMock).toHaveBeenCalledWith("/api/v1/settings", {
        method: "PATCH",
        body: { "bookings.pay_basis": "actual" },
      });
    });

    const taskAutoAssign = screen.getByLabelText("Auto-assign tasks");
    fireEvent.change(taskAutoAssign, { target: { value: "false" } });
    const taskForm = taskAutoAssign.closest("form");
    expect(taskForm).not.toBeNull();
    fireEvent.click(within(taskForm as HTMLFormElement).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(fetchJsonMock).toHaveBeenCalledWith("/api/v1/settings", {
        method: "PATCH",
        body: { "tasks.auto_assign": false },
      });
    });
  });

  it("keeps invalid numbers from being submitted", async () => {
    render(renderSettings());

    const overrun = await screen.findByLabelText("Auto-approve overrun");
    fireEvent.change(overrun, { target: { value: "" } });
    const overrunForm = overrun.closest("form");
    expect(overrunForm).not.toBeNull();
    const save = within(overrunForm as HTMLFormElement).getByRole("button", { name: "Save" });

    expect(save).toBeDisabled();
    fireEvent.click(save);

    expect(fetchJsonMock).not.toHaveBeenCalledWith("/api/v1/settings", {
      method: "PATCH",
      body: { "bookings.auto_approve_overrun_minutes": expect.anything() },
    });
  });
});

function workspaceSettings(overrides: Record<string, unknown> = {}): unknown {
  return {
    meta: {
      name: "Acme",
      timezone: "Europe/Paris",
      currency: "EUR",
      country: "FR",
      default_locale: "fr-FR",
    },
    defaults: {
      "evidence.policy": "optional",
      "bookings.pay_basis": "scheduled",
      "bookings.auto_approve_overrun_minutes": 30,
      "bookings.cancellation_pay_to_worker": true,
      "tasks.auto_assign": true,
      ...overrides,
    },
    policy: {
      approvals: {
        always_gated: ["vendor_invoice.pay"],
        configurable: ["expense.create"],
      },
      danger_zone: ["Delete workspace"],
    },
  };
}

function settingsCatalog(): unknown {
  return [
    {
      key: "evidence.policy",
      label: "Evidence policy",
      description: "Whether tasks require photo or file evidence.",
      type: "enum",
      catalog_default: "optional",
      enum_values: ["require", "optional", "forbid"],
      override_scope: "W/P/U/WE/T",
    },
    {
      key: "bookings.pay_basis",
      label: "Booking pay basis",
      description: "How booking pay is computed by default.",
      type: "enum",
      catalog_default: "scheduled",
      enum_values: ["scheduled", "actual"],
      override_scope: "W/WE",
    },
    {
      key: "bookings.auto_approve_overrun_minutes",
      label: "Auto-approve overrun",
      description: "Minutes of overrun that can be approved automatically.",
      type: "int",
      catalog_default: 30,
      enum_values: null,
      override_scope: "W/WE",
    },
    {
      key: "bookings.cancellation_pay_to_worker",
      label: "Pay worker on cancellation",
      description: "Whether cancellation fees flow to the worker.",
      type: "bool",
      catalog_default: true,
      enum_values: null,
      override_scope: "W/WE",
    },
    {
      key: "tasks.auto_assign",
      label: "Auto-assign tasks",
      description: "Assign generated tasks automatically.",
      type: "bool",
      catalog_default: false,
      enum_values: null,
      override_scope: "workspace",
    },
  ];
}

function workspaceUsage(): unknown {
  return {
    percent: 25,
    paused: false,
    window_label: "May 2026",
  };
}
