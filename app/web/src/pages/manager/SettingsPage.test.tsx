import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { fetchJson } from "@/lib/api";
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
  fetchJsonMock.mockImplementation(async (path: string) => {
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
      <MemoryRouter initialEntries={["/settings"]}>
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
    expect(screen.getByRole("link", { name: "My profile" })).toHaveAttribute("href", "/me");

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
});

function workspaceSettings(): unknown {
  return {
    meta: {
      name: "Acme",
      timezone: "Europe/Paris",
      currency: "EUR",
      country: "FR",
      default_locale: "fr-FR",
    },
    defaults: {
      "tasks.auto_assign": true,
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
