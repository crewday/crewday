import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests, qk } from "@/lib/queryKeys";
import { installFetchRouteHandlers, type FetchRouteRequest } from "@/test/helpers";
import { renderWithProviders } from "@/test/render";
import type { AgentDoc, AgentDocRevision, AgentDocSummary } from "@/types/api";
import AgentDocsPage from "./AgentDocsPage";

function renderPage() {
  return renderWithProviders(<AgentDocsPage />, { router: "/admin/agent-docs" });
}

const summaries: AgentDocSummary[] = [
  {
    slug: "manager-playbook",
    title: "Manager playbook",
    summary: "Default guidance for manager-facing turns.",
    roles: ["manager", "admin"],
    updated_at: "2026-04-01T12:00:00Z",
    version: 3,
    is_customised: false,
    default_hash: "abc123",
    metadata_default_hash: "meta123",
    approx_token_count: 9,
  },
];

const detail: AgentDoc = {
  ...summaries[0]!,
  body_md: "# Manager playbook\n\nUse workspace context.",
  capabilities: ["kb.search", "tasks.read"],
  notes: null,
};

const revisions: AgentDocRevision[] = [
  {
    version: 2,
    body_md: "# Old manager playbook",
    roles: ["manager"],
    notes: "Previous change",
    approx_token_count: 6,
    created_at: "2026-03-30T12:00:00Z",
    created_by_user_id: "usr_admin",
  },
];

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
});

