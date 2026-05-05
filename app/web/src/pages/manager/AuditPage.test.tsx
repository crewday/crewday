import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import AuditPage from "./AuditPage";
import { jsonResponse } from "@/test/helpers";

function installFetch({ failAudit = false }: { failAudit?: boolean } = {}) {
  const calls: string[] = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request) => {
    // code-health: ignore[nloc] Audit route fetch fixture remains local and explicit for filter assertions.
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push(resolved);
    if (resolved === "/api/v1/auth/me") {
      return jsonResponse({
        user_id: "usr_1",
        display_name: "Mina",
        email: "mina@example.com",
        available_workspaces: [],
        current_workspace_id: "ws_1",
      });
    }
    if (resolved === "/api/v1/me/workspaces") {
      return jsonResponse([
        {
          workspace_id: "ws_1",
          slug: "acme",
          name: "Acme",
          current_role: "manager",
          last_seen_at: null,
          settings_override: {},
        },
      ]);
    }
    if (resolved.startsWith("/w/acme/api/v1/audit?")) {
      if (failAudit) {
        return jsonResponse({ type: "server_error", title: "Server error" }, 500);
      }
      const parsed = new URL(resolved, "http://crewday.test");
      if (parsed.searchParams.get("cursor") === "cursor_2") {
        return jsonResponse({
          data: [
            {
              at: "2026-04-29T11:59:00+00:00",
              actor_kind: "system",
              actor: "system:privacy-purge",
              action: "audit.second_page",
              target: "audit:second",
              via: "worker",
              reason: null,
              actor_grant_role: "manager",
              actor_was_owner_member: false,
              actor_action_key: null,
              actor_id: "system:privacy-purge",
              agent_label: null,
              entity_kind: "audit",
              entity_id: "second",
              correlation_id: "corr_2",
              diff: {},
            },
          ],
          next_cursor: null,
          has_more: false,
        });
      }
      const hasSecondPage = parsed.searchParams.get("action") === "multi";
      return jsonResponse({
        data: [
          {
            at: "2026-04-29T12:00:00+00:00",
            actor_kind: "user",
            actor: "usr_1",
            action: "asset.updated",
            target: "asset:asset_1",
            via: "web",
            reason: "because",
            actor_grant_role: "manager",
            actor_was_owner_member: true,
            actor_action_key: null,
            actor_id: "usr_1",
            agent_label: null,
            entity_kind: "asset",
            entity_id: "asset_1",
            correlation_id: "corr_1",
            diff: {},
          },
        ],
        next_cursor: hasSecondPage ? "cursor_2" : null,
        has_more: hasSecondPage,
      });
    }
    throw new Error(`Unexpected fetch call: ${resolved}`);
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function Harness({ initial = "/audit" }: { initial?: string }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <WorkspaceProvider>
          <AuditPage />
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function JumpToFilteredAudit() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate("/audit?actor=usr_1")}>
      Jump
    </button>
  );
}

function NavHarness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/audit?actor=old_actor"]}>
        <WorkspaceProvider>
          <JumpToFilteredAudit />
          <AuditPage />
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<AuditPage>", () => {
  it("passes URL filters through to the workspace audit API", async () => {
    const fake = installFetch();
    try {
      render(<Harness initial="/audit?actor=usr_1&action=asset.updated&entity=asset%3Aasset_1" />);

      expect(await screen.findByText("asset.updated")).toBeInTheDocument();
      expect(screen.getByText("asset:asset_1")).toBeInTheDocument();
      const auditCall = fake.calls.find((call) => call.startsWith("/w/acme/api/v1/audit?"));
      expect(auditCall).toContain("actor=usr_1");
      expect(auditCall).toContain("action=asset.updated");
      expect(auditCall).toContain("entity=asset%3Aasset_1");
      expect(auditCall).toContain("limit=50");
    } finally {
      fake.restore();
    }
  });

  it("renders the mock failure copy when the audit query fails", async () => {
    const fake = installFetch({ failAudit: true });
    try {
      render(<Harness initial="/audit?actor=usr_1" />);

      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
      expect(screen.queryByText("asset.updated")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("keeps filter fields in sync with URL navigation", async () => {
    const fake = installFetch();
    try {
      render(<NavHarness />);

      expect(await screen.findByLabelText("Actor")).toHaveValue("old_actor");
      fireEvent.click(screen.getByRole("button", { name: "Jump" }));

      await waitFor(() => expect(screen.getByLabelText("Actor")).toHaveValue("usr_1"));
      expect(fake.calls.some((call) => call.includes("actor=usr_1"))).toBe(true);
    } finally {
      fake.restore();
    }
  });

  it("loads the next server page with the returned cursor", async () => {
    const fake = installFetch();
    try {
      render(<Harness initial="/audit?action=multi" />);

      fireEvent.click(await screen.findByRole("button", { name: "Load more" }));

      expect(await screen.findByText("audit.second_page")).toBeInTheDocument();
      expect(fake.calls.some((call) => call.includes("cursor=cursor_2"))).toBe(true);
    } finally {
      fake.restore();
    }
  });
});
