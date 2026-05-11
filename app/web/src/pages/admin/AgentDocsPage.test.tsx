import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import { installFetchRoutes } from "@/test/helpers";
import { renderWithProviders } from "@/test/render";
import type { AgentDoc, AgentDocSummary } from "@/types/api";
import AgentDocsPage from "./AgentDocsPage";

function renderPage(): void {
  renderWithProviders(<AgentDocsPage />, { router: "/admin/agent-docs" });
}

const summaries: AgentDocSummary[] = [
  {
    slug: "manager-playbook",
    title: "Manager playbook",
    summary: "Default guidance for manager-facing turns.",
    roles: ["manager", "admin"],
    updated_at: "2026-04-01T12:00:00Z",
  },
];

const detail: AgentDoc = {
  ...summaries[0]!,
  body_md: "# Manager playbook\n\nUse workspace context.",
  capabilities: ["kb.search", "tasks.read"],
  version: 3,
  is_customised: false,
  default_hash: "abc123",
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
});

describe("<AgentDocsPage>", () => {
  it("fetches the doc list first and fetches detail only after row selection", async () => {
    const fetcher = installFetchRoutes({
      "/admin/api/v1/agent_docs": [{ body: summaries }],
      "/admin/api/v1/agent_docs/manager-playbook": [{ body: detail }],
    });
    try {
      renderPage();

      expect(await screen.findByText("Manager playbook")).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "OpenAPI" })).not.toBeInTheDocument();
      expect(
        screen.queryByRole("searchbox", { name: "Search OpenAPI endpoints" }),
      ).not.toBeInTheDocument();
      expect(fetcher.calls.map((call) => new URL(call.url, "http://crewday.test").pathname))
        .toEqual(["/admin/api/v1/agent_docs"]);
      expect(screen.queryByText("# Manager playbook")).not.toBeInTheDocument();

      fireEvent.click(screen.getByText("manager-playbook"));

      await waitFor(() => {
        expect(fetcher.calls.map((call) => new URL(call.url, "http://crewday.test").pathname))
          .toEqual([
            "/admin/api/v1/agent_docs",
            "/admin/api/v1/agent_docs/manager-playbook",
          ]);
      });
      expect(await screen.findByText("# Manager playbook", { exact: false })).toBeInTheDocument();
      expect(
        fetcher.calls.map((call) => new URL(call.url, "http://crewday.test").pathname),
      ).not.toContain("/api/openapi.json");
    } finally {
      fetcher.restore();
    }
  });
});