describe("<AgentDocsPage>", () => {
  it("renders the inline editor and loads the row-attached body editor on edit", async () => {
    const fetcher = installFetchRouteHandlers([
      { path: "/admin/api/v1/agent_docs", respond: { body: summaries } },
      { path: "/admin/api/v1/agent_docs/manager-playbook", respond: { body: detail } },
      { path: "/admin/api/v1/agent_docs/manager-playbook/revisions", respond: { body: revisions } },
    ]);
    try {
      const { container } = renderPage();

      expect(await screen.findByRole("table", { name: "Agent docs editor" })).toBeInTheDocument();
      expect(container.querySelector("table")).toBeNull();
      expect(screen.queryByText("# Manager playbook")).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Edit" }));

      await waitFor(() => {
        expect(screen.getByLabelText("Body for manager-playbook")).toHaveValue(detail.body_md);
      });
      expect(screen.getByText(
        "Body is sent to every chat agent that loads this doc. Do not paste workspace secrets, customer data, or live API keys.",
      )).toBeInTheDocument();
      expect(screen.getByRole("region", { name: "Revision history for manager-playbook" })).toBeInTheDocument();
      expect(screen.getByText("Previous change")).toBeInTheDocument();
      expect(
        fetcher.calls.map((call) => new URL(call.url, "http://crewday.test").pathname),
      ).not.toContain("/api/openapi.json");
    } finally {
      fetcher.restore();
    }
  });

  it("does not crash when a live summary is missing approximate token metadata", async () => {
    const legacySummary = { ...summaries[0]! };
    delete legacySummary.approx_token_count;
    delete legacySummary.metadata_default_hash;
    const fetcher = installFetchRouteHandlers([
      { path: "/admin/api/v1/agent_docs", respond: { body: [legacySummary] } },
    ]);
    try {
      renderPage();

      expect(await screen.findByRole("table", { name: "Agent docs editor" })).toBeInTheDocument();
      expect(screen.getByText("Approx. tokens unavailable")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("saves body, roles, and change note with live approximate token count", async () => {
    const updatedBody = "# Manager playbook\n\nUse workspace context.\n\nAdd calmer escalation.";
    const updated: AgentDoc = {
      ...detail,
      body_md: updatedBody,
      roles: ["manager", "employee"],
      version: 4,
      is_customised: true,
      notes: "Clarify escalation",
      approx_token_count: Math.ceil(updatedBody.trim().length / 4),
      updated_at: "2026-04-02T12:00:00Z",
    };
    let currentDoc = detail;
    const fetcher = installFetchRouteHandlers([
      { path: "/admin/api/v1/agent_docs", respond: () => ({ body: [summaryFromDoc(currentDoc)] }) },
      { path: "/admin/api/v1/agent_docs/manager-playbook", respond: () => ({ body: currentDoc }) },
      { path: "/admin/api/v1/agent_docs/manager-playbook/revisions", respond: { body: [] } },
      {
        path: "/admin/api/v1/agent_docs/manager-playbook",
        method: "PUT",
        respond: () => {
          currentDoc = updated;
          return { body: updated };
        },
      },
    ]);
    try {
      renderPage();
      fireEvent.click(await screen.findByRole("button", { name: "Edit" }));

      await waitFor(() => {
        expect(screen.getByLabelText("Body for manager-playbook")).toHaveValue(detail.body_md);
      });
      const body = screen.getByLabelText("Body for manager-playbook");
      fireEvent.change(body, { target: { value: updatedBody } });
      fireEvent.click(within(screen.getByRole("group", { name: "Roles for manager-playbook" })).getByRole("button", {
        name: "Employee",
      }));
      fireEvent.click(within(screen.getByRole("group", { name: "Roles for manager-playbook" })).getByRole("button", {
        name: "Admin",
      }));
      fireEvent.change(screen.getByLabelText("Change note for manager-playbook"), {
        target: { value: "Clarify escalation" },
      });

      expect(screen.getAllByText(`Approx. ${updated.approx_token_count} tokens`).length).toBeGreaterThan(0);
      fireEvent.click(screen.getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(fetcher.requests.some((request) => request.method === "PUT")).toBe(true);
      });
      const put = fetcher.requests.find((request) => request.method === "PUT");
      expect(put?.body).toEqual({
        body_md: updatedBody,
        roles: ["manager", "employee"],
        notes: "Clarify escalation",
      });
      await waitFor(() => {
        expect(requestCount(fetcher.requests, "GET", "/admin/api/v1/agent_docs")).toBeGreaterThanOrEqual(2);
        expect(requestCount(fetcher.requests, "GET", "/admin/api/v1/agent_docs/manager-playbook")).toBeGreaterThanOrEqual(2);
        expect(requestCount(fetcher.requests, "GET", "/admin/api/v1/agent_docs/manager-playbook/revisions")).toBeGreaterThanOrEqual(2);
      });
      expect(await screen.findByText((_, element) => element?.textContent === "v4")).toBeInTheDocument();
      expect(screen.getByText("customised")).toBeInTheDocument();
      expect(screen.getByLabelText("Body for manager-playbook")).toHaveValue(updatedBody);
    } finally {
      fetcher.restore();
    }
  });

  it("preserves dirty role edits across list refetches before saving", async () => {
    const updatedBody = "# Manager playbook\n\nUse workspace context.\n\nKeep the draft roles.";
    const updated: AgentDoc = {
      ...detail,
      body_md: updatedBody,
      roles: ["manager", "employee"],
      version: 4,
      is_customised: true,
      approx_token_count: Math.ceil(updatedBody.trim().length / 4),
      updated_at: "2026-04-02T12:00:00Z",
    };
    let currentSummary = summaries[0]!;
    let currentDoc = detail;
    const fetcher = installFetchRouteHandlers([
      { path: "/admin/api/v1/agent_docs", respond: () => ({ body: [currentSummary] }) },
      { path: "/admin/api/v1/agent_docs/manager-playbook", respond: () => ({ body: currentDoc }) },
      { path: "/admin/api/v1/agent_docs/manager-playbook/revisions", respond: { body: [] } },
      {
        path: "/admin/api/v1/agent_docs/manager-playbook",
        method: "PUT",
        respond: () => {
          currentDoc = updated;
          currentSummary = summaryFromDoc(updated);
          return { body: updated };
        },
      },
    ]);
    try {
      const { queryClient } = renderPage();
      fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
      await waitFor(() => {
        expect(screen.getByLabelText("Body for manager-playbook")).toHaveValue(detail.body_md);
      });

      fireEvent.change(screen.getByLabelText("Body for manager-playbook"), { target: { value: updatedBody } });
      const roles = screen.getByRole("group", { name: "Roles for manager-playbook" });
      fireEvent.click(within(roles).getByRole("button", { name: "Employee" }));
      fireEvent.click(within(roles).getByRole("button", { name: "Admin" }));

      currentSummary = { ...currentSummary, roles: ["admin"], version: 4 };
      await queryClient.invalidateQueries({ queryKey: qk.adminAgentDocs() });
      await waitFor(() => {
        expect(requestCount(fetcher.requests, "GET", "/admin/api/v1/agent_docs")).toBeGreaterThanOrEqual(2);
      });

      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      await waitFor(() => {
        expect(fetcher.requests.some((request) => request.method === "PUT")).toBe(true);
      });
      const put = fetcher.requests.find((request) => request.method === "PUT");
      expect(put?.body).toMatchObject({
        body_md: updatedBody,
        roles: ["manager", "employee"],
      });
    } finally {
      fetcher.restore();
    }
  });

  it("validates blank body and zero roles before sending the mutation", async () => {
    const fetcher = installFetchRouteHandlers([
      { path: "/admin/api/v1/agent_docs", respond: { body: summaries } },
      { path: "/admin/api/v1/agent_docs/manager-playbook", respond: { body: detail } },
      { path: "/admin/api/v1/agent_docs/manager-playbook/revisions", respond: { body: [] } },
    ]);
    try {
      renderPage();
      fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
      fireEvent.change(await screen.findByLabelText("Body for manager-playbook"), { target: { value: "   " } });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));

      expect(await screen.findByText("Body is required before saving.")).toBeInTheDocument();
      expect(fetcher.requests.some((request) => request.method === "PUT")).toBe(false);

      fireEvent.change(screen.getByLabelText("Body for manager-playbook"), { target: { value: "Back in scope." } });
      const roles = screen.getByRole("group", { name: "Roles for manager-playbook" });
      fireEvent.click(within(roles).getByRole("button", { name: "Manager" }));
      fireEvent.click(within(roles).getByRole("button", { name: "Admin" }));
      fireEvent.click(screen.getByRole("button", { name: "Save" }));

      expect(await screen.findByText("Pick at least one role before saving.")).toBeInTheDocument();
      expect(fetcher.requests.some((request) => request.method === "PUT")).toBe(false);
    } finally {
      fetcher.restore();
    }
  });

  it("validates unknown roles before sending the mutation", async () => {
    const invalidDetail: AgentDoc = {
      ...detail,
      roles: ["manager", "legacy", "manager"],
    };
    const fetcher = installFetchRouteHandlers([
      { path: "/admin/api/v1/agent_docs", respond: { body: summaries } },
      { path: "/admin/api/v1/agent_docs/manager-playbook", respond: { body: invalidDetail } },
      { path: "/admin/api/v1/agent_docs/manager-playbook/revisions", respond: { body: [] } },
    ]);
    try {
      renderPage();
      fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
      await waitFor(() => {
        expect(screen.getByLabelText("Body for manager-playbook")).toHaveValue(detail.body_md);
      });
      fireEvent.change(await screen.findByLabelText("Body for manager-playbook"), {
        target: { value: "# Manager playbook\n\nEdited body." },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));

      expect(await screen.findByText("Roles must be manager, employee, or admin.")).toBeInTheDocument();
      expect(fetcher.requests.some((request) => request.method === "PUT")).toBe(false);
    } finally {
      fetcher.restore();
    }
  });

  it("validates duplicate roles before sending the mutation", async () => {
    const invalidDetail: AgentDoc = {
      ...detail,
      roles: ["manager", "manager"],
    };
    const fetcher = installFetchRouteHandlers([
      { path: "/admin/api/v1/agent_docs", respond: { body: summaries } },
      { path: "/admin/api/v1/agent_docs/manager-playbook", respond: { body: invalidDetail } },
      { path: "/admin/api/v1/agent_docs/manager-playbook/revisions", respond: { body: [] } },
    ]);
    try {
      renderPage();
      fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
      await waitFor(() => {
        expect(screen.getByLabelText("Body for manager-playbook")).toHaveValue(detail.body_md);
      });
      fireEvent.change(await screen.findByLabelText("Body for manager-playbook"), {
        target: { value: "# Manager playbook\n\nEdited body." },
      });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));

      expect(await screen.findByText("Roles must not contain duplicates.")).toBeInTheDocument();
      expect(fetcher.requests.some((request) => request.method === "PUT")).toBe(false);
    } finally {
      fetcher.restore();
    }
  });

  it("shows a row error on save failure without discarding the draft", async () => {
    const fetcher = installFetchRouteHandlers([
      { path: "/admin/api/v1/agent_docs", respond: { body: summaries } },
      { path: "/admin/api/v1/agent_docs/manager-playbook", respond: { body: detail } },
      { path: "/admin/api/v1/agent_docs/manager-playbook/revisions", respond: { body: [] } },
      {
        path: "/admin/api/v1/agent_docs/manager-playbook",
        method: "PUT",
        respond: { status: 500, body: { detail: "Backend refused the save." } },
      },
    ]);
    try {
      renderPage();
      fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
      const draft = "# Manager playbook\n\nDraft that should remain.";
      fireEvent.change(await screen.findByLabelText("Body for manager-playbook"), { target: { value: draft } });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));

      expect(await screen.findByText("Backend refused the save.")).toBeInTheDocument();
      expect(screen.getByLabelText("Body for manager-playbook")).toHaveValue(draft);
      expect(fetcher.requests.some((request) => request.method === "PUT")).toBe(true);
    } finally {
      fetcher.restore();
    }
  });

  it("resets to the backend default through the reset endpoint", async () => {
    const resetBody = "# Manager playbook\n\nDefault again.";
    const resetDoc: AgentDoc = {
      ...detail,
      body_md: resetBody,
      version: 4,
      is_customised: false,
      approx_token_count: Math.ceil(resetBody.trim().length / 4),
      updated_at: "2026-04-02T12:00:00Z",
    };
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let currentDoc = detail;
    const fetcher = installFetchRouteHandlers([
      { path: "/admin/api/v1/agent_docs", respond: () => ({ body: [summaryFromDoc(currentDoc)] }) },
      { path: "/admin/api/v1/agent_docs/manager-playbook", respond: () => ({ body: currentDoc }) },
      { path: "/admin/api/v1/agent_docs/manager-playbook/revisions", respond: { body: [] } },
      {
        path: "/admin/api/v1/agent_docs/manager-playbook/reset-to-default",
        method: "POST",
        respond: () => {
          currentDoc = resetDoc;
          return { body: resetDoc };
        },
      },
    ]);
    try {
      renderPage();
      fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
      await waitFor(() => {
        expect(screen.getByLabelText("Body for manager-playbook")).toHaveValue(detail.body_md);
      });
      fireEvent.change(await screen.findByLabelText("Change note for manager-playbook"), {
        target: { value: "Return to shipped doc" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Reset manager-playbook to code default" }));

      await waitFor(() => {
        expect(fetcher.requests.some((request) => request.method === "POST")).toBe(true);
      });
      const post = fetcher.requests.find((request) => request.method === "POST");
      expect(post?.body).toEqual({ notes: "Return to shipped doc" });
      await waitFor(() => {
        expect(requestCount(fetcher.requests, "GET", "/admin/api/v1/agent_docs")).toBeGreaterThanOrEqual(2);
        expect(requestCount(fetcher.requests, "GET", "/admin/api/v1/agent_docs/manager-playbook")).toBeGreaterThanOrEqual(2);
        expect(requestCount(fetcher.requests, "GET", "/admin/api/v1/agent_docs/manager-playbook/revisions")).toBeGreaterThanOrEqual(2);
      });
      expect(await screen.findByText("default")).toBeInTheDocument();
      expect(screen.getByLabelText("Body for manager-playbook")).toHaveValue(resetBody);
    } finally {
      fetcher.restore();
    }
  });

  it("shows revision load failures without blocking the editor", async () => {
    const fetcher = installFetchRouteHandlers([
      { path: "/admin/api/v1/agent_docs", respond: { body: summaries } },
      { path: "/admin/api/v1/agent_docs/manager-playbook", respond: { body: detail } },
      {
        path: "/admin/api/v1/agent_docs/manager-playbook/revisions",
        respond: { status: 500, body: { detail: "Could not load revision history." } },
      },
    ]);
    try {
      renderPage();
      fireEvent.click(await screen.findByRole("button", { name: "Edit" }));

      expect(await screen.findByText("Could not load revision history.")).toBeInTheDocument();
      expect(screen.getByLabelText("Body for manager-playbook")).toHaveValue(detail.body_md);
      expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    } finally {
      fetcher.restore();
    }
  });
});

function requestCount(
  requests: readonly FetchRouteRequest[],
  method: string,
  path: string,
): number {
  return requests.filter((request) => request.method === method && request.path === path).length;
}

function summaryFromDoc(doc: AgentDoc): AgentDocSummary {
  const {
    slug,
    title,
    summary,
    roles,
    updated_at,
    version,
    is_customised,
    default_hash,
    metadata_default_hash,
    approx_token_count,
  } = doc;
  return {
    slug,
    title,
    summary,
    roles,
    updated_at,
    version,
    is_customised,
    default_hash,
    metadata_default_hash,
    approx_token_count,
  };
}
