import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceProvider, useWorkspace } from "@/context/WorkspaceContext";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import PermissionsPage from "./PermissionsPage";
import { jsonResponse } from "@/test/helpers";

function WorkspaceJump() {
  const { setWorkspaceId } = useWorkspace();
  return (
    <button type="button" onClick={() => setWorkspaceId("beta")}>
      Use Beta
    </button>
  );
}

function renderPage(hash = "", includeWorkspaceJump = false) {
  window.history.replaceState(null, "", `/permissions${hash}`);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <WorkspaceProvider>
        {includeWorkspaceJump ? <WorkspaceJump /> : null}
        <MemoryRouter initialEntries={["/permissions"]}>
          <PermissionsPage />
        </MemoryRouter>
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  document.cookie = "crewday_workspace=acme; path=/";
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  registerWorkspaceSlugGetter(() => "acme");
});

afterEach(() => {
  cleanup();
  document.cookie = "crewday_workspace=; path=/; max-age=0";
  window.history.replaceState(null, "", "/permissions");
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("Permissions privacy tab", () => {
  it("renders empty upstream PII consent and writes checkbox toggles", async () => {
    const fetchSpy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const resolved = typeof url === "string" ? url : url.toString();
      const parsed = new URL(resolved, "http://crewday.test");
      const path = parsed.pathname;
      if (path === "/api/v1/me/workspaces") {
        return jsonResponse([{ workspace_id: "ws_1", slug: "acme", name: "Acme" }]);
      }
      if (path === "/w/acme/api/v1/users") {
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
      if (path === "/w/acme/api/v1/permission_groups") {
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
      if (path === "/w/acme/api/v1/agent_preferences/workspace/upstream_pii_consent") {
        if (init?.method === "PUT") {
          expect(JSON.parse(String(init.body))).toEqual({
            upstream_pii_consent: ["legal_name"],
          });
          return jsonResponse({
            upstream_pii_consent: ["legal_name"],
            available_tokens: ["legal_name", "email", "phone", "address"],
          });
        }
        return jsonResponse({
          upstream_pii_consent: [],
          available_tokens: ["legal_name", "email", "phone", "address"],
        });
      }
      throw new Error(`Unexpected fetch call: ${resolved}`);
    });
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

    renderPage();
    fireEvent.click(screen.getByRole("link", { name: "Privacy" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "No upstream PII consent selected",
    );
    fireEvent.click(screen.getByRole("checkbox", { name: /Legal names/ }));

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        "/w/acme/api/v1/agent_preferences/workspace/upstream_pii_consent",
        expect.objectContaining({ method: "PUT" }),
      );
    });
    expect(screen.getByRole("checkbox", { name: /Legal names/ })).toBeChecked();
  });

  it("uses stable hash links for permissions tabs", async () => {
    const fetchSpy = vi.fn(async (url: string | URL | Request) => {
      const resolved = typeof url === "string" ? url : url.toString();
      const parsed = new URL(resolved, "http://crewday.test");
      if (parsed.pathname === "/api/v1/me/workspaces") {
        return jsonResponse([{ workspace_id: "ws_1", slug: "acme", name: "Acme" }]);
      }
      if (parsed.pathname === "/w/acme/api/v1/users") {
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
      if (parsed.pathname === "/w/acme/api/v1/permission_groups") {
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
      if (parsed.pathname === "/w/acme/api/v1/permissions/action_catalog") {
        return jsonResponse({ entries: [], count: 0 });
      }
      if (parsed.pathname === "/w/acme/api/v1/permission_rules") {
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
      throw new Error(`Unexpected fetch call: ${resolved}`);
    });
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

    renderPage();

    expect(screen.getByRole("link", { name: "Groups" })).toHaveAttribute("href", "#groups");
    expect(screen.getByRole("link", { name: "Rules" })).toHaveAttribute("href", "#rules");
    expect(screen.getByRole("link", { name: "Privacy" })).toHaveAttribute("href", "#privacy");

    fireEvent.click(screen.getByRole("link", { name: "Rules" }));
    expect(window.location.hash).toBe("#rules");
    expect(await screen.findByRole("heading", { name: "Rule matrix" })).toBeInTheDocument();
  });

  it("deeplinks directly to the privacy tab", async () => {
    const fetchSpy = vi.fn(async (url: string | URL | Request) => {
      const resolved = typeof url === "string" ? url : url.toString();
      const parsed = new URL(resolved, "http://crewday.test");
      if (parsed.pathname === "/w/acme/api/v1/agent_preferences/workspace/upstream_pii_consent") {
        return jsonResponse({
          upstream_pii_consent: [],
          available_tokens: ["legal_name", "email", "phone", "address"],
        });
      }
      throw new Error(`Unexpected fetch call: ${resolved}`);
    });
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

    renderPage("#privacy");

    expect(await screen.findByRole("heading", { name: "Privacy" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Privacy" })).toHaveAttribute("aria-current", "page");
  });
});

describe("Permissions workspace scope", () => {
  it("derives the groups workspace scope from the active shell workspace", async () => {
    let groupScope = "";
    const fetchSpy = vi.fn(async (url: string | URL | Request) => {
      const resolved = typeof url === "string" ? url : url.toString();
      const parsed = new URL(resolved, "http://crewday.test");
      if (parsed.pathname === "/api/v1/me/workspaces") {
        return jsonResponse([
          { workspace_id: "ws_beta", slug: "beta", name: "Beta" },
          { workspace_id: "ws_acme", slug: "acme", name: "Acme" },
        ]);
      }
      if (parsed.pathname === "/w/acme/api/v1/users") {
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
      if (parsed.pathname === "/w/acme/api/v1/permission_groups") {
        groupScope = parsed.searchParams.get("scope_id") ?? "";
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
      throw new Error(`Unexpected fetch call: ${resolved}`);
    });
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

    renderPage();

    expect(await screen.findByText("No groups.")).toBeInTheDocument();
    expect(groupScope).toBe("ws_acme");
    expect(screen.queryByLabelText("Workspace")).not.toBeInTheDocument();
  });

  it("derives rules and resolver scope from the active shell workspace", async () => {
    const resolvedScopes: string[] = [];
    const fetchSpy = vi.fn(async (url: string | URL | Request) => {
      const resolved = typeof url === "string" ? url : url.toString();
      const parsed = new URL(resolved, "http://crewday.test");
      if (parsed.pathname === "/api/v1/me/workspaces") {
        return jsonResponse([
          { workspace_id: "ws_beta", slug: "beta", name: "Beta" },
          { workspace_id: "ws_acme", slug: "acme", name: "Acme" },
        ]);
      }
      if (parsed.pathname === "/w/acme/api/v1/users") {
        return jsonResponse({
          data: [{ id: "user_1", display_name: "Alice", email: "alice@example.com" }],
          next_cursor: null,
          has_more: false,
        });
      }
      if (parsed.pathname === "/w/acme/api/v1/permissions/action_catalog") {
        return jsonResponse({
          entries: [{
            key: "properties.create",
            valid_scope_kinds: ["workspace"],
            default_allow: ["owners"],
            root_only: false,
            root_protected_deny: false,
          }],
          count: 1,
        });
      }
      if (parsed.pathname === "/w/acme/api/v1/permission_rules") {
        expect(parsed.searchParams.get("scope_id")).toBe("ws_acme");
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
      if (parsed.pathname === "/w/acme/api/v1/permission_groups") {
        expect(parsed.searchParams.get("scope_id")).toBe("ws_acme");
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
      if (parsed.pathname === "/w/acme/api/v1/permissions/resolved") {
        resolvedScopes.push(parsed.searchParams.get("scope_id") ?? "");
        return jsonResponse({
          effect: "allow",
          source_layer: "default",
          source_rule_id: null,
          matched_groups: [],
        });
      }
      throw new Error(`Unexpected fetch call: ${resolved}`);
    });
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

    renderPage("#rules");

    expect(await screen.findByText("allow")).toBeInTheDocument();
    expect(resolvedScopes).toContain("ws_acme");
    expect(screen.queryByLabelText("Workspace")).not.toBeInTheDocument();
  });

  it("refreshes permission queries when the active shell workspace changes", async () => {
    const resolvedScopes: string[] = [];
    const fetchSpy = vi.fn(async (url: string | URL | Request) => {
      const resolved = typeof url === "string" ? url : url.toString();
      const parsed = new URL(resolved, "http://crewday.test");
      if (parsed.pathname === "/api/v1/me/workspaces") {
        return jsonResponse([
          { workspace_id: "ws_acme", slug: "acme", name: "Acme" },
          { workspace_id: "ws_beta", slug: "beta", name: "Beta" },
        ]);
      }
      if (parsed.pathname === "/w/acme/api/v1/users" || parsed.pathname === "/w/beta/api/v1/users") {
        return jsonResponse({
          data: [{ id: "user_1", display_name: "Alice", email: "alice@example.com" }],
          next_cursor: null,
          has_more: false,
        });
      }
      if (
        parsed.pathname === "/w/acme/api/v1/permissions/action_catalog" ||
        parsed.pathname === "/w/beta/api/v1/permissions/action_catalog"
      ) {
        return jsonResponse({
          entries: [{
            key: "properties.create",
            valid_scope_kinds: ["workspace"],
            default_allow: ["owners"],
            root_only: false,
            root_protected_deny: false,
          }],
          count: 1,
        });
      }
      if (parsed.pathname.endsWith("/api/v1/permission_rules")) {
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
      if (parsed.pathname.endsWith("/api/v1/permission_groups")) {
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
      if (parsed.pathname.endsWith("/api/v1/permissions/resolved")) {
        resolvedScopes.push(parsed.searchParams.get("scope_id") ?? "");
        return jsonResponse({
          effect: "allow",
          source_layer: "default",
          source_rule_id: null,
          matched_groups: [],
        });
      }
      throw new Error(`Unexpected fetch call: ${resolved}`);
    });
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

    renderPage("#rules", true);

    await waitFor(() => expect(resolvedScopes).toContain("ws_acme"));
    fireEvent.click(screen.getByRole("button", { name: "Use Beta" }));

    await waitFor(() => expect(resolvedScopes).toContain("ws_beta"));
    expect(fetchSpy).toHaveBeenCalledWith(
      "/w/beta/api/v1/permission_rules?scope_kind=workspace&scope_id=ws_beta",
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetchSpy).toHaveBeenCalledWith(
      "/w/beta/api/v1/permission_groups?scope_kind=workspace&scope_id=ws_beta",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("shows an empty active-workspace state without fetching permission data", async () => {
    document.cookie = "crewday_workspace=; path=/; max-age=0";
    const fetchSpy = vi.fn();
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

    renderPage();

    expect(await screen.findByText("No active workspace selected.")).toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
