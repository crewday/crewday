import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { BrowserRouter, MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchJson } from "@/lib/api";
import HistoryPage from "./HistoryPage";

vi.mock("@/lib/api", () => ({
  fetchJson: vi.fn(),
}));

const fetchJsonMock = vi.mocked(fetchJson);

beforeEach(() => {
  fetchJsonMock.mockReset();
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

function renderHistory(initial = "/history#tasks"): ReactElement {
  // code-health: ignore[nloc] Lizard misattributes adjacent history fixtures to this compact routed render helper.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <HistoryPage />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function renderBrowserHistory(initial = "/history#tasks"): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  window.history.replaceState(null, "", initial);
  return (
    <QueryClientProvider client={qc}>
      <BrowserRouter>
        <HistoryPage />
      </BrowserRouter>
    </QueryClientProvider>
  );
}

function LocationProbe(): ReactElement {
  const location = useLocation();
  return <span data-testid="location">{location.pathname + location.search + location.hash}</span>;
}

function property(): unknown {
  return {
    id: "prop_1",
    name: "Villa Sud",
    city: "Nice",
    timezone: "Europe/Paris",
    color: "moss",
    kind: "str",
    areas: [],
    evidence_policy: "inherit",
    country: "FR",
    locale: "fr",
    settings_override: {},
    client_org_id: null,
    owner_user_id: null,
  };
}

function task(id: string, title: string): unknown {
  return {
    id,
    workspace_id: "ws_1",
    title,
    property_id: "prop_1",
    area_id: null,
    area: null,
    priority: "normal",
    state: "completed",
    status: "completed",
    scheduled_start: "2026-04-28T09:30:00Z",
    scheduled_end: "2026-04-28T10:00:00Z",
    scheduled_for_utc: "2026-04-28T09:30:00Z",
    duration_minutes: 30,
    photo_evidence: "disabled",
    linked_instruction_ids: [],
    inventory_consumption_json: {},
    assigned_user_id: "user_1",
    checklist: [],
  };
}

describe("HistoryPage", () => {
  it("loads the active tab as a cursor-paginated history stream", async () => {
    fetchJsonMock.mockImplementation(async (path: string) => {
      if (path === "/api/v1/properties") return [property()];
      if (path === "/api/v1/history?tab=tasks") {
        return {
          data: [task("task_2", "Newest done task")],
          next_cursor: "cursor_2",
          has_more: true,
        };
      }
      if (path === "/api/v1/history?tab=tasks&cursor=cursor_2") {
        return {
          data: [task("task_1", "Older done task")],
          next_cursor: null,
          has_more: false,
        };
      }
      throw new Error("Unscripted fetch: " + path);
    });

    render(renderHistory());

    expect(await screen.findByText("Newest done task")).toBeInTheDocument();
    expect(screen.getByText(/Villa Sud/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Load more" }));

    expect(await screen.findByText("Older done task")).toBeInTheDocument();
    await waitFor(() => {
      expect(fetchJsonMock).toHaveBeenCalledWith(
        "/api/v1/history?tab=tasks&cursor=cursor_2",
      );
    });
  });

  it("fetches and renders only the active hash tab stream", async () => {
    fetchJsonMock.mockImplementation(async (path: string) => {
      if (path === "/api/v1/properties") return [property()];
      if (path === "/api/v1/history?tab=chats") {
        return {
          data: [
            {
              id: "chat_1",
              title: "Closed concierge thread",
              last_at: "2026-04-28T10:00:00Z",
              summary: "Guest question resolved.",
            },
          ],
          next_cursor: null,
          has_more: false,
        };
      }
      throw new Error("Unscripted fetch: " + path);
    });

    render(renderHistory("/history#chats"));

    expect(await screen.findByText("Closed concierge thread")).toBeInTheDocument();
    expect(screen.getByText("Guest question resolved.")).toBeInTheDocument();
    expect(fetchJsonMock).not.toHaveBeenCalledWith("/api/v1/history?tab=tasks");
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Chats" })).toHaveAttribute("aria-selected", "true");
  });

  it("normalizes old query tab links to hash deeplinks", async () => {
    fetchJsonMock.mockImplementation(async (path: string) => {
      if (path === "/api/v1/properties") return [property()];
      if (path === "/api/v1/history?tab=chats") {
        return {
          data: [
            {
              id: "chat_1",
              title: "Archived chat",
              last_at: "2026-04-28T10:00:00Z",
              summary: "Resolved.",
            },
          ],
          next_cursor: null,
          has_more: false,
        };
      }
      throw new Error("Unscripted fetch: " + path);
    });

    render(renderHistory("/history?tab=chats&from=legacy"));

    expect(await screen.findByText("Archived chat")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/history?from=legacy#chats");
    });
    expect(fetchJsonMock).toHaveBeenCalledWith("/api/v1/history?tab=chats");
  });

  it("clicks update the hash and browser Back/Forward follows the selected panel", async () => {
    fetchJsonMock.mockImplementation(async (path: string) => {
      if (path === "/api/v1/properties") return [property()];
      if (path === "/api/v1/history?tab=tasks") {
        return { data: [task("task_1", "Completed task")], next_cursor: null, has_more: false };
      }
      if (path === "/api/v1/history?tab=chats") {
        return {
          data: [
            {
              id: "chat_1",
              title: "Closed chat",
              last_at: "2026-04-28T10:00:00Z",
              summary: "Resolved.",
            },
          ],
          next_cursor: null,
          has_more: false,
        };
      }
      if (path === "/api/v1/history?tab=expenses") {
        return {
          data: [
            {
              id: "expense_1",
              vendor: "Stationery Shop",
              total_amount_cents: 1299,
              currency: "USD",
              submitted_at: "2026-04-28",
              purchased_at: null,
              note_md: "Pens",
              state: "reimbursed",
            },
          ],
          next_cursor: null,
          has_more: false,
        };
      }
      throw new Error("Unscripted fetch: " + path);
    });

    render(renderBrowserHistory());

    expect(await screen.findByText("Completed task")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Chats" }));
    await waitFor(() => {
      expect(window.location.hash).toBe("#chats");
    });
    expect(await screen.findByText("Closed chat")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Expenses" }));
    await waitFor(() => {
      expect(window.location.hash).toBe("#expenses");
    });
    expect(await screen.findByText("Stationery Shop · $12.99")).toBeInTheDocument();

    window.history.back();
    await waitFor(() => {
      expect(window.location.hash).toBe("#chats");
      expect(screen.getByRole("tab", { name: "Chats" })).toHaveAttribute("aria-selected", "true");
    });
    expect(screen.getByText("Closed chat")).toBeInTheDocument();

    window.history.forward();
    await waitFor(() => {
      expect(window.location.hash).toBe("#expenses");
      expect(screen.getByRole("tab", { name: "Expenses" })).toHaveAttribute("aria-selected", "true");
    });
    expect(screen.getByText("Stationery Shop · $12.99")).toBeInTheDocument();
  });
});
