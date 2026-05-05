import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { installFetchRouteHandlers } from "@/test/helpers";
import StaysPage from "./StaysPage";

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
    { path: "/w/acme/api/v1/stays/reservations?limit=500", respond: { body: { data: [] } } },
    { path: "/w/acme/api/v1/user_leaves?approved=true&limit=500", respond: { body: { data: [] } } },
    { path: "/w/acme/api/v1/properties", respond: { body: [] } },
    { path: "/w/acme/api/v1/employees", respond: { body: [] } },
  ]);
}

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WorkspaceProvider>
          <StaysPage />
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

describe("<StaysPage>", () => {
  it("marks missing stay management actions as unavailable", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByRole("button", { name: "Import iCal" })).toBeDisabled();
      expect(screen.getByText("iCal import setup is not implemented yet.")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "More actions" }));
      expect(screen.getByRole("menuitem", { name: /Add stay/ })).toBeDisabled();
      expect(screen.getByText("Manual stay creation is not implemented yet.")).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });
});
