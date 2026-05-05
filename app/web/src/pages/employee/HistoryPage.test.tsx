import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fetchJson } from "@/lib/api";
import HistoryPage from "./HistoryPage";

vi.mock("@/lib/api", () => ({
  fetchJson: vi.fn(),
}));

const fetchJsonMock = vi.mocked(fetchJson);

beforeEach(() => {
  fetchJsonMock.mockReset();
});

function renderHistory(initial = "/history?tab=tasks"): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <HistoryPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
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

  it("fetches and renders only the active tab stream", async () => {
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

    render(renderHistory("/history?tab=chats"));

    expect(await screen.findByText("Closed concierge thread")).toBeInTheDocument();
    expect(screen.getByText("Guest question resolved.")).toBeInTheDocument();
    expect(fetchJsonMock).not.toHaveBeenCalledWith("/api/v1/history?tab=tasks");
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });
});
