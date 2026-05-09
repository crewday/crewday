import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import ApiTokensPage from "./ApiTokensPage";

vi.mock("@/lib/api", () => ({
  fetchJson: vi.fn(),
}));

const fetchJsonMock = vi.mocked(fetchJson);

afterEach(() => {
  cleanup();
  fetchJsonMock.mockReset();
});

function renderApiTokens(): void {
  fetchJsonMock.mockResolvedValue({ data: [], next_cursor: null });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/w/acme/tokens"]}>
        <ApiTokensPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("<ApiTokensPage>", () => {
  it("links personal tokens to the active workspace profile route", async () => {
    renderApiTokens();

    expect(await screen.findByRole("heading", { name: "Workspace tokens" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "/me" })).toHaveAttribute("href", "/w/acme/me");
    expect(fetchJsonMock).toHaveBeenCalledWith("/api/v1/auth/tokens");
  });
});
