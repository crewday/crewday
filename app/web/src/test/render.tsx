// crewday — shared Vitest render helper.
//
// Most page suites mount the unit-under-test inside the same shell:
// QueryClient + MemoryRouter + (optionally) WorkspaceProvider /
// AuthProvider. `renderWithProviders` opts in to each layer so callers
// stay explicit — there is no opaque "render everything" mode. Tests
// that need a custom routing tree (e.g. `<Routes><Route path=...>`)
// pass the entire `<MemoryRouter>` themselves and skip the `router`
// option.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderOptions, type RenderResult } from "@testing-library/react";
import { type ReactElement, type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { AuthProvider } from "@/auth";
import { WorkspaceProvider } from "@/context/WorkspaceContext";

export interface RenderWithProvidersOptions {
  /** Provide a pre-built QueryClient (e.g. to inspect mutation state). */
  queryClient?: QueryClient;
  /** Wrap in `<MemoryRouter initialEntries={[router]}>` when set. */
  router?: string;
  /** Wrap in `<WorkspaceProvider>` (requires QueryClient). */
  workspace?: boolean;
  /** Wrap in `<AuthProvider>`. */
  auth?: boolean;
  /** Forwarded to `@testing-library/react#render`. */
  renderOptions?: Omit<RenderOptions, "wrapper">;
}

/**
 * Build a fresh QueryClient with retry disabled so tests never wait on
 * exponential backoff. Exposed for suites that want the same client to
 * inspect cache state before the render.
 */
export function makeTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

/**
 * Render `ui` wrapped in the requested providers. The provider chain
 * is fixed (Query → Router → Workspace → Auth → ui) because order
 * matters — `WorkspaceProvider` calls `useQueryClient()`, so it has to
 * sit inside `QueryClientProvider`. `AuthProvider` reads the workspace
 * slug to scope `/auth/me` requests.
 */
export function renderWithProviders(
  ui: ReactElement,
  options: RenderWithProvidersOptions = {},
): RenderResult & { queryClient: QueryClient } {
  const queryClient = options.queryClient ?? makeTestQueryClient();
  const wrap = (node: ReactNode): ReactNode => {
    let tree: ReactNode = node;
    if (options.auth) tree = <AuthProvider>{tree}</AuthProvider>;
    if (options.workspace) tree = <WorkspaceProvider>{tree}</WorkspaceProvider>;
    if (options.router !== undefined) {
      tree = <MemoryRouter initialEntries={[options.router]}>{tree}</MemoryRouter>;
    }
    return <QueryClientProvider client={queryClient}>{tree}</QueryClientProvider>;
  };
  const result = render(<>{wrap(ui)}</>, options.renderOptions);
  return Object.assign(result, { queryClient });
}
