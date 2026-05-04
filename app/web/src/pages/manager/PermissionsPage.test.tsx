import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import PermissionsPage from "./PermissionsPage";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? "OK" : "Error",
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/permissions"]}>
        <PermissionsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  registerWorkspaceSlugGetter(() => "acme");
});

afterEach(() => {
  cleanup();
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
    fireEvent.click(screen.getByRole("button", { name: "Privacy" }));

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
});
