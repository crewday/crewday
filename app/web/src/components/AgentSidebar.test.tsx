import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import AgentSidebar from "@/components/AgentSidebar";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import {
  __resetQueryKeyGetterForTests,
  registerQueryKeyWorkspaceGetter,
} from "@/lib/queryKeys";
import { makeTestQueryClient, renderWithProviders } from "@/test/render";
import { installFetchRouteHandlers } from "@/test/helpers";

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  registerWorkspaceSlugGetter(() => "crewday");
  registerQueryKeyWorkspaceGetter(() => "crewday");
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("AgentSidebar", () => {
  it("sends workspace agent messages through the shared API URL path", async () => {
    const sentAt = "2026-05-06T12:00:00Z";
    let messages: Array<{ at: string; kind: "user"; body: string }> = [];
    const env = installFetchRouteHandlers([
      {
        path: "/w/crewday/api/v1/agent/employee/log",
        respond: () => ({ body: messages }),
      },
      {
        path: "/w/crewday/api/v1/agent/employee/message",
        method: "POST",
        respond: () => {
          const message = { at: sentAt, kind: "user" as const, body: "bbash browser smoke" };
          messages = [message];
          return {
            status: 201,
            body: message,
          };
        },
      },
    ]);

    try {
      renderWithProviders(
        <MemoryRouter initialEntries={["/w/crewday/today"]}>
          <AgentSidebar role="employee" />
        </MemoryRouter>,
        { queryClient: makeTestQueryClient() },
      );

      const input = screen.getByLabelText("Message agent");
      fireEvent.change(input, { target: { value: "bbash browser smoke" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));

      await waitFor(() => {
        expect(
          env.requests.some(
            (request) =>
              request.method === "POST" &&
              request.path === "/w/crewday/api/v1/agent/employee/message",
          ),
        ).toBe(true);
      });

      const post = env.requests.find(
        (request) =>
          request.method === "POST" &&
          request.path === "/w/crewday/api/v1/agent/employee/message",
      );
      expect(post).toBeDefined();
      expect(post?.url).toBe("/w/crewday/api/v1/agent/employee/message");
      expect(post?.body).toEqual({ body: "bbash browser smoke" });
      expect(post?.headers["X-Agent-Page"]).toBe(
        "route=/w/crewday/today; surface=employee",
      );
      expect(await screen.findByText("bbash browser smoke")).toBeInTheDocument();
    } finally {
      env.restore();
    }
  });

  it("shows the user message optimistically while the send request is pending", async () => {
    let resolveSend: (value: Response) => void = () => {};
    const pendingSend = new Promise<Response>((resolve) => {
      resolveSend = resolve;
    });
    const env = installFetchRouteHandlers([
      {
        path: "/w/crewday/api/v1/agent/employee/log",
        respond: { body: [] },
      },
      {
        path: "/w/crewday/api/v1/agent/employee/message",
        method: "POST",
        respond: () => pendingSend,
      },
    ]);

    try {
      renderWithProviders(
        <MemoryRouter initialEntries={["/w/crewday/today"]}>
          <AgentSidebar role="employee" />
        </MemoryRouter>,
        { queryClient: makeTestQueryClient() },
      );

      const input = screen.getByLabelText("Message agent");
      fireEvent.change(input, { target: { value: "bbash browser smoke" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));

      expect(await screen.findByText("bbash browser smoke")).toBeInTheDocument();

      resolveSend(
        new Response(
          JSON.stringify({
            at: "2026-05-06T12:00:00Z",
            kind: "user",
            body: "bbash browser smoke",
          }),
          { status: 201 },
        ),
      );
      await waitFor(() => {
        expect(
          env.requests.filter(
            (request) =>
              request.method === "GET" &&
              request.path === "/w/crewday/api/v1/agent/employee/log",
          ).length,
        ).toBeGreaterThan(1);
      });
    } finally {
      env.restore();
    }
  });

  it("shows manager typing feedback while the send request is pending", async () => {
    let resolveSend: (value: Response) => void = () => {};
    const pendingSend = new Promise<Response>((resolve) => {
      resolveSend = resolve;
    });
    const env = installFetchRouteHandlers([
      {
        path: "/w/crewday/api/v1/agent/manager/log",
        respond: { body: [] },
      },
      {
        path: "/w/crewday/api/v1/approvals",
        respond: { body: [] },
      },
      {
        path: "/w/crewday/api/v1/agent/manager/message",
        method: "POST",
        respond: () => pendingSend,
      },
    ]);

    try {
      renderWithProviders(
        <MemoryRouter initialEntries={["/w/crewday/today"]}>
          <AgentSidebar role="manager" />
        </MemoryRouter>,
        { queryClient: makeTestQueryClient() },
      );

      const input = screen.getByLabelText("Message agent");
      fireEvent.change(input, { target: { value: "Can you summarize today's dashboard?" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));

      expect(
        await screen.findByText("Can you summarize today's dashboard?"),
      ).toBeInTheDocument();
      expect(screen.getByText("Agent is typing")).toBeInTheDocument();

      resolveSend(
        new Response(
          JSON.stringify({
            at: "2026-05-06T12:00:00Z",
            kind: "user",
            body: "Can you summarize today's dashboard?",
          }),
          { status: 201 },
        ),
      );
      await waitFor(() => {
        expect(screen.queryByText("Agent is typing")).not.toBeInTheDocument();
      });
    } finally {
      env.restore();
    }
  });

  it("lets the sidebar composer grow without an internal textarea scrollbar", async () => {
    const scrollHeight = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype,
      "scrollHeight",
    );
    Object.defineProperty(HTMLTextAreaElement.prototype, "scrollHeight", {
      configurable: true,
      value: 196,
    });
    const env = installFetchRouteHandlers([
      {
        path: "/w/crewday/api/v1/agent/manager/log",
        respond: { body: [] },
      },
      {
        path: "/w/crewday/api/v1/approvals",
        respond: { body: [] },
      },
    ]);

    try {
      renderWithProviders(
        <MemoryRouter initialEntries={["/w/crewday/dashboard"]}>
          <AgentSidebar role="manager" />
        </MemoryRouter>,
        { queryClient: makeTestQueryClient() },
      );

      const input = screen.getByLabelText<HTMLTextAreaElement>("Message agent");
      fireEvent.change(input, {
        target: {
          value:
            "Please draft a detailed plan for tomorrow's turnover, including supplies, priorities, and which open approvals need a manager decision before the morning shift starts.",
        },
      });

      expect(input.style.height).toBe("196px");
      expect(input.style.overflowY).toBe("hidden");
    } finally {
      env.restore();
      if (scrollHeight) {
        Object.defineProperty(HTMLTextAreaElement.prototype, "scrollHeight", scrollHeight);
      } else {
        delete (HTMLTextAreaElement.prototype as { scrollHeight?: number }).scrollHeight;
      }
    }
  });

  it("renders a manager runtime fallback from the refreshed log", async () => {
    const sentAt = "2026-05-06T12:00:00Z";
    const fallbackAt = "2026-05-06T12:00:01Z";
    let messages: Array<{ at: string; kind: "user" | "agent"; body: string }> = [];
    const env = installFetchRouteHandlers([
      {
        path: "/w/crewday/api/v1/agent/manager/log",
        respond: () => ({ body: messages }),
      },
      {
        path: "/w/crewday/api/v1/approvals",
        respond: { body: [] },
      },
      {
        path: "/w/crewday/api/v1/agent/manager/message",
        method: "POST",
        respond: () => {
          const userMessage = { at: sentAt, kind: "user" as const, body: "Hello" };
          messages = [
            userMessage,
            {
              at: fallbackAt,
              kind: "agent" as const,
              body: "The agent is not configured for this workspace yet. Ask an admin to assign a chat model, then try again.",
            },
          ];
          return {
            status: 201,
            body: userMessage,
          };
        },
      },
    ]);

    try {
      renderWithProviders(
        <MemoryRouter initialEntries={["/w/crewday/dashboard"]}>
          <AgentSidebar role="manager" />
        </MemoryRouter>,
        { queryClient: makeTestQueryClient() },
      );

      const input = screen.getByLabelText("Message agent");
      fireEvent.change(input, { target: { value: "Hello" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));

      expect(await screen.findByText("Hello")).toBeInTheDocument();
      expect(
        await screen.findByText(
          "The agent is not configured for this workspace yet. Ask an admin to assign a chat model, then try again.",
        ),
      ).toBeInTheDocument();
      expect(screen.queryByText("Agent is typing")).not.toBeInTheDocument();
    } finally {
      env.restore();
    }
  });

  it("rolls back the optimistic user message when send fails", async () => {
    let rejectSend: (value: Response) => void = () => {};
    const failedSend = new Promise<Response>((resolve) => {
      rejectSend = resolve;
    });
    const env = installFetchRouteHandlers([
      {
        path: "/w/crewday/api/v1/agent/employee/log",
        respond: {
          body: [{ at: "2026-05-06T11:00:00Z", kind: "agent", body: "prior message" }],
        },
      },
      {
        path: "/w/crewday/api/v1/agent/employee/message",
        method: "POST",
        respond: () => failedSend,
      },
    ]);

    try {
      renderWithProviders(
        <MemoryRouter initialEntries={["/w/crewday/today"]}>
          <AgentSidebar role="employee" />
        </MemoryRouter>,
        { queryClient: makeTestQueryClient() },
      );

      expect(await screen.findByText("prior message")).toBeInTheDocument();

      const input = screen.getByLabelText("Message agent");
      fireEvent.change(input, { target: { value: "bbash browser smoke" } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));

      expect(await screen.findByText("bbash browser smoke")).toBeInTheDocument();
      expect(screen.getByText("Agent is typing")).toBeInTheDocument();

      rejectSend(
        new Response(JSON.stringify({ detail: "send failed" }), { status: 500 }),
      );
      await waitFor(() => {
        expect(screen.queryByText("bbash browser smoke")).not.toBeInTheDocument();
      });
      expect(screen.queryByText("Agent is typing")).not.toBeInTheDocument();
      expect(screen.getByText("prior message")).toBeInTheDocument();
    } finally {
      env.restore();
    }
  });
});
