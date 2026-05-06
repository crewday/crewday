import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import type { ReactElement } from "react";
import { fetchJson } from "@/lib/api";
import { __resetAuthStoreForTests } from "@/auth";
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
  __resetAuthStoreForTests();
  Object.defineProperty(navigator, "credentials", {
    value: undefined,
    configurable: true,
  });
});

function renderProfile(initial = "/me"): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route path="/me" element={<><MePage /><LocationProbe /><BackProbe /></>} />
          <Route path="/history" element={<><HistoryPage /><LocationProbe /><BackProbe /></>} />
          <Route path="/login" element={<><span>Login</span><LocationProbe /></>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function LocationProbe(): ReactElement {
  const loc = useLocation();
  const state = loc.state as { notice?: unknown } | null;
  return (
    <>
      <span data-testid="location">{loc.pathname}</span>
      {typeof state?.notice === "string" && (
        <span data-testid="location-notice">{state.notice}</span>
      )}
    </>
  );
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

  it("registers another passkey through start, credentials.create, and finish", async () => {
    const events: string[] = [];
    fetchJsonMock.mockImplementation(async (path: string, opts?: { body?: unknown }) => {
      if (path === "/api/v1/me") return mePayload();
      if (path === "/api/v1/auth/passkey/register/start") {
        events.push("start");
        return creationOptions();
      }
      if (path === "/api/v1/auth/passkey/register/finish") {
        events.push("finish");
        expect(opts?.body).toMatchObject({ challenge_id: "ch_1" });
        expect((opts?.body as { credential?: { rawId?: string } }).credential?.rawId).toBe("qrs");
        return {
          credential_id: "cred_new",
          transports: "internal",
          backup_eligible: true,
          aaguid: "00000000-0000-0000-0000-000000000000",
        };
      }
      throw new Error("Unscripted fetch: " + path);
    });
    const createSpy = vi.fn(async () => {
      events.push("create");
      return fakeCredential();
    });
    Object.defineProperty(navigator, "credentials", {
      value: { create: createSpy },
      configurable: true,
    });

    render(renderProfile());

    fireEvent.click(await screen.findByRole("button", { name: "+ Register another device" }));

    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/login"));
    expect(screen.getByTestId("location-notice")).toHaveTextContent(
      "Passkey registered. For your security, all sessions were signed out. Sign in again to continue.",
    );
    expect(events).toEqual(["start", "create", "finish"]);
    expect(createSpy).toHaveBeenCalledTimes(1);
    expect(fetchJsonMock).toHaveBeenCalledWith(
      "/api/v1/auth/passkey/register/start",
      { method: "POST", body: {} },
    );
    expect(fetchJsonMock).toHaveBeenCalledWith(
      "/api/v1/auth/passkey/register/finish",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("surfaces a cancelled passkey prompt and re-arms the register button", async () => {
    fetchJsonMock.mockImplementation(async (path: string) => {
      if (path === "/api/v1/me") return mePayload();
      if (path === "/api/v1/auth/passkey/register/start") return creationOptions();
      throw new Error("Unscripted fetch: " + path);
    });
    Object.defineProperty(navigator, "credentials", {
      value: {
        create: vi.fn(async () => {
          throw new DOMException("closed", "NotAllowedError");
        }),
      },
      configurable: true,
    });

    render(renderProfile());

    const button = await screen.findByRole("button", { name: "+ Register another device" });
    fireEvent.click(button);

    expect(await screen.findByRole("alert")).toHaveTextContent(/Passkey prompt closed/i);
    await waitFor(() => expect(button).not.toBeDisabled());
  });

  it("surfaces structured API passkey errors and re-arms the register button", async () => {
    fetchJsonMock.mockImplementation(async (path: string) => {
      if (path === "/api/v1/me") return mePayload();
      if (path === "/api/v1/auth/passkey/register/start") {
        throw {
          status: 422,
          problem: { error: "too_many_passkeys" },
        };
      }
      throw new Error("Unscripted fetch: " + path);
    });
    Object.defineProperty(navigator, "credentials", {
      value: { create: vi.fn() },
      configurable: true,
    });

    render(renderProfile());

    const button = await screen.findByRole("button", { name: "+ Register another device" });
    fireEvent.click(button);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You already have the maximum 5 passkeys. Remove one before registering another device.",
    );
    await waitFor(() => expect(button).not.toBeDisabled());
    expect(navigator.credentials?.create).not.toHaveBeenCalled();
  });
});

function creationOptions(): unknown {
  return {
    challenge_id: "ch_1",
    options: {
      challenge: "AQID",
      rp: { name: "crew.day" },
      user: { id: "AQID", name: "mina@example.test", displayName: "Mina Manager" },
      pubKeyCredParams: [{ type: "public-key", alg: -7 }],
    },
  };
}

function fakeCredential(): Credential {
  return {
    id: "cred_new",
    rawId: new Uint8Array([0xaa, 0xbb]).buffer,
    type: "public-key",
    response: {
      clientDataJSON: new Uint8Array([0x01]).buffer,
      attestationObject: new Uint8Array([0x02]).buffer,
      getTransports: () => ["internal"],
    },
    authenticatorAttachment: "platform",
    getClientExtensionResults: () => ({}),
  } as unknown as Credential;
}
