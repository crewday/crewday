import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentDoc } from "@/types/api";

import AdminAgentDocsPage from "./AgentDocsPage";

interface AgentDocDetail extends AgentDoc {
  notes?: string | null;
  approx_token_count?: number;
}

const detail: AgentDocDetail = {
  slug: "cli-cheatsheet",
  title: "CLI cheat-sheet",
  summary: "Crewday CLI verbs grouped by surface.",
  roles: ["manager", "employee"],
  capabilities: ["chat.manager", "chat.employee"],
  body_md: "# CLI\n\nUse the right command.",
  version: 1,
  is_customised: false,
  default_hash: "abc123",
  updated_at: "2026-04-18T09:00:00Z",
};

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? "OK" : "Error",
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function installFetch(): () => void {
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const parsed = new URL(resolved, "http://crewday.test");
    if (parsed.pathname === "/admin/api/v1/agent_docs") {
      return jsonResponse([{
        slug: detail.slug,
        title: detail.title,
        summary: detail.summary,
        roles: detail.roles,
        capabilities: detail.capabilities,
        version: detail.version,
        is_customised: detail.is_customised,
        default_hash: detail.default_hash,
        updated_at: detail.updated_at,
      }]);
    }
    if (parsed.pathname === "/admin/api/v1/agent_docs/cli-cheatsheet") {
      return jsonResponse(detail);
    }
    throw new Error(`Unscripted fetch: ${init?.method ?? "GET"} ${parsed.pathname}`);
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return () => {
    (globalThis as { fetch: typeof fetch }).fetch = original;
  };
}

function makeClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function Harness({ client }: { client: QueryClient }): ReactElement {
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/admin/agent-docs"]}>
        <AdminAgentDocsPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("AdminAgentDocsPage", () => {
  it("edits agent docs inline with role validation, local save, and cancel restore", async () => {
    const restore = installFetch();
    const client = makeClient();
    try {
      render(<Harness client={client} />);

      expect(await screen.findByRole("table", { name: "Agent docs editor" })).toBeInTheDocument();
      expect(screen.getByText("cli-cheatsheet")).toBeInTheDocument();
      expect(screen.getByText("Crewday CLI verbs grouped by surface.")).toBeInTheDocument();
      expect(screen.queryByText("capabilities:")).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Edit" }));
      const body = await screen.findByRole("textbox", { name: "Body for cli-cheatsheet" });
      expect(body).toHaveValue(detail.body_md);
      expect(screen.getByText("chat.manager")).toBeInTheDocument();
      expect(screen.queryByRole("textbox", { name: /Slug/i })).not.toBeInTheDocument();
      expect(screen.queryByRole("textbox", { name: /Title/i })).not.toBeInTheDocument();
      expect(screen.queryByRole("textbox", { name: /Summary/i })).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Manager" }));
      fireEvent.click(screen.getByRole("button", { name: "Employee" }));
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      expect(await screen.findByText("Pick at least one role before saving.")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Admin" }));
      fireEvent.change(body, { target: { value: "# Updated\n\nUse the saved command." } });
      fireEvent.change(screen.getByRole("textbox", { name: "Change note for cli-cheatsheet" }), {
        target: { value: " Operator note " },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(screen.queryByText("Pick at least one role before saving.")).not.toBeInTheDocument();
      });
      expect(screen.getByText("customised")).toBeInTheDocument();
      expect(screen.getByText("v2")).toBeInTheDocument();
      expect(screen.getByRole("textbox", { name: "Body for cli-cheatsheet" })).toHaveValue(
        "# Updated\n\nUse the saved command.",
      );

      fireEvent.change(screen.getByRole("textbox", { name: "Body for cli-cheatsheet" }), {
        target: { value: "temporary edit" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      expect(screen.queryByRole("textbox", { name: "Body for cli-cheatsheet" })).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Edit" }));
      expect(await screen.findByRole("textbox", { name: "Body for cli-cheatsheet" })).toHaveValue(
        "# Updated\n\nUse the saved command.",
      );
      expect(screen.getByRole("textbox", { name: "Change note for cli-cheatsheet" })).toHaveValue("Operator note");
      expect(within(screen.getByRole("group", { name: "Roles" })).getByRole("button", { name: "Admin" }))
        .toHaveAttribute("aria-pressed", "true");
    } finally {
      restore();
      client.clear();
    }
  });
});
