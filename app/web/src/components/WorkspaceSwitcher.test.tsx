import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import WorkspaceSwitcher from "./WorkspaceSwitcher";
import { fetchJson } from "@/lib/api";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchJson: vi.fn(),
  };
});

const fetchJsonMock = vi.mocked(fetchJson);

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
  fetchJsonMock.mockResolvedValue(mePayload());
});

afterEach(() => {
  cleanup();
  fetchJsonMock.mockReset();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

function renderSwitcher(): QueryClient {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <WorkspaceProvider>
        <WorkspaceSwitcher />
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
  return qc;
}

describe("WorkspaceSwitcher", () => {
  it("keeps the desktop sidebar switcher and invalidates data on selection", async () => {
    const persistWorkspace = vi.spyOn(preferences, "persistWorkspace");
    const qc = renderSwitcher();
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    fireEvent.click(await screen.findByRole("button", { name: /Acme/i }));
    fireEvent.click(screen.getByRole("button", { name: /Beta/i }));

    await waitFor(() => {
      expect(persistWorkspace).toHaveBeenCalledWith("beta");
    });
    expect(invalidateSpy).toHaveBeenCalled();
  });

  it("renders an inert context chip for single-workspace users", async () => {
    fetchJsonMock.mockResolvedValue({
      ...mePayload(),
      available_workspaces: [mePayload().available_workspaces[0]],
    });

    renderSwitcher();

    const trigger = await screen.findByRole("button", { name: /Acme/i });
    expect(trigger).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(trigger);
    expect(screen.queryByRole("listbox", { name: "Switch workspace" })).toBeNull();
  });
});

function mePayload() {
  return {
    role: "manager",
    theme: "system",
    agent_sidebar_collapsed: false,
    user_id: "usr_1",
    agent_approval_mode: "strict",
    current_workspace_id: "acme",
    available_workspaces: [
      {
        workspace: {
          id: "acme",
          name: "Acme",
          timezone: "UTC",
          default_currency: "USD",
          default_country: "US",
          default_locale: "en",
        },
        grant_role: "manager",
        binding_org_id: null,
        source: "workspace_grant",
      },
      {
        workspace: {
          id: "beta",
          name: "Beta",
          timezone: "UTC",
          default_currency: "USD",
          default_country: "US",
          default_locale: "en",
        },
        grant_role: "worker",
        binding_org_id: null,
        source: "workspace_grant",
      },
    ],
    client_binding_org_ids: [],
    is_deployment_admin: false,
    is_deployment_owner: false,
    manager_name: "Mina Manager",
    today: "2026-05-05",
    now: "2026-05-05T10:00:00Z",
    employee: {
      id: "emp_1",
      user_id: "usr_1",
      first_name: "Mina",
      last_name: "Manager",
      name: "Mina Manager",
      email: "mina@example.test",
      phone: "+15550101010",
      avatar_url: null,
      avatar_initials: "MM",
      roles: ["manager"],
      started_on: "2025-03-12",
      language: "en",
    },
  };
}
