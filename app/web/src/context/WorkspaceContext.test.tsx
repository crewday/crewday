import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode } from "react";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests, resolveApiPath } from "@/lib/api";
import { __resetQueryKeyGetterForTests, qk } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";

// These tests guard the timing contract documented in
// `WorkspaceContext.tsx`: children mounted below `<WorkspaceProvider>`
// must see the correct slug from their very first render, not after
// some deferred `useEffect` fires. A regression here would leave the
// cache wedged under `["w", "_", ...]` while the actual fetch went to
// `/w/<slug>/...` — a subtle inconsistency that SSE invalidation then
// can't repair.

function Providers({ children }: { children: ReactNode }) {
  const qc = new QueryClient();
  return (
    <QueryClientProvider client={qc}>
      <WorkspaceProvider>{children}</WorkspaceProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<WorkspaceProvider> — getter wiring timing", () => {
  it("exposes the cookie-resolved slug to children synchronously on first render", () => {
    vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");

    // Observer that samples `qk.*` during its own render — the same
    // thing `useQuery({ queryKey: qk.tasks() })` does. If the getter
    // isn't wired until after an effect, this observer would see the
    // `"_"` sentinel instead of `"acme"`.
    const observed: unknown[] = [];
    function Observer() {
      observed.push(qk.tasks());
      observed.push(resolveApiPath("/api/v1/tasks"));
      return null;
    }

    render(
      <Providers>
        <Observer />
      </Providers>,
    );

    expect(observed[0]).toEqual(["w", "acme", "tasks"]);
    expect(observed[1]).toBe("/w/acme/api/v1/tasks");
  });

  it("is idempotent across repeated renders (React 18 StrictMode double-invoke safe)", () => {
    vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");

    const { rerender } = render(
      <Providers>
        <div data-testid="child" />
      </Providers>,
    );
    // Rerender several times to simulate StrictMode's double-mount and
    // general parent-driven churn. The registration guard must make
    // repeat registrations a no-op.
    rerender(
      <Providers>
        <div data-testid="child" />
      </Providers>,
    );
    rerender(
      <Providers>
        <div data-testid="child" />
      </Providers>,
    );

    // Slug remains correctly resolved after repeated renders — the
    // getter still points at a live `slugRef`.
    expect(qk.tasks()).toEqual(["w", "acme", "tasks"]);
    expect(resolveApiPath("/api/v1/tasks")).toBe("/w/acme/api/v1/tasks");
  });
});
