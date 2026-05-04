import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import EmployeeDetailPage from "./EmployeeDetailPage";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? "OK" : "Error",
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function installFetch() {
  const calls: { url: string; method: string }[] = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const method = init?.method ?? "GET";
    calls.push({ url: resolved, method });
    if (resolved === "/w/acme/api/v1/employees/emp_1") {
      return jsonResponse({
        subject: {
          id: "emp_1",
          name: "Maya Santos",
          roles: ["housekeeper"],
          properties: [],
          avatar_initials: "MS",
          avatar_file_id: null,
          avatar_url: null,
          phone: "+351 555 0100",
          email: "maya@example.com",
          started_on: "2026-01-01",
          capabilities: {},
          workspaces: ["ws_owner"],
          villas: [],
          language: "en",
          weekly_availability: {},
          evidence_policy: "inherit",
          preferred_locale: null,
          settings_override: {},
        },
        subject_tasks: [],
        subject_expenses: [],
        subject_leaves: [],
        subject_payslips: [],
      });
    }
    if (resolved === "/w/acme/api/v1/properties") {
      return jsonResponse([]);
    }
    if (resolved === "/w/acme/api/v1/employees/emp_1/settings") {
      return jsonResponse({
        overrides: { "payroll.locale": "pt-PT" },
        resolved: {
          "payroll.locale": { value: "pt-PT", source: "employee" },
        },
      });
    }
    if (resolved === "/w/acme/api/v1/settings/catalog") {
      return jsonResponse([
        {
          key: "payroll.locale",
          label: "Payroll locale",
          type: "enum",
          catalog_default: "en-US",
          enum_values: ["en-US", "pt-PT"],
          override_scope: "E",
          description: "Locale for payroll formatting.",
          spec: "09",
        },
      ]);
    }
    throw new Error(`Unexpected fetch call: ${resolved}`);
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/employee/emp_1"]}>
        <WorkspaceProvider>
          <Routes>
            <Route path="/employee/:eid" element={<EmployeeDetailPage />} />
          </Routes>
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
  window.location.hash = "";
});

afterEach(() => {
  cleanup();
  window.location.hash = "";
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<EmployeeDetailPage>", () => {
  it("renders all canonical employee tabs as stable hash links", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Maya Santos")).toBeInTheDocument();
      for (const [label, hash] of [
        ["Overview", "#overview"],
        ["Shifts", "#shifts"],
        ["Payslips", "#payslips"],
        ["Leaves", "#leaves"],
        ["Policies", "#policies"],
        ["Settings", "#settings"],
        ["Passkeys", "#passkeys"],
      ]) {
        expect(screen.getByRole("link", { name: label }).getAttribute("href")).toBe(hash);
      }
      expect(screen.getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");

      window.location.hash = "#passkeys";
      window.dispatchEvent(new Event("hashchange"));
      await waitFor(() => {
        expect(screen.getByRole("link", { name: "Passkeys" })).toHaveAttribute("aria-current", "page");
      });
      expect(fake.calls).not.toContainEqual({
        url: "/w/acme/api/v1/employees/emp_1/settings",
        method: "GET",
      });
    } finally {
      fake.restore();
    }
  });

  it("selects the active tab from hash navigation and keeps settings loading", async () => {
    const fake = installFetch();
    try {
      window.location.hash = "#settings";
      render(<Harness />);

      expect(await screen.findByText("Settings overrides")).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("aria-current", "page");
      expect(fake.calls).toContainEqual({
        url: "/w/acme/api/v1/employees/emp_1/settings",
        method: "GET",
      });

      window.location.hash = "#payslips";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
      await waitFor(() => {
        expect(screen.getByRole("link", { name: "Payslips" })).toHaveAttribute("aria-current", "page");
      });
    } finally {
      fake.restore();
    }
  });
});
