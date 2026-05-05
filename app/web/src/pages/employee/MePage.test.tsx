import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import type { ReactElement } from "react";
import { fetchJson } from "@/lib/api";
import HistoryPage from "./HistoryPage";
import MePage from "./MePage";

vi.mock("@/lib/api", () => ({
  fetchJson: vi.fn(),
}));

vi.mock("@/components/AppearancePanel", () => ({
  default: () => <section aria-label="Appearance">Appearance settings</section>,
}));

vi.mock("@/components/AgentApprovalModePanel", () => ({
  default: () => <section aria-label="Agent approval mode">Agent approval mode</section>,
}));

vi.mock("@/components/AgentPreferencesPanel", () => ({
  default: () => <section aria-label="My agent preferences">My agent preferences</section>,
}));

vi.mock("@/components/ChatChannelsMeCard", () => ({
  default: () => <section aria-label="Chat channels">Chat channels</section>,
}));

vi.mock("@/components/AvatarEditor", () => ({
  default: () => null,
}));

vi.mock("@/components/PersonalTokensPanel", () => ({
  default: () => <section aria-label="Personal access tokens">Personal access tokens</section>,
}));

const fetchJsonMock = vi.mocked(fetchJson);

beforeEach(() => {
  fetchJsonMock.mockImplementation(async (path: string) => {
    if (path === "/api/v1/me") return mePayload();
    if (path === "/api/v1/properties") return [];
    if (path === "/api/v1/history?tab=tasks") {
      return { data: [], next_cursor: null, has_more: false };
    }
    throw new Error("Unscripted fetch: " + path);
  });
});

afterEach(() => {
  cleanup();
  fetchJsonMock.mockReset();
});

function renderProfile(initial = "/me"): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route path="/me" element={<><MePage /><LocationProbe /><BackProbe /></>} />
          <Route path="/history" element={<><HistoryPage /><LocationProbe /><BackProbe /></>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function LocationProbe(): ReactElement {
  const loc = useLocation();
  return <span data-testid="location">{loc.pathname}</span>;
}

function BackProbe(): ReactElement {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(-1)}>
      Browser back
    </button>
  );
}

function mePayload(): unknown {
  return {
    role: "manager",
    theme: "system",
    agent_sidebar_collapsed: false,
    user_id: "usr_1",
    agent_approval_mode: "strict",
    current_workspace_id: "ws_1",
    available_workspaces: [],
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

describe("MePage", () => {
  it("navigates from the History profile card to History and browser back returns to Profile", async () => {
    render(renderProfile());

    expect(await screen.findByText("Mina Manager")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Change" })).toHaveLength(2);
    expect(screen.getByText("English")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: /Past tasks, chats, expenses, leaves/i }));

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/history");
    });
    expect(screen.getByRole("heading", { name: "History" })).toBeInTheDocument();
    expect(fetchJsonMock).toHaveBeenCalledWith("/api/v1/history?tab=tasks");

    fireEvent.click(screen.getByRole("button", { name: "Browser back" }));

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/me");
    });
    expect(screen.getAllByRole("button", { name: "Change" })).toHaveLength(2);
    expect(screen.getByText("English")).toBeInTheDocument();
  });
});
