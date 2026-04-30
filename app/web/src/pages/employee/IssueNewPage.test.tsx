import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import type { ReactElement } from "react";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import IssueNewPage from "./IssueNewPage";

interface FakeResponse {
  status?: number;
  body: unknown;
}

interface FetchCall {
  url: string;
  init: RequestInit;
}

function installFetch(scripted: Record<string, FakeResponse[]>): {
  calls: FetchCall[];
  restore: () => void;
} {
  const calls: FetchCall[] = [];
  const original = globalThis.fetch;
  const queues: Record<string, FakeResponse[]> = {};
  for (const [suffix, responses] of Object.entries(scripted)) {
    queues[suffix] = [...responses];
  }
  const suffixes = Object.keys(queues).sort((a, b) => b.length - a.length);
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    const suffix = suffixes.find((candidate) => resolved.endsWith(candidate));
    if (!suffix) throw new Error(`Unscripted fetch: ${resolved}`);
    const next = queues[suffix]!.shift();
    if (!next) throw new Error(`No more responses for: ${resolved}`);
    const status = next.status ?? 200;
    const ok = status >= 200 && status < 300;
    return {
      ok,
      status,
      statusText: ok ? "OK" : "Error",
      text: async () => JSON.stringify(next.body),
    } as unknown as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function Harness(): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/issues/new"]}>
        <Routes>
          <Route path="/issues/new" element={<><IssueNewPage /><LocationProbe /></>} />
          <Route path="/me" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function LocationProbe(): ReactElement {
  const loc = useLocation();
  return <span data-testid="location">{loc.pathname}</span>;
}

function property(): unknown {
  return {
    id: "prop_1",
    name: "Villa Sud",
    city: "Nice",
    timezone: "Europe/Paris",
    color: "moss",
    kind: "str",
    areas: ["Kitchen", "Bedroom"],
    evidence_policy: "inherit",
    country: "FR",
    locale: "fr",
    settings_override: {},
    client_org_id: null,
  };
}

function issue(): unknown {
  return {
    id: "issue_1",
    reported_by: "user_1",
    property_id: "prop_1",
    area: "Master bathroom",
    severity: "urgent",
    category: "safety",
    title: "Bathroom tap dripping",
    body: "Water is leaking under the sink.",
    reported_at: "2026-04-30T03:00:00Z",
    status: "open",
  };
}

beforeEach(() => {
  __resetApiProvidersForTests();
  registerWorkspaceSlugGetter(() => "acme");
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  vi.restoreAllMocks();
});

describe("IssueNewPage", () => {
  it("submits a worker issue to the real JSON API contract", async () => {
    const env = installFetch({
      "/api/v1/properties": [{ body: [property()] }],
      "/api/v1/issues": [{ status: 201, body: issue() }],
    });
    render(<Harness />);

    await screen.findByText("Villa Sud");
    fireEvent.change(screen.getByLabelText("Short title"), {
      target: { value: "Bathroom tap dripping" },
    });
    fireEvent.change(screen.getByLabelText("Area"), {
      target: { value: "Master bathroom" },
    });
    fireEvent.click(screen.getByLabelText("Safety"));
    fireEvent.click(screen.getByLabelText("Urgent — needs action today"));
    fireEvent.change(screen.getByLabelText("What happened?"), {
      target: { value: "Water is leaking under the sink." },
    });

    expect(screen.getByRole("button", { name: "Attach photo" })).toBeInTheDocument();
    const input = screen.getByLabelText("Photo file") as HTMLInputElement;
    expect(input.accept).toBe("image/*");
    expect(input.getAttribute("capture")).toBe("environment");

    fireEvent.click(screen.getByRole("button", { name: "Send to manager" }));

    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/me"));
    const submit = env.calls.find((call) => call.url.endsWith("/api/v1/issues"));
    expect(submit?.init.method).toBe("POST");
    expect((submit?.init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect(JSON.parse(submit?.init.body as string)).toEqual({
      title: "Bathroom tap dripping",
      severity: "urgent",
      category: "safety",
      property_id: "prop_1",
      area: "Master bathroom",
      body: "Water is leaking under the sink.",
    });
    env.restore();
  });

  it("renders the property load failure state", async () => {
    const env = installFetch({
      "/api/v1/properties": [{ status: 500, body: { title: "Broken" } }],
    });
    render(<Harness />);

    expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
    env.restore();
  });
});
