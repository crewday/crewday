import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import GuestPage from "./GuestPage";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? "OK" : "Error",
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function renderGuest(initial = "/w/acme/guest/token-123") {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route path="/guest/:token" element={<GuestPage />} />
          <Route path="/w/:slug/guest/:token" element={<GuestPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
});

describe("GuestPage", () => {
  it("loads the public welcome bundle with the token in the bare API path", async () => {
    const calls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (url: string | URL | Request) => {
        const resolved = typeof url === "string" ? url : url.toString();
        calls.push(resolved);
        return jsonResponse({
          property_id: "prop_1",
          property_name: "Villa Rosa",
          unit_id: "unit_1",
          unit_name: "Garden Suite",
          welcome: {
            wifi_ssid: "villa-guest",
            wifi_password: "secret-wifi",
            door_code: "4281",
            parking: "Bay 3B",
            house_rules: "Quiet hours: 22:00-08:00.",
            trash_schedule_md: "Bins go out Monday morning.",
            emergency_contacts: [
              { label: "Host", name: "Elodie", phone_e164: "+33601020304" },
            ],
          },
          checklist: [{ id: "c1", label: "Strip beds" }],
          assets: [
            {
              id: "a1",
              name: "Coffee machine",
              guest_instructions_md: "Use the silver pods.",
              cover_photo_url: null,
            },
          ],
          check_in_at: "2026-04-29T15:00:00Z",
          check_out_at: "2026-05-02T10:00:00Z",
          guest_name: "Ada Guest",
        });
      },
    );

    renderGuest();

    expect(await screen.findByText("Garden Suite")).toBeInTheDocument();
    expect(screen.getByText("villa-guest")).toBeInTheDocument();
    expect(screen.getByText("secret-wifi")).toBeInTheDocument();
    expect(screen.getByText("Front door code: 4281")).toBeInTheDocument();
    expect(screen.getByText("Strip beds")).toBeInTheDocument();
    expect(screen.getByText("Coffee machine")).toBeInTheDocument();
    expect(calls).toContain("/api/v1/stays/welcome/token-123");
  });

  it("renders the expired-link state for gone responses", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ error: "welcome_link_expired" }, 410),
    );

    renderGuest();

    expect(
      await screen.findByText("This guest link is no longer valid."),
    ).toBeInTheDocument();
  });
});
