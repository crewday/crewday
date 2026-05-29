import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { type ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import { installFakeIndexedDb } from "@/test/fakeIndexedDb";
import { installFetchRouteHandlers } from "@/test/helpers";
import type { NotificationListResponse, NotificationPayload } from "@/types/api";
import NotificationsPage from "./NotificationsPage";

let restoreIndexedDb: (() => void) | null = null;

function notification(overrides: Partial<NotificationPayload> = {}): NotificationPayload {
  return {
    id: "notif_1",
    workspace_id: "ws_1",
    recipient_user_id: "usr_current",
    kind: "agent_message",
    subject: "crew.day - Storm watch",
    body_md: "Hi Mina,\n\nYou have a new message in crew.day:\n\n> Secure the terrace.\n\n[Open in app](/w/acme/notifications)",
    payload: {
      broadcast_id: "broadcast_1",
      broadcast_subject: "Storm watch",
      preview: "Storm watch",
      message_body: "Secure the terrace.",
      recipient_user_ids: ["usr_current", "usr_other"],
      recipient_emails: ["other@example.test"],
    },
    read_at: null,
    created_at: "2026-05-29T10:30:00Z",
    ...overrides,
  };
}

function list(data: NotificationPayload[]): NotificationListResponse {
  return {
    data,
    next_cursor: null,
    has_more: false,
    total_estimate: data.length,
  };
}

function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function Harness({
  queryClient = createTestQueryClient(),
}: { queryClient?: QueryClient } = {}): ReactElement {
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/w/acme/notifications"]}>
        <WorkspaceProvider>
          <NotificationsPage />
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  restoreIndexedDb = installFakeIndexedDb();
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  restoreIndexedDb?.();
  restoreIndexedDb = null;
  vi.restoreAllMocks();
});

describe("NotificationsPage", () => {
  it("lists current-user broadcast notifications without exposing recipient fanout", async () => {
    const env = installFetchRouteHandlers([
      {
        path: "/w/acme/api/v1/messaging/notifications?limit=100",
        respond: { body: list([notification()]) },
      },
      {
        method: "POST",
        path: "/w/acme/api/v1/messaging/notifications:mark-read",
        respond: { body: list([{ ...notification(), read_at: "2026-05-29T10:45:00Z" }]) },
      },
    ]);

    const queryClient = createTestQueryClient();
    render(<Harness queryClient={queryClient} />);

    const card = await screen.findByRole("article", { name: "crew.day - Storm watch" });
    expect(within(card).getByText("Unread")).toBeInTheDocument();
    expect(within(card).getByText("agent message")).toBeInTheDocument();
    expect(within(card).getByText("Secure the terrace.", { exact: false })).toBeInTheDocument();
    expect(card.querySelector('time[datetime="2026-05-29T10:30:00.000Z"]')).not.toBeNull();
    expect(within(card).getByRole("link", { name: "Open in app" })).toHaveAttribute(
      "href",
      "/w/acme/notifications",
    );
    expect(screen.queryByRole("button", { name: "Mark visible read" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mark read" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mark unread" })).not.toBeInTheDocument();
    expect(screen.queryByText("usr_other")).not.toBeInTheDocument();
    expect(screen.queryByText("other@example.test")).not.toBeInTheDocument();
    expect(env.requests[0]?.path).toBe("/w/acme/api/v1/messaging/notifications?limit=100");
  });

  it("auto-marks loaded unread notifications read without changing the visible list", async () => {
    const first = notification({ id: "notif_1", subject: "First broadcast" });
    const second = notification({ id: "notif_2", subject: "Second broadcast" });
    const alreadyRead = notification({
      id: "notif_3",
      subject: "Already read",
      read_at: "2026-05-29T10:40:00Z",
    });
    let current = [first, second, alreadyRead];
    const env = installFetchRouteHandlers([
      {
        path: "/w/acme/api/v1/messaging/notifications?limit=100",
        respond: () => ({ body: list(current) }),
      },
      {
        method: "POST",
        path: "/w/acme/api/v1/messaging/notifications:mark-read",
        respond: (request) => {
          const updated = [
            { ...first, read_at: "2026-05-29T10:45:00Z" },
            { ...second, read_at: "2026-05-29T10:45:00Z" },
          ];
          current = [...updated, alreadyRead];
          return { body: list(updated), status: request.body ? 200 : 400 };
        },
      },
    ]);

    const queryClient = createTestQueryClient();
    render(<Harness queryClient={queryClient} />);

    expect(await screen.findByRole("article", { name: "First broadcast" })).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Second broadcast" })).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Already read" })).toBeInTheDocument();
    expect(screen.getAllByText("Unread")).toHaveLength(2);
    expect(screen.getAllByText("Read")).toHaveLength(1);

    await waitFor(() => {
      expect(
        env.requests.filter(
          (request) =>
            request.method === "POST" &&
            request.path === "/w/acme/api/v1/messaging/notifications:mark-read",
        ),
      ).toHaveLength(1);
    });
    const post = env.requests.find(
      (request) =>
        request.method === "POST" &&
        request.path === "/w/acme/api/v1/messaging/notifications:mark-read",
    );
    expect(post?.body).toEqual({ ids: ["notif_1", "notif_2"] });

    expect(screen.getAllByText("Unread")).toHaveLength(2);
    expect(screen.getAllByText("Read")).toHaveLength(1);

    cleanup();
    render(<Harness queryClient={queryClient} />);

    await waitFor(() => {
      expect(screen.getAllByText("Read")).toHaveLength(3);
    });
    expect(screen.getAllByText("Read")).toHaveLength(3);
    expect(
      env.requests.filter(
        (request) =>
          request.method === "POST" &&
          request.path === "/w/acme/api/v1/messaging/notifications:mark-read",
      ),
    ).toHaveLength(1);
  });
});
