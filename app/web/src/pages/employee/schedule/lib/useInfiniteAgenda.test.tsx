import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchJson } from "@/lib/api";
import type { MySchedulePayload } from "@/types/api";
import { useInfiniteAgenda } from "./useInfiniteAgenda";

vi.mock("@/lib/api", () => ({
  fetchJson: vi.fn(),
}));

const fetchJsonMock = vi.mocked(fetchJson);

function emptyPayload(from: string, to: string): MySchedulePayload {
  return {
    window: { from, to },
    user_id: "u1",
    weekly_availability: [],
    rulesets: [],
    slots: [],
    assignments: [],
    tasks: [],
    properties: [],
    leaves: [],
    overrides: [],
    bookings: [],
  };
}

function makeWrapper(): ({ children }: { children: ReactNode }) => ReactNode {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }): ReactNode {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

function payloadForPath(path: string): MySchedulePayload {
  const url = new URL(path, "http://crewday.local");
  return emptyPayload(
    url.searchParams.get("from") ?? "",
    url.searchParams.get("to") ?? "",
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("useInfiniteAgenda", () => {
  it("fetches /me/schedule with from and to wire parameters", async () => {
    fetchJsonMock.mockImplementation(async (path: string) => payloadForPath(path));

    const { result } = renderHook(
      () => useInfiniteAgenda(new Date(2026, 4, 4), "2026-05-04"),
      { wrapper: makeWrapper() },
    );

    await waitFor(() => expect(result.current.q.isSuccess).toBe(true));

    expect(fetchJsonMock).toHaveBeenCalledTimes(1);
    const path = fetchJsonMock.mock.calls[0]![0];
    expect(path).toBe("/api/v1/me/schedule?from=2026-05-04&to=2026-05-10");
    expect(path).toContain("from=");
    expect(path).not.toContain("from_=");
  });

  it("advances the fetched window when the next agenda page loads", async () => {
    fetchJsonMock.mockImplementation(async (path: string) => payloadForPath(path));

    const { result } = renderHook(
      () => useInfiniteAgenda(new Date(2026, 4, 4), "2026-05-04"),
      { wrapper: makeWrapper() },
    );

    await waitFor(() => expect(result.current.q.isSuccess).toBe(true));

    await act(async () => {
      await result.current.q.fetchNextPage();
    });

    expect(fetchJsonMock).toHaveBeenCalledTimes(2);
    const path = fetchJsonMock.mock.calls[1]![0];
    expect(path).toBe("/api/v1/me/schedule?from=2026-05-11&to=2026-05-17");
    expect(path).not.toContain("from_=");
  });
});
