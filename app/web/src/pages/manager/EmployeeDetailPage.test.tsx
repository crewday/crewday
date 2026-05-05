import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import EmployeeDetailPage from "./EmployeeDetailPage";
import { jsonResponse } from "@/test/helpers";

interface TestUserWorkRole {
  id: string;
  user_id: string;
  workspace_id: string;
  work_role_id: string;
  started_on: string;
  ended_on: string | null;
  pay_rule_id: string | null;
  created_at: string;
  deleted_at: string | null;
}

function installFetch(options: { failRoleDelete?: boolean; failRoleList?: boolean; failRoleSave?: boolean } = {}) {
  const calls: { url: string; method: string; body?: unknown }[] = [];
  const original = globalThis.fetch;
  const roleRows = [
    {
      id: "wr_housekeeper",
      workspace_id: "ws_acme",
      key: "housekeeper",
      name: "Housekeeper",
      description_md: "",
      default_settings_json: {},
      icon_name: "BrushCleaning",
      created_at: "2026-01-01T00:00:00Z",
      deleted_at: null,
    },
    {
      id: "wr_cook",
      workspace_id: "ws_acme",
      key: "cook",
      name: "Cook",
      description_md: "",
      default_settings_json: {},
      icon_name: "ChefHat",
      created_at: "2026-01-01T00:00:00Z",
      deleted_at: null,
    },
    {
      id: "wr_gardener",
      workspace_id: "ws_acme",
      key: "gardener",
      name: "Gardener",
      description_md: "",
      default_settings_json: {},
      icon_name: "Leaf",
      created_at: "2026-01-01T00:00:00Z",
      deleted_at: null,
    },
  ];
  const linkRows: TestUserWorkRole[] = [
    {
      id: "uwr_housekeeper",
      user_id: "emp_1",
      workspace_id: "ws_acme",
      work_role_id: "wr_housekeeper",
      started_on: "2026-01-01",
      ended_on: null,
      pay_rule_id: null,
      created_at: "2026-01-01T00:00:00Z",
      deleted_at: null,
    },
    {
      id: "uwr_gardener",
      user_id: "emp_1",
      workspace_id: "ws_acme",
      work_role_id: "wr_gardener",
      started_on: "2026-01-01",
      ended_on: null,
      pay_rule_id: null,
      created_at: "2026-01-01T00:00:00Z",
      deleted_at: null,
    },
  ];
  const roleById = new Map(roleRows.map((role) => [role.id, role]));
  const bodyOf = (body: BodyInit | null | undefined) =>
    typeof body === "string" ? JSON.parse(body) as unknown : undefined;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const parsed = new URL(resolved, "http://crewday.test");
    const path = parsed.pathname;
    const cursor = parsed.searchParams.get("cursor");
    const method = init?.method ?? "GET";
    const body = bodyOf(init?.body);
    calls.push({ url: resolved, method, body });
    if (path === "/w/acme/api/v1/employees/emp_1") {
      return jsonResponse({
        subject: {
          id: "emp_1",
          name: "Maya Santos",
          roles: linkRows
            .filter((link) => link.deleted_at === null)
            .map((link) => roleById.get(link.work_role_id)?.key ?? link.work_role_id)
            .sort(),
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
    if (path === "/w/acme/api/v1/work_roles") {
      if (options.failRoleList) {
        return jsonResponse({ detail: "Role catalog is unavailable." }, 500);
      }
      return jsonResponse(
        cursor === "roles-page-2"
          ? { data: roleRows.slice(2), next_cursor: null, has_more: false }
          : { data: roleRows.slice(0, 2), next_cursor: "roles-page-2", has_more: true },
      );
    }
    if (path === "/w/acme/api/v1/users/emp_1/user_work_roles") {
      const liveLinks = linkRows.filter((link) => link.deleted_at === null);
      return jsonResponse(
        cursor === "links-page-2"
          ? { data: liveLinks.slice(1), next_cursor: null, has_more: false }
          : { data: liveLinks.slice(0, 1), next_cursor: "links-page-2", has_more: true },
      );
    }
    if (path === "/w/acme/api/v1/user_work_roles" && method === "POST") {
      if (options.failRoleSave) {
        return jsonResponse({ detail: "Selected role is not valid." }, 422);
      }
      const payload = body as { user_id: string; work_role_id: string; started_on: string };
      const next = {
        id: "uwr_" + payload.work_role_id,
        user_id: payload.user_id,
        workspace_id: "ws_acme",
        work_role_id: payload.work_role_id,
        started_on: payload.started_on,
        ended_on: null,
        pay_rule_id: null,
        created_at: "2026-01-02T00:00:00Z",
        deleted_at: null,
      };
      linkRows.push(next);
      return jsonResponse(next, 201);
    }
    if (path.startsWith("/w/acme/api/v1/user_work_roles/") && method === "DELETE") {
      if (options.failRoleDelete) {
        return jsonResponse({ detail: "Role link could not be removed." }, 500);
      }
      const linkId = path.slice("/w/acme/api/v1/user_work_roles/".length);
      const link = linkRows.find((row) => row.id === linkId);
      if (link) link.deleted_at = "2026-01-02T00:00:00Z";
      return jsonResponse(null, 204);
    }
    if (path === "/w/acme/api/v1/properties") {
      return jsonResponse([]);
    }
    if (path === "/w/acme/api/v1/employees/emp_1/settings") {
      return jsonResponse({
        overrides: { "payroll.locale": "pt-PT" },
        resolved: {
          "payroll.locale": { value: "pt-PT", source: "employee" },
        },
      });
    }
    if (path === "/w/acme/api/v1/settings/catalog") {
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
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
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
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  });
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

  it("opens the work-role editor and saves selected roles", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Maya Santos")).toBeInTheDocument();
      const editRoles = screen.getByRole("button", { name: "Edit roles" });
      expect(editRoles).toBeEnabled();
      expect(screen.queryByText("Role editing is not implemented yet.")).not.toBeInTheDocument();

      fireEvent.click(editRoles);
      const dialog = await screen.findByRole("dialog", { name: "Edit work roles" });
      const housekeeper = await within(dialog).findByLabelText(/Housekeeper/);
      const cook = within(dialog).getByLabelText(/Cook/);
      const gardener = within(dialog).getByLabelText(/Gardener/);
      expect(housekeeper).toBeChecked();
      expect(cook).not.toBeChecked();
      expect(gardener).toBeChecked();

      fireEvent.click(cook);
      fireEvent.click(within(dialog).getByRole("button", { name: "Save roles" }));

      await waitFor(() => {
        expect(screen.queryByRole("dialog", { name: "Edit work roles" })).not.toBeInTheDocument();
      });
      expect(await screen.findByText("cook · gardener · housekeeper · +351 555 0100")).toBeInTheDocument();
      expect(fake.calls).toContainEqual({
        url: "/w/acme/api/v1/user_work_roles",
        method: "POST",
        body: {
          user_id: "emp_1",
          work_role_id: "wr_cook",
          started_on: "2026-01-01",
        },
      });

      fireEvent.click(screen.getByRole("button", { name: "More actions" }));
      expect(screen.getByRole("menuitem", { name: /Message/ })).toBeDisabled();
      expect(screen.getByText("Direct manager-to-worker messaging is not part of v1.")).toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("keeps the role editor open with visible errors when role saves fail", async () => {
    const fake = installFetch({ failRoleDelete: true });
    try {
      render(<Harness />);

      expect(await screen.findByText("Maya Santos")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Edit roles" }));
      const dialog = await screen.findByRole("dialog", { name: "Edit work roles" });
      fireEvent.click(await within(dialog).findByLabelText(/Cook/));
      fireEvent.click(within(dialog).getByLabelText(/Gardener/));
      fireEvent.click(within(dialog).getByRole("button", { name: "Save roles" }));

      expect(await within(dialog).findByRole("alert")).toHaveTextContent(
        "Role link could not be removed. Some role changes may have been saved",
      );
      expect(screen.getByRole("dialog", { name: "Edit work roles" })).toBeInTheDocument();
      await waitFor(() => {
        expect(within(dialog).getByLabelText(/Cook/)).toBeChecked();
      });
    } finally {
      fake.restore();
    }
  });

  it("shows a visible error when work roles cannot be loaded", async () => {
    const fake = installFetch({ failRoleList: true });
    try {
      render(<Harness />);

      expect(await screen.findByText("Maya Santos")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Edit roles" }));
      const dialog = await screen.findByRole("dialog", { name: "Edit work roles" });

      expect(await within(dialog).findByRole("alert")).toHaveTextContent("Work roles could not be loaded.");
      expect(within(dialog).getByRole("button", { name: "Save roles" })).toBeDisabled();
    } finally {
      fake.restore();
    }
  });
});
