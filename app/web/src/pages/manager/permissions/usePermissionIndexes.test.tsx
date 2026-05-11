import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { renderHook } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceProvider, useWorkspace } from "@/context/WorkspaceContext";
import {
  __resetApiProvidersForTests,
  ApiError,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import GroupsTab from "./GroupsTab";
import WhoCanDoThis from "./WhoCanDoThis";
import { useActiveWorkspaceScope, useUsersIndex } from "./lib/usePermissionIndexes";
import { jsonResponse } from "@/test/helpers";

function queryWrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <WorkspaceProvider>{children}</WorkspaceProvider>
    </QueryClientProvider>
  );
}

function WorkspaceJump() {
  const { setWorkspaceId } = useWorkspace();
  return (
    <button type="button" onClick={() => setWorkspaceId("beta")}>
      Use Beta
    </button>
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
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("useActiveWorkspaceScope", () => {
  it("maps the active workspace slug to the API workspace id", async () => {
    const fetchSpy = vi.fn(async () =>
      jsonResponse([
        { workspace_id: "ws_beta", slug: "beta", name: "Beta" },
        { workspace_id: "ws_acme", slug: "acme", name: "Acme" },
      ]),
    );
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    const { result } = renderHook(() => useActiveWorkspaceScope(), {
      wrapper: queryWrapper(qc),
    });

    await waitFor(() => expect(result.current.workspaceId).toBe("ws_acme"));
    expect(result.current.workspaceName).toBe("Acme");
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/v1/me/workspaces",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("does not fetch workspace metadata when no shell workspace is active", async () => {
    document.cookie = "crewday_workspace=; path=/; max-age=0";
    const fetchSpy = vi.fn();
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    const { result } = renderHook(() => useActiveWorkspaceScope(), {
      wrapper: queryWrapper(qc),
    });

    expect(result.current.workspaceId).toBe("");
    expect(result.current.isPending).toBe(false);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("useUsersIndex", () => {
  it("walks paginated users envelopes into an id index", async () => {
    const fetchSpy = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          data: [
            { id: "user_1", display_name: "Alice", email: "alice@example.com" },
          ],
          next_cursor: "cursor-2",
          has_more: true,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          data: [
            { id: "user_2", display_name: "Bea", email: "bea@example.com" },
          ],
          next_cursor: null,
          has_more: false,
        }),
      );
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    const { result } = renderHook(() => useUsersIndex(), {
      wrapper: queryWrapper(qc),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({
      user_1: {
        id: "user_1",
        display_name: "Alice",
        email: "alice@example.com",
      },
      user_2: {
        id: "user_2",
        display_name: "Bea",
        email: "bea@example.com",
      },
    });
    expect(fetchSpy).toHaveBeenCalledWith(
      "/w/acme/api/v1/users?limit=500",
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetchSpy).toHaveBeenCalledWith(
      "/w/acme/api/v1/users?limit=500&cursor=cursor-2",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("surfaces 404 errors instead of degrading to an empty index", async () => {
    const fetchSpy = vi.fn(async () =>
      jsonResponse(
        {
          type: "https://crewday.dev/errors/not_found",
          title: "Not found",
          status: 404,
        },
        404,
      ),
    );
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    const { result } = renderHook(() => useUsersIndex(), {
      wrapper: queryWrapper(qc),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
    expect(result.current.error).toBeInstanceOf(ApiError);
    expect((result.current.error as ApiError).status).toBe(404);
  });
});

describe("permissions user display", () => {
  it("renders resolver choices with display names from the users index", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const fetchSpy = vi.fn(async () =>
      jsonResponse({
        effect: "allow",
        source_layer: "default",
        source_rule_id: null,
        matched_groups: [],
      }),
    );
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

    render(
      <QueryClientProvider client={qc}>
        <WhoCanDoThis
          users={[{ id: "user_1", display_name: "Alice", email: "alice@example.com" }]}
          actions={[{
            key: "employees.read",
            valid_scope_kinds: ["workspace"],
            default_allow: ["owners"],
            root_only: false,
            root_protected_deny: false,
          }]}
          scopeKind="workspace"
          scopeId="ws_1"
        />
      </QueryClientProvider>,
    );

    expect(screen.getByRole("combobox", { name: /^User/ })).toHaveValue("Alice");
    expect(screen.queryByText("user_1")).not.toBeInTheDocument();
  });

  it("renders group member display names and email addresses from GET /users", async () => {
    const originalFetch = globalThis.fetch;
    const fetchSpy = vi.fn(async (url: string | URL | Request) => {
      const resolved = typeof url === "string" ? url : url.toString();
      const path = new URL(resolved, "http://crewday.test").pathname;
      const search = new URL(resolved, "http://crewday.test").search;
      if (path === "/api/v1/me/workspaces") {
        return jsonResponse([
          { workspace_id: "ws_1", slug: "acme", name: "Acme" },
        ]);
      }
      if (path === "/w/acme/api/v1/users") {
        return jsonResponse({
          data: [
            { id: "user_1", display_name: "Alice", email: "alice@example.com" },
          ],
          next_cursor: null,
          has_more: false,
        });
      }
      if (path === "/w/acme/api/v1/permission_groups") {
        expect(search).toBe("?scope_kind=workspace&scope_id=ws_1");
        return jsonResponse({
          data: [{
            id: "group_1",
            slug: "managers",
            name: "Managers",
            system: true,
            group_kind: "derived",
            capabilities: {},
            created_at: "2026-05-04T12:00:00Z",
          }],
          next_cursor: null,
          has_more: false,
        });
      }
      if (path === "/w/acme/api/v1/permission_groups/group_1/members") {
        return jsonResponse({
          data: [{
            group_id: "group_1",
            user_id: "user_1",
            added_at: "2026-05-04T12:00:00Z",
            added_by_user_id: null,
          }],
          next_cursor: null,
          has_more: false,
        });
      }
      throw new Error(`Unexpected fetch call: ${resolved}`);
    });
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

    try {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(
        <QueryClientProvider client={qc}>
          <WorkspaceProvider>
            <GroupsTab />
          </WorkspaceProvider>
        </QueryClientProvider>,
      );

      expect(await screen.findByText("Alice")).toBeInTheDocument();
      expect(screen.getByText("alice@example.com")).toBeInTheDocument();
      expect(screen.getByText(/Auto-populated from role_grants/)).toBeInTheDocument();
    } finally {
      (globalThis as { fetch: typeof fetch }).fetch = originalFetch;
    }
  });

  it("does not request previous-workspace group members after workspace changes", async () => {
    const staleMemberUrls: string[] = [];
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
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
      if (parsed.pathname === "/w/acme/api/v1/permission_groups") {
        return jsonResponse({
          data: [{
            id: "group_acme",
            slug: "owners",
            name: "Owners",
            system: true,
            group_kind: "system",
            capabilities: {},
            created_at: "2026-05-04T12:00:00Z",
          }],
          next_cursor: null,
          has_more: false,
        });
      }
      if (parsed.pathname === "/w/acme/api/v1/permission_groups/group_acme/members") {
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
      if (parsed.pathname === "/w/beta/api/v1/permission_groups") {
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
      if (parsed.pathname.includes("/group_acme/members")) {
        staleMemberUrls.push(parsed.pathname);
        return jsonResponse({ data: [], next_cursor: null, has_more: false });
      }
      throw new Error(`Unexpected fetch call: ${resolved}`);
    });
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={qc}>
        <WorkspaceProvider>
          <WorkspaceJump />
          <GroupsTab />
        </WorkspaceProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findAllByText("Owners")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Use Beta" }));

    expect(await screen.findByText("No groups.")).toBeInTheDocument();
    expect(staleMemberUrls).toEqual([]);
  });
});
