import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import { installFetchRoutes } from "@/test/helpers";
import type { ApiTokenCreated } from "@/types/api";
import MintTokenModal from "./MintTokenModal";
import { WORKSPACE_SCOPES } from "./lib/tokenStatus";

function Harness({
  onCreated = vi.fn(),
  onCancel = vi.fn(),
}: {
  onCreated?: (created: ApiTokenCreated) => void;
  onCancel?: () => void;
}): ReactElement {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return (
    <QueryClientProvider client={qc}>
      <MintTokenModal onCreated={onCreated} onCancel={onCancel} />
    </QueryClientProvider>
  );
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

describe("MintTokenModal", () => {
  it("posts all workspace scopes with no expiry when requested", async () => {
    const onCreated = vi.fn();
    const env = installFetchRoutes(
      {
        "/w/dev/api/v1/auth/tokens": [
          {
            status: 201,
            body: {
              token: "mip_tok_99_secretpart",
              key_id: "tok_99",
              prefix: "mip_tok_99",
              expires_at: null,
              kind: "scoped",
            },
          },
        ],
      },
      { match: "endsWith" },
    );
    try {
      render(<Harness onCreated={onCreated} />);

      fireEvent.click(screen.getByRole("button", { name: "Add all scopes" }));
      fireEvent.click(screen.getByRole("button", { name: "Never expires" }));
      fireEvent.click(screen.getByRole("button", { name: "Create token" }));

      await waitFor(() => expect(onCreated).toHaveBeenCalledOnce());
      const post = env.calls.find(
        (call) =>
          call.url.endsWith("/w/dev/api/v1/auth/tokens") &&
          call.init.method === "POST",
      );
      expect(post).toBeDefined();
      expect(JSON.parse(String(post?.init.body))).toEqual({
        label: "my-script",
        scopes: Object.fromEntries(WORKSPACE_SCOPES.map((scope) => [scope, true])),
        never_expires: true,
      });
    } finally {
      env.restore();
    }
  });
});
