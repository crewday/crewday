import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";
import type { ReactElement } from "react";
import { fetchApiDownload, fetchJson } from "@/lib/api";
import type { FetchOpts } from "@/lib/api";
import { NavHistoryProvider } from "@/context/NavHistoryContext";
import SettingsPage from "./SettingsPage";

vi.mock("@/lib/api", () => ({
  fetchApiDownload: vi.fn(),
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

const fetchApiDownloadMock = vi.mocked(fetchApiDownload);
const fetchJsonMock = vi.mocked(fetchJson);

beforeEach(() => {
  fetchApiDownloadMock.mockResolvedValue({
    blob: new Blob(["zip"], { type: "application/zip" }),
    filename: "acme-export.zip",
  });
  fetchJsonMock.mockImplementation(async (path: string, opts?: FetchOpts) => {
    if (path === "/api/v1/settings" && opts?.method === "PATCH") {
      return workspaceSettings(opts.body as Record<string, unknown>);
    }
    if (path === "/api/v1/settings") return workspaceSettings();
    if (path === "/api/v1/settings/catalog") return settingsCatalog();
    if (path === "/api/v1/properties") return [];
    if (path === "/api/v1/employees") return [];
    if (path === "/api/v1/workspace/usage") return workspaceUsage();
    if (path === "/api/v1/me/workspaces") return [];
    if (path === "/w/acme/api/v1/admin/workspace/archive" && opts?.method === "POST") {
      return { id: "ws_acme", archived_at: "2026-05-10T12:00:00.000Z" };
    }
    if (path === "/w/acme/api/v1/admin/workspace/delete" && opts?.method === "POST") {
      return {
        id: "ws_acme",
        archived_at: "2026-05-10T12:00:00.000Z",
        delete_requested_at: "2026-05-10T12:00:00.000Z",
        purge_after: "2026-05-24T12:00:00.000Z",
      };
    }
    throw new Error("Unscripted fetch: " + path);
  });
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:workspace-export");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  fetchApiDownloadMock.mockReset();
  fetchJsonMock.mockReset();
});

interface RenderedSettings {
  queryClient: QueryClient;
  view: ReactElement;
}

function LocationProbe(): ReactElement {
  const location = useLocation();
  return <span data-testid="location">{location.pathname + location.search + location.hash}</span>;
}

function renderSettings(): RenderedSettings {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    queryClient: qc,
    view: (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/w/acme/settings"]}>
        <NavHistoryProvider>
          <SettingsPage />
          <LocationProbe />
        </NavHistoryProvider>
      </MemoryRouter>
    </QueryClientProvider>
    ),
  };
}

describe("SettingsPage", () => {
  it("renders workspace-owned settings without personal agent controls", async () => {
    render(renderSettings().view);

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
    render(renderSettings().view);

    expect(await screen.findByText("Whether tasks require photo or file evidence.")).toBeInTheDocument();
    expect(screen.getByText("How booking pay is computed by default.")).toBeInTheDocument();
    expect(screen.getByText("Can be overridden at: workspace, property, unit, work engagement, task")).toBeInTheDocument();
    expect(screen.getAllByText("Can be overridden at: workspace, work engagement").length).toBeGreaterThan(0);

    const evidenceHelp = screen.getByText("Whether tasks require photo or file evidence.");
    expect(evidenceHelp.closest(".form-layout__help")).toBeInTheDocument();
    expect(evidenceHelp.closest(".settings-editor")).toHaveClass("form-layout__row");

    expect(within(screen.getByLabelText("Evidence policy")).getByRole("option", { name: "Required" })).toHaveValue("require");
    expect(within(screen.getByLabelText("Booking pay basis")).getByRole("option", { name: "Actual worked time" })).toHaveValue("actual");
    expect(within(screen.getByLabelText("Auto-assign tasks")).getByRole("option", { name: "Yes" })).toHaveValue("true");
    expect(within(screen.getByLabelText("Auto-assign tasks")).getByRole("option", { name: "No" })).toHaveValue("false");
  });

  it("keeps PATCH values as API enum and boolean values", async () => {
    render(renderSettings().view);

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
    render(renderSettings().view);

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

  it("renders only the current owner-facing danger zone actions", async () => {
    render(renderSettings().view);

    const dangerZone = (await screen.findByRole("heading", { name: "Danger zone" })).closest(".panel");
    expect(dangerZone).not.toBeNull();
    const zone = within(dangerZone as HTMLElement);

    expect(zone.getByRole("button", { name: "Export" })).toBeInTheDocument();
    expect(zone.getByRole("button", { name: "Archive" })).toBeInTheDocument();
    expect(zone.getByRole("button", { name: "Delete" })).toBeInTheDocument();
    expect(zone.queryByText(/Restore/i)).not.toBeInTheDocument();
    expect(zone.queryByText(/Root key/i)).not.toBeInTheDocument();
    expect(zone.queryByText(/Backup restore/i)).not.toBeInTheDocument();
    expect(zone.queryByText(/Hard-delete purge/i)).not.toBeInTheDocument();
  });

  it("downloads a workspace export without navigating away", async () => {
    render(renderSettings().view);

    fireEvent.click(await screen.findByRole("button", { name: "Export" }));

    expect(screen.getByRole("button", { name: "Exporting…" })).toBeDisabled();
    await waitFor(() => {
      expect(fetchApiDownloadMock).toHaveBeenCalledWith(
        "/w/acme/api/v1/admin/workspace/export",
        { method: "POST" },
      );
    });
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled();
    expect(screen.getByTestId("location")).toHaveTextContent("/w/acme/settings");
  });

  it("surfaces workspace export failures without starting a download", async () => {
    fetchApiDownloadMock.mockRejectedValueOnce(new Error("Export service unavailable"));
    render(renderSettings().view);

    fireEvent.click(await screen.findByRole("button", { name: "Export" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Export service unavailable");
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("archives the workspace after typed confirmation and refreshes workspace state", async () => {
    fetchJsonMock.mockImplementation(async (path: string, opts?: FetchOpts) => {
      if (path === "/api/v1/settings") return workspaceSettings();
      if (path === "/api/v1/settings/catalog") return settingsCatalog();
      if (path === "/api/v1/properties") return [];
      if (path === "/api/v1/employees") return [];
      if (path === "/api/v1/workspace/usage") return workspaceUsage();
      if (path === "/api/v1/me/workspaces") return [
        {
          workspace: {
            id: "beta",
            name: "Beta",
            timezone: "Europe/Paris",
            default_currency: "EUR",
            default_country: "FR",
            default_locale: "fr-FR",
          },
          grant_role: "worker",
          binding_org_id: null,
          source: "workspace_grant",
        },
      ];
      if (path === "/w/acme/api/v1/admin/workspace/archive" && opts?.method === "POST") {
        return { id: "ws_acme", archived_at: "2026-05-10T12:00:00.000Z" };
      }
      throw new Error("Unscripted fetch: " + path);
    });
    const rendered = renderSettings();
    const invalidate = vi.spyOn(rendered.queryClient, "invalidateQueries");
    render(rendered.view);

    fireEvent.click(await screen.findByRole("button", { name: "Archive" }));
    const dialog = screen.getByRole("dialog", { name: "Archive workspace" });
    expect(within(dialog).getByRole("button", { name: "Archive workspace" })).toBeDisabled();

    fireEvent.change(within(dialog).getByLabelText("Type ARCHIVE to confirm"), {
      target: { value: "ARCHIVE" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Archive workspace" }));

    await waitFor(() => {
      expect(fetchJsonMock).toHaveBeenCalledWith(
        "/w/acme/api/v1/admin/workspace/archive",
        { method: "POST" },
      );
    });
    await waitFor(() => {
      expect(fetchJsonMock).toHaveBeenCalledWith("/api/v1/me/workspaces");
    });
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/beta/today");
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["auth", "me"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["w", "_", "me", "workspaces"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["w", "_", "me"], refetchType: "none" });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["w", "_", "settings"], refetchType: "none" });
    expect(screen.queryByRole("dialog", { name: "Archive workspace" })).not.toBeInTheDocument();
  });

  it("schedules workspace deletion after deliberate confirmation and shows the purge deadline", async () => {
    const rendered = renderSettings();
    const invalidate = vi.spyOn(rendered.queryClient, "invalidateQueries");
    render(rendered.view);

    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog", { name: "Delete workspace" });
    expect(within(dialog).getByText(/14-day grace period/i)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Schedule deletion" })).toBeDisabled();

    fireEvent.change(within(dialog).getByLabelText("Type DELETE to confirm"), {
      target: { value: "DELETE" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Schedule deletion" }));

    await waitFor(() => {
      expect(fetchJsonMock).toHaveBeenCalledWith(
        "/w/acme/api/v1/admin/workspace/delete",
        { method: "POST" },
      );
    });
    expect(await screen.findByText(/Deletion scheduled/i)).toBeInTheDocument();
    expect(screen.getByText(/May 24, 2026/i)).toBeInTheDocument();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["auth", "me"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["w", "_", "me", "workspaces"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["w", "_", "me"], refetchType: "none" });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["w", "_", "settings"], refetchType: "none" });
    expect(screen.queryByRole("dialog", { name: "Delete workspace" })).not.toBeInTheDocument();
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
