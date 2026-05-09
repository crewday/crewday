import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { type ReactElement } from "react";
import WorkspaceCreatePage from "./WorkspaceCreatePage";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import { installFetchRoutes, type FakeResponse } from "@/test/helpers";
import * as preferences from "@/lib/preferences";

function installFetch(scripted: Record<string, FakeResponse[]>) {
  return installFetchRoutes(scripted, { match: "endsWith" });
}

function Harness(): ReactElement {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/workspaces/new"]}>
        <WorkspaceProvider>
          <Routes>
            <Route path="/workspaces/new" element={<><WorkspaceCreatePage /><LocationProbe /></>} />
            <Route path="/w/:slug/today" element={<LocationProbe />} />
          </Routes>
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function flush(): Promise<void> {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
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

describe("<WorkspaceCreatePage>", () => {
  it("normalizes slug input, posts to signed-in workspace creation, and redirects", async () => {
    const persistWorkspace = vi.spyOn(preferences, "persistWorkspace");
    const { calls, restore } = installFetch({
      "/api/v1/me/workspaces": [
        {
          status: 201,
          body: {
            workspace_id: "ws_villa",
            workspace_slug: "villasud",
            redirect: "/w/villasud/today",
          },
        },
      ],
    });

    try {
      render(<Harness />);

      const name = screen.getByTestId("workspace-create-name") as HTMLInputElement;
      const slug = screen.getByTestId("workspace-create-slug") as HTMLInputElement;
      fireEvent.change(name, { target: { value: "Villa Sud" } });
      fireEvent.change(slug, { target: { value: "Villa Sud!" } });
      expect(slug.value).toBe("villasud");
      fireEvent.change(slug, { target: { value: "Villa-Sud-" } });
      expect(slug.value).toBe("villa-sud-");
      fireEvent.change(slug, { target: { value: "Villa Sud!" } });

      await act(async () => {
        fireEvent.submit(name.closest("form")!);
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      await flush();

      const posts = calls.filter((call) => call.url === "/api/v1/me/workspaces");
      expect(posts).toHaveLength(1);
      expect(JSON.parse(posts[0]!.init.body as string)).toEqual({
        slug: "villasud",
        name: "Villa Sud",
      });
      expect(persistWorkspace).toHaveBeenCalledWith("villasud");
      expect(screen.getByTestId("location")).toHaveTextContent("/w/villasud/today");
    } finally {
      restore();
    }
  });

  it("reuses signup slug errors and accepts server suggestions", async () => {
    const { restore } = installFetch({
      "/api/v1/me/workspaces": [
        {
          status: 409,
          body: {
            detail: {
              error: "slug_taken",
              suggested_alternative: "villa-sud-2",
            },
          },
        },
      ],
    });

    try {
      render(<Harness />);

      const name = screen.getByTestId("workspace-create-name") as HTMLInputElement;
      const slug = screen.getByTestId("workspace-create-slug") as HTMLInputElement;
      fireEvent.change(name, { target: { value: "Villa Sud" } });
      fireEvent.change(slug, { target: { value: "villa-sud" } });

      await act(async () => {
        fireEvent.submit(name.closest("form")!);
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      await flush();

      expect(screen.getByTestId("workspace-create-slug-error")).toHaveTextContent(
        "That workspace handle is already in use.",
      );
      fireEvent.click(screen.getByTestId("workspace-create-slug-accept"));
      expect(slug.value).toBe("villa-sud-2");
      expect(screen.queryByTestId("workspace-create-slug-error")).toBeNull();
    } finally {
      restore();
    }
  });
});

function LocationProbe(): ReactElement {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}
