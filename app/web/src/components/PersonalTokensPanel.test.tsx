import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import { installFetchRoutes } from "@/test/helpers";
import PersonalTokensPanel from "./PersonalTokensPanel";

// `PersonalTokensPanel` lives inside `/me`, which is wrapped by
// WorkspaceGate — so a slug is always live. The panel calls the
// bare-host `/api/v1/me/tokens` surface; we register a slug here to
// catch the regression where `resolveApiPath` would mistakenly rewrite
// the URL into `/w/<slug>/api/v1/me/tokens` (the server only mounts
// PATs at the bare host, so that rewrite would 404).

function Harness(): ReactElement {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/me"]}>
        <PersonalTokensPanel />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function token(overrides: Record<string, unknown> = {}): unknown {
  return {
    key_id: "tok_01",
    label: "kitchen-printer",
    kind: "personal",
    prefix: "mip_tok_01",
    scopes: { "me.tasks:read": true },
    created_at: "2026-04-01T08:00:00Z",
    expires_at: "2026-07-01T08:00:00Z",
    last_used_at: null,
    revoked_at: null,
    ...overrides,
  };
}

function created(overrides: Record<string, unknown> = {}): unknown {
  return {
    token: "mip_tok_99_secretpart",
    key_id: "tok_99",
    prefix: "mip_tok_99",
    expires_at: "2026-07-30T08:00:00Z",
    kind: "personal",
    ...overrides,
  };
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  registerWorkspaceSlugGetter(() => "dev");
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("PersonalTokensPanel", () => {
  it("lists existing personal tokens from /api/v1/me/tokens (bare-host)", async () => {
    const env = installFetchRoutes(
      {
        "/api/v1/me/tokens": [{ body: [token()] }],
      },
      { match: "endsWith" },
    );
    try {
      render(<Harness />);

      // The list call resolves to the bare-host surface, NOT
      // /w/dev/api/v1/me/tokens.
      await screen.findByText("kitchen-printer");
      const list = env.calls.find((c) => c.url.endsWith("/api/v1/me/tokens"));
      expect(list).toBeDefined();
      expect(list?.url).not.toContain("/w/dev/");
      expect(list?.init.method ?? "GET").toBe("GET");
    } finally {
      env.restore();
    }
  });

  it("mints a PAT with me:* scopes via POST and shows the plaintext once", async () => {
    const env = installFetchRoutes(
      {
        "/api/v1/me/tokens": [
          { body: [] },
          { status: 201, body: created() },
          { body: [{ ...(token() as object), key_id: "tok_99" }] },
        ],
      },
      { match: "endsWith" },
    );
    try {
      render(<Harness />);

      // Empty state first.
      await screen.findByText("No personal tokens yet");

      fireEvent.click(screen.getByRole("button", { name: /New token/ }));

      const nameInput = screen.getByLabelText("Name") as HTMLInputElement;
      fireEvent.change(nameInput, { target: { value: "kitchen-printer" } });

      // The default scope (me.tasks:read) is preselected; submit.
      fireEvent.click(screen.getByRole("button", { name: "Create token" }));

      // The reveal panel surfaces the plaintext exactly once.
      await screen.findByText("Save this token now");
      expect(screen.getByText("mip_tok_99_secretpart")).toBeInTheDocument();

      const post = env.calls.find(
        (c) =>
          c.url.endsWith("/api/v1/me/tokens") && c.init.method === "POST",
      );
      expect(post).toBeDefined();
      expect(post?.url).not.toContain("/w/dev/");
      const body = JSON.parse(String(post?.init.body));
      expect(body).toEqual({
        label: "kitchen-printer",
        scopes: { "me.tasks:read": true },
        expires_at_days: 90,
      });
    } finally {
      env.restore();
    }
  });

  it("revokes a PAT via DELETE /api/v1/me/tokens/{id}", async () => {
    const env = installFetchRoutes(
      {
        "/api/v1/me/tokens": [
          { body: [token()] },
          { body: [{ ...(token() as object), revoked_at: "2026-04-15T08:00:00Z" }] },
        ],
        "/api/v1/me/tokens/tok_01": [{ status: 204, body: null }],
      },
      { match: "endsWith" },
    );
    try {
      render(<Harness />);
      await screen.findByText("kitchen-printer");

      fireEvent.click(screen.getByRole("button", { name: /Revoke/ }));

      await waitFor(() => {
        const del = env.calls.find(
          (c) =>
            c.url.endsWith("/api/v1/me/tokens/tok_01") &&
            c.init.method === "DELETE",
        );
        expect(del).toBeDefined();
        expect(del?.url).not.toContain("/w/dev/");
      });
    } finally {
      env.restore();
    }
  });
});
