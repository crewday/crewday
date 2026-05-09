import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import PermissionsPage from "./PermissionsPage";

interface FetchCall {
  url: string;
  init: RequestInit;
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? "OK" : "Error",
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function renderPage(hash = "") {
  window.history.replaceState(null, "", `/permissions${hash}`);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/permissions"]}>
        <PermissionsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function installFetch(): FetchCall[] {
  const calls: FetchCall[] = [];
  let upstreamPiiConsent = [] as string[];
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const initRecord = init ?? {};
    const parsed = new URL(resolved, "http://crewday.test");
    calls.push({ url: resolved, init: initRecord });

    if (parsed.pathname === "/api/v1/me/workspaces") {
      return jsonResponse([{ workspace_id: "ws-bernard", slug: "ws-bernard", name: "Bernard workspace" }]);
    }
    if (parsed.pathname === "/api/v1/users") {
      return jsonResponse([]);
    }
    if (parsed.pathname === "/api/v1/permission_groups") {
      return jsonResponse({ data: [], next_cursor: null, has_more: false });
    }
    if (parsed.pathname === "/api/v1/permissions/action_catalog") {
      return jsonResponse({ entries: [], count: 0 });
    }
    if (parsed.pathname === "/api/v1/permission_rules") {
      return jsonResponse({ data: [], next_cursor: null, has_more: false });
    }
    if (parsed.pathname === "/api/v1/agent_preferences/workspace/upstream_pii_consent") {
      if (initRecord.method === "PUT") {
        const body = JSON.parse(String(initRecord.body)) as { upstream_pii_consent: string[] };
        upstreamPiiConsent = body.upstream_pii_consent;
      }
      return jsonResponse({
        upstream_pii_consent: upstreamPiiConsent,
        available_tokens: ["legal_name", "email", "phone", "address"],
      });
    }
    throw new Error(`Unscripted fetch: ${initRecord.method ?? "GET"} ${parsed.pathname}`);
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return calls;
}

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/permissions");
  vi.restoreAllMocks();
});

describe("PermissionsPage PageTabs", () => {
  it("renders Groups, Rules, and Privacy tabs with hash-backed navigation", async () => {
    installFetch();
    renderPage();

    expect(screen.getByRole("tablist", { name: "Permissions sections" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Groups" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Rules" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tab", { name: "Privacy" })).toHaveAttribute("aria-selected", "false");
    expect(await screen.findByText("No groups.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Rules" }));
    expect(window.location.hash).toBe("#rules");
    expect(await screen.findByText(/No rules on this workspace/)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Rules" })).toHaveAttribute("aria-selected", "true");

    fireEvent.click(screen.getByRole("tab", { name: "Privacy" }));
    expect(window.location.hash).toBe("#privacy");
    expect(await screen.findByRole("heading", { name: "Privacy" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Privacy" })).toHaveAttribute("aria-selected", "true");
  });

  it("opens /permissions#privacy directly and updates upstream PII consent", async () => {
    const calls = installFetch();
    renderPage("#privacy");

    expect(await screen.findByRole("heading", { name: "Privacy" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Privacy" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("status")).toHaveTextContent("No upstream PII consent selected");

    fireEvent.click(screen.getByRole("checkbox", { name: /Legal names/ }));

    await waitFor(() => {
      expect(calls.some((call) => {
        if (call.init.method !== "PUT") return false;
        const body = JSON.parse(String(call.init.body)) as { upstream_pii_consent: string[] };
        return body.upstream_pii_consent[0] === "legal_name";
      })).toBe(true);
    });
    expect(screen.getByRole("checkbox", { name: /Legal names/ })).toBeChecked();
  });

  it("tracks browser back and forward across tab hashes", async () => {
    installFetch();
    renderPage("#groups");
    expect(await screen.findByText("No groups.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Rules" }));
    expect(await screen.findByText(/No rules on this workspace/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Privacy" }));
    expect(await screen.findByRole("heading", { name: "Privacy" })).toBeInTheDocument();

    window.history.back();
    await waitFor(() => expect(window.location.hash).toBe("#rules"));
    expect(await screen.findByText(/No rules on this workspace/)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Rules" })).toHaveAttribute("aria-selected", "true");

    window.history.forward();
    await waitFor(() => expect(window.location.hash).toBe("#privacy"));
    expect(await screen.findByRole("heading", { name: "Privacy" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Privacy" })).toHaveAttribute("aria-selected", "true");
  });
});
