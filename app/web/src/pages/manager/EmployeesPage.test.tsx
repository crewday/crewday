import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import {
  __resetQueryKeyGetterForTests,
  registerQueryKeyWorkspaceGetter,
} from "@/lib/queryKeys";
import { installFetchRouteHandlers, type FetchRoute } from "@/test/helpers";
import type { WorkRole } from "@/types/api";
import EmployeesPage from "./EmployeesPage";

const originalShowModal = HTMLDialogElement.prototype.showModal;
const originalClose = HTMLDialogElement.prototype.close;

const EMPLOYEE = {
  id: "emp_1",
  name: "Mina Manager",
  roles: ["manager"],
  properties: ["prop_1"],
  avatar_initials: "MM",
  avatar_file_id: null,
  avatar_url: null,
  phone: "+15550100",
  email: "mina@example.test",
  started_on: "2026-01-01",
  capabilities: {},
  workspaces: ["ws_1"],
  villas: ["prop_1"],
  language: "en",
  weekly_availability: {},
  evidence_policy: "inherit",
  preferred_locale: null,
  settings_override: {},
};

const PROPERTY = {
  id: "prop_1",
  name: "Villa Rosa",
  city: "Nice",
  timezone: "Europe/Paris",
  color: "moss",
  kind: "str",
  areas: [],
  evidence_policy: "inherit",
  country: "FR",
  locale: "fr-FR",
  settings_override: {},
  client_org_id: null,
  owner_user_id: null,
};

const ME = {
  role: "manager",
  theme: "system",
  agent_sidebar_collapsed: false,
  employee: EMPLOYEE,
  manager_name: "Mina",
  today: "2026-05-05",
  now: "2026-05-05T10:00:00Z",
  user_id: "usr_1",
  agent_approval_mode: "confirm",
  current_workspace_id: "ws_1",
  available_workspaces: [],
  client_binding_org_ids: [],
  is_deployment_admin: false,
  is_deployment_owner: false,
};

const WORK_ROLE = {
  id: "wr_housekeeper",
  workspace_id: "ws_1",
  key: "housekeeper",
  name: "Housekeeper",
  description_md: "Turns guest rooms between stays.",
  default_settings_json: {},
  icon_name: "BrushCleaning",
  created_at: "2026-01-01T00:00:00Z",
  deleted_at: null,
} satisfies WorkRole;

function renderEmployees(routes: FetchRoute[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const fetchEnv = installFetchRouteHandlers([
    ...routes,
    { path: "/w/acme/api/v1/employees", respond: { body: [EMPLOYEE] } },
    { path: "/w/acme/api/v1/properties", respond: { body: [PROPERTY] } },
    { path: "/w/acme/api/v1/bookings", respond: { body: [] } },
    { path: "/w/acme/api/v1/me", respond: { body: ME } },
    {
      path: "/w/acme/api/v1/work_roles?limit=500",
      respond: { body: { data: [WORK_ROLE], next_cursor: null, has_more: false } },
    },
  ]);
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/w/acme/employees"]}>
        <EmployeesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, ...fetchEnv };
}

async function findWorkRoleCatalog(): Promise<HTMLElement> {
  const catalog = await screen.findByRole("region", { name: "Work roles" });
  await within(catalog).findByRole("table", { name: "Work role catalog" });
  return catalog;
}

function installDialogPolyfill(): void {
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  });
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

beforeEach(() => {
  installDialogPolyfill();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  registerWorkspaceSlugGetter(() => "acme");
  registerQueryKeyWorkspaceGetter(() => "acme");
});

afterEach(() => {
  cleanup();
  HTMLDialogElement.prototype.showModal = originalShowModal;
  HTMLDialogElement.prototype.close = originalClose;
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
});

describe("<EmployeesPage> invite action", () => {
  it("opens a real invite flow and sends a workspace worker invite", async () => {
    const { requests } = renderEmployees([
      {
        path: "/w/acme/api/v1/users/invite",
        method: "POST",
        respond: {
          status: 201,
          body: {
            invite_id: "inv_1",
            pending_email: "riley@example.test",
            user_id: "usr_riley",
            user_created: true,
          },
        },
      },
    ]);

    expect(await screen.findByRole("link", { name: "Mina Manager" })).toHaveAttribute(
      "href",
      "/w/acme/employee/emp_1",
    );
    fireEvent.click(await screen.findByRole("button", { name: "+ Invite employee" }));
    const dialog = screen.getByRole("dialog", { name: "Invite employee" });
    fireEvent.change(within(dialog).getByLabelText(/^Full name\b/), {
      target: { value: "Riley Chen" },
    });
    fireEvent.change(within(dialog).getByLabelText(/^Email\b/), {
      target: { value: "riley@example.test" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Send invite" }));

    expect(await within(dialog).findByRole("status")).toHaveTextContent(
      "Invite sent to riley@example.test",
    );
    const inviteRequest = requests.find((request) => request.method === "POST");
    expect(inviteRequest?.path).toBe("/w/acme/api/v1/users/invite");
    expect(inviteRequest?.body).toEqual({
      email: "riley@example.test",
      display_name: "Riley Chen",
      grants: [
        {
          scope_kind: "workspace",
          scope_id: "ws_1",
          grant_role: "worker",
        },
      ],
    });
  });

  it("shows a pending state while the invite mutation is in flight", async () => {
    const inviteResponse = deferred<{
      invite_id: string;
      pending_email: string;
      user_id: string | null;
      user_created: boolean;
    }>();
    renderEmployees([
      {
        path: "/w/acme/api/v1/users/invite",
        method: "POST",
        respond: () => inviteResponse.promise.then((body) => ({ status: 201, body })),
      },
    ]);

    fireEvent.click(await screen.findByRole("button", { name: "+ Invite employee" }));
    const dialog = screen.getByRole("dialog", { name: "Invite employee" });
    fireEvent.change(within(dialog).getByLabelText(/^Full name\b/), {
      target: { value: "Riley Chen" },
    });
    fireEvent.change(within(dialog).getByLabelText(/^Email\b/), {
      target: { value: "riley@example.test" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Send invite" }));

    expect(await within(dialog).findByRole("button", { name: "Sending..." })).toBeDisabled();

    inviteResponse.resolve({
      invite_id: "inv_1",
      pending_email: "riley@example.test",
      user_id: null,
      user_created: true,
    });
    expect(await within(dialog).findByRole("status")).toHaveTextContent("Invite sent");
  });

  it("keeps the dialog open with server validation errors", async () => {
    renderEmployees([
      {
        path: "/w/acme/api/v1/users/invite",
        method: "POST",
        respond: {
          status: 422,
          body: {
            type: "https://crewday.dev/errors/validation",
            title: "Validation error",
            status: 422,
            detail: "Request validation failed",
            errors: [{ loc: ["body", "email"], msg: "Enter a valid email address" }],
          },
        },
      },
    ]);

    fireEvent.click(await screen.findByRole("button", { name: "+ Invite employee" }));
    const dialog = screen.getByRole("dialog", { name: "Invite employee" });
    fireEvent.change(within(dialog).getByLabelText(/^Full name\b/), {
      target: { value: "Riley Chen" },
    });
    fireEvent.change(within(dialog).getByLabelText(/^Email\b/), {
      target: { value: "riley@example.test" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Send invite" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Email: Enter a valid email address",
    );
    expect(screen.getByRole("dialog", { name: "Invite employee" })).toBeInTheDocument();
  });

  it("keeps the dialog open with server errors", async () => {
    renderEmployees([
      {
        path: "/w/acme/api/v1/users/invite",
        method: "POST",
        respond: {
          status: 500,
          body: {
            type: "https://crewday.dev/errors/internal",
            title: "Internal server error",
            status: 500,
            detail: "Invite mailer failed",
          },
        },
      },
    ]);

    fireEvent.click(await screen.findByRole("button", { name: "+ Invite employee" }));
    const dialog = screen.getByRole("dialog", { name: "Invite employee" });
    fireEvent.change(within(dialog).getByLabelText(/^Full name\b/), {
      target: { value: "Riley Chen" },
    });
    fireEvent.change(within(dialog).getByLabelText(/^Email\b/), {
      target: { value: "riley@example.test" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Send invite" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Invite mailer failed",
    );
    expect(within(dialog).getByRole("button", { name: "Send invite" })).toBeEnabled();
  });
});

describe("<EmployeesPage> work-role catalog", () => {
  it("lists role catalog rows with edit and remove affordances", async () => {
    renderEmployees();

    const catalog = await findWorkRoleCatalog();
    expect(await within(catalog).findByText("Housekeeper")).toBeInTheDocument();
    expect(within(catalog).getByRole("table", { name: "Work role catalog" })).toBeInTheDocument();
    expect(within(catalog).getByText("housekeeper")).toBeInTheDocument();
    expect(within(catalog).queryByText("BrushCleaning")).not.toBeInTheDocument();
    expect(within(catalog).getByText("Turns guest rooms between stays.")).toBeInTheDocument();
    expect(catalog.querySelector(".work-role-row__mark svg")).toBeInTheDocument();
    expect(within(catalog).queryByRole("button", { name: "Add role" })).not.toBeInTheDocument();
    expect(within(catalog).getByLabelText("New work role")).toBeInTheDocument();
    expect(within(catalog).getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(within(catalog).getByRole("button", { name: "Remove" })).toBeInTheDocument();
  });

  it("uses a readable text mark when a role has no icon", async () => {
    renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles?limit=500",
        respond: {
          body: {
            data: [{ ...WORK_ROLE, icon_name: "", name: "Night porter", key: "night_porter" }],
            next_cursor: null,
            has_more: false,
          },
        },
      },
    ]);

    const catalog = await findWorkRoleCatalog();
    await within(catalog).findByText("Night porter");
    const row = within(catalog).getByLabelText("Night porter");
    expect(row?.querySelector(".work-role-row__mark")).toHaveTextContent("NP");
    expect(within(catalog).getByText("night_porter")).toBeInTheDocument();
  });

  it("falls back cleanly for unknown icon names without exposing the raw value", async () => {
    renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles?limit=500",
        respond: {
          body: {
            data: [{ ...WORK_ROLE, icon_name: "LegacyRoleIcon", name: "Legacy role", key: "legacy_role" }],
            next_cursor: null,
            has_more: false,
          },
        },
      },
    ]);

    const catalog = await findWorkRoleCatalog();
    await within(catalog).findByText("Legacy role");
    const row = within(catalog).getByLabelText("Legacy role");
    expect(row?.querySelector(".work-role-row__mark svg")).toBeInTheDocument();
    expect(within(catalog).queryByText("LegacyRoleIcon")).not.toBeInTheDocument();
    expect(within(catalog).getByText("legacy_role")).toBeInTheDocument();
  });

  it("shows loading, error, and default create-row empty states", async () => {
    const rolesResponse = deferred<{ data: unknown[]; next_cursor: null; has_more: false }>();
    const { unmount } = renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles?limit=500",
        respond: () => rolesResponse.promise.then((body) => ({ body })),
      },
    ]);

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    rolesResponse.resolve({ data: [], next_cursor: null, has_more: false });
    expect(await screen.findByRole("table", { name: "Work role catalog" })).toBeInTheDocument();
    expect(screen.getByLabelText("New work role")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add role" })).not.toBeInTheDocument();
    unmount();

    renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles?limit=500",
        respond: {
          status: 500,
          body: { type: "https://crewday.dev/errors/internal", title: "Internal server error" },
        },
      },
    ]);

    expect(await screen.findByRole("alert")).toHaveTextContent("Work roles could not be loaded.");
  });

  it("creates a work role and invalidates dependent queries", async () => {
    const roles = [WORK_ROLE];
    const { requests } = renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles?limit=500",
        respond: { body: { data: roles, next_cursor: null, has_more: false } },
      },
      {
        path: "/w/acme/api/v1/work_roles",
        method: "POST",
        respond: ({ body }) => {
          const role = {
            ...WORK_ROLE,
            ...(body as object),
            id: "wr_pool",
            workspace_id: "ws_1",
            created_at: "2026-01-02T00:00:00Z",
            deleted_at: null,
          };
          roles.push(role);
          return { status: 201, body: role };
        },
      },
    ]);

    const catalog = await findWorkRoleCatalog();
    fireEvent.change(within(catalog).getByLabelText("Name"), { target: { value: "Pool technician" } });
    fireEvent.change(within(catalog).getByLabelText("Key"), { target: { value: "pool_tech" } });
    expect(within(catalog).queryByLabelText(/^Icon name\b/)).not.toBeInTheDocument();
    fireEvent.click(within(catalog).getByRole("button", { name: "Icon: No icon. Edit icon" }));
    fireEvent.change(within(catalog).getByLabelText("Search icon choices"), { target: { value: "waves" } });
    fireEvent.click(within(catalog).getByRole("button", { name: "Select Waves icon" }));
    fireEvent.change(within(catalog).getByLabelText("Description"), {
      target: { value: "Handles weekly pool checks." },
    });
    fireEvent.click(within(catalog).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Pool technician")).toBeInTheDocument();
    expect(within(screen.getByLabelText("New work role")).getByLabelText("Name")).toHaveValue("");
    const createRequest = requests.find((request) => request.method === "POST");
    expect(createRequest?.path).toBe("/w/acme/api/v1/work_roles");
    expect(createRequest?.body).toEqual({
      name: "Pool technician",
      key: "pool_tech",
      description_md: "Handles weekly pool checks.",
      icon_name: "Waves",
      default_settings_json: {},
    });
    expect(requests.filter((request) => request.path === "/w/acme/api/v1/employees").length).toBeGreaterThan(1);
    expect(requests.filter((request) => request.path === "/w/acme/api/v1/work_roles?limit=500").length).toBeGreaterThan(1);
  });

  it("shows save-in-progress while creating a role", async () => {
    const saveResponse = deferred<never>();
    renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles",
        method: "POST",
        respond: () => saveResponse.promise,
      },
    ]);

    const catalog = await findWorkRoleCatalog();
    fireEvent.change(within(catalog).getByLabelText("Name"), { target: { value: "Housekeeper" } });
    fireEvent.change(within(catalog).getByLabelText("Key"), { target: { value: "housekeeper" } });
    fireEvent.click(within(catalog).getByRole("button", { name: "Save" }));

    expect(await within(catalog).findByText("Saving")).toBeInTheDocument();
    expect(within(catalog).getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("surfaces duplicate-key and validation errors from the API", async () => {
    renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles",
        method: "POST",
        respond: {
          status: 422,
          body: {
            type: "https://crewday.dev/errors/validation",
            title: "Validation error",
            detail: "Duplicate key",
            error: "work_role_key_conflict",
          },
        },
      },
    ]);

    const catalog = await findWorkRoleCatalog();
    fireEvent.change(within(catalog).getByLabelText("Name"), { target: { value: "Housekeeper" } });
    fireEvent.change(within(catalog).getByLabelText("Key"), { target: { value: "housekeeper" } });
    fireEvent.click(within(catalog).getByRole("button", { name: "Save" }));

    expect(await within(catalog).findByRole("alert")).toHaveTextContent(
      "That role key is already used",
    );
    expect(within(catalog).getByLabelText("Name")).toBeInTheDocument();
  });

  it("shows API field errors next to matching inline controls", async () => {
    renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles",
        method: "POST",
        respond: {
          status: 422,
          body: {
            type: "https://crewday.dev/errors/validation",
            title: "Validation error",
            detail: "Request validation failed",
            errors: [
              { loc: ["body", "name"], msg: "Name is too short" },
              { loc: ["body", "key"], msg: "Use lowercase letters, numbers, or underscores" },
              { loc: ["body", "icon_name"], msg: "Choose an icon from the catalog" },
              { loc: ["body", "description_md"], msg: "Description is too long" },
            ],
          },
        },
      },
    ]);

    const catalog = await findWorkRoleCatalog();
    const nameInput = within(catalog).getByLabelText("Name");
    const keyInput = within(catalog).getByLabelText("Key");
    const descriptionInput = within(catalog).getByLabelText("Description");
    fireEvent.change(nameInput, { target: { value: "N" } });
    fireEvent.change(keyInput, { target: { value: "Night Manager" } });
    fireEvent.change(descriptionInput, { target: { value: "x".repeat(20) } });
    fireEvent.click(within(catalog).getByRole("button", { name: "Save" }));

    expect(await within(catalog).findByText("Name is too short")).toBeInTheDocument();
    expect(within(catalog).getByText("Use lowercase letters, numbers, or underscores")).toBeInTheDocument();
    expect(within(catalog).getByText("Choose an icon from the catalog")).toBeInTheDocument();
    expect(within(catalog).getByText("Description is too long")).toBeInTheDocument();
    expect(nameInput).toHaveAttribute("aria-invalid", "true");
    expect(keyInput).toHaveAttribute("aria-invalid", "true");
    expect(descriptionInput).toHaveAttribute("aria-invalid", "true");
    expect(within(catalog).getByRole("button", { name: "Icon: No icon. Edit icon" })).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(within(catalog).getByRole("alert")).toHaveTextContent(
      "Could not save work role. Name is too short",
    );
  });

  it("surfaces permission errors when saving a role", async () => {
    renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles",
        method: "POST",
        respond: {
          status: 403,
          body: {
            type: "https://crewday.dev/errors/forbidden",
            title: "Forbidden",
            detail: "Forbidden",
          },
        },
      },
    ]);

    const catalog = await findWorkRoleCatalog();
    fireEvent.change(within(catalog).getByLabelText("Name"), { target: { value: "Driver" } });
    fireEvent.change(within(catalog).getByLabelText("Key"), { target: { value: "driver" } });
    fireEvent.click(within(catalog).getByRole("button", { name: "Save" }));

    expect(await within(catalog).findByRole("alert")).toHaveTextContent(
      "You do not have permission to manage work roles.",
    );
  });

  it("edits a work role", async () => {
    const roles: WorkRole[] = [{ ...WORK_ROLE }];
    const { requests } = renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles?limit=500",
        respond: { body: { data: roles, next_cursor: null, has_more: false } },
      },
      {
        path: "/w/acme/api/v1/work_roles/wr_housekeeper",
        method: "PATCH",
        respond: ({ body }) => {
          roles[0] = { ...roles[0]!, ...(body as Partial<WorkRole>) };
          return { body: roles[0] };
        },
      },
    ]);

    const catalog = await findWorkRoleCatalog();
    const roleRow = within(catalog).getByLabelText("Housekeeper");
    fireEvent.click(within(roleRow).getByRole("button", { name: "Edit" }));
    expect(within(roleRow).queryByText("Selected icon")).not.toBeInTheDocument();
    expect(within(roleRow).getByRole("button", { name: "Icon: Brush Cleaning. Edit icon" })).toBeInTheDocument();
    expect(within(roleRow).queryByText("Brush Cleaning")).not.toBeInTheDocument();
    const descriptionInput = within(roleRow).getByLabelText("Description");
    expect(descriptionInput.tagName).toBe("INPUT");
    expect(within(roleRow).queryByRole("textbox", { name: "Description" })?.tagName).toBe("INPUT");
    expect(roleRow.querySelector("textarea")).not.toBeInTheDocument();
    fireEvent.change(within(roleRow).getByLabelText("Name"), { target: { value: "Lead housekeeper" } });
    fireEvent.change(within(roleRow).getByLabelText("Key"), { target: { value: "lead_housekeeper" } });
    fireEvent.change(descriptionInput, { target: { value: "Runs quality checks." } });
    fireEvent.click(within(roleRow).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Lead housekeeper")).toBeInTheDocument();
    const patchRequest = requests.find((request) => request.method === "PATCH");
    expect(patchRequest?.body).toEqual({
      name: "Lead housekeeper",
      key: "lead_housekeeper",
      description_md: "Runs quality checks.",
      icon_name: "BrushCleaning",
    });
  });

  it("cancels an inline edit without saving", async () => {
    const { requests } = renderEmployees();

    const catalog = await findWorkRoleCatalog();
    const roleRow = within(catalog).getByLabelText("Housekeeper");
    fireEvent.click(within(roleRow).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(roleRow).getByLabelText("Name"), { target: { value: "Lead housekeeper" } });
    fireEvent.click(within(roleRow).getByRole("button", { name: "Cancel" }));

    expect(within(catalog).getByText("Housekeeper")).toBeInTheDocument();
    expect(within(catalog).queryByText("Lead housekeeper")).not.toBeInTheDocument();
    expect(requests.some((request) => request.method === "PATCH")).toBe(false);
  });

  it("cancels a new inline role without creating it", async () => {
    const { requests } = renderEmployees();

    const catalog = await findWorkRoleCatalog();
    expect(within(catalog).getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    fireEvent.change(within(catalog).getByLabelText("Name"), { target: { value: "Pool technician" } });
    fireEvent.click(within(catalog).getByRole("button", { name: "Cancel" }));

    expect(within(catalog).queryByDisplayValue("Pool technician")).not.toBeInTheDocument();
    expect(requests.some((request) => request.method === "POST")).toBe(false);
  });

  it("changes a work role icon before submitting", async () => {
    const roles: WorkRole[] = [{ ...WORK_ROLE }];
    const { requests } = renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles?limit=500",
        respond: { body: { data: roles, next_cursor: null, has_more: false } },
      },
      {
        path: "/w/acme/api/v1/work_roles/wr_housekeeper",
        method: "PATCH",
        respond: ({ body }) => {
          roles[0] = { ...roles[0]!, ...(body as Partial<WorkRole>) };
          return { body: roles[0] };
        },
      },
    ]);

    const catalog = await findWorkRoleCatalog();
    const roleRow = within(catalog).getByLabelText("Housekeeper");
    fireEvent.click(within(roleRow).getByRole("button", { name: "Edit" }));
    fireEvent.click(within(roleRow).getByRole("button", { name: "Icon: Brush Cleaning. Edit icon" }));
    expect(within(roleRow).getByText("Brush Cleaning")).toBeInTheDocument();
    fireEvent.change(within(roleRow).getByLabelText("Search icon choices"), { target: { value: "waves" } });
    fireEvent.click(within(roleRow).getByRole("button", { name: "Select Waves icon" }));
    fireEvent.click(within(roleRow).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Housekeeper")).toBeInTheDocument();
    const patchRequest = requests.find((request) => request.method === "PATCH");
    expect(patchRequest?.body).toEqual({
      name: "Housekeeper",
      key: "housekeeper",
      description_md: "Turns guest rooms between stays.",
      icon_name: "Waves",
    });
  });

  it("clears an optional work role icon before submitting", async () => {
    const roles: WorkRole[] = [{ ...WORK_ROLE }];
    const { requests } = renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles?limit=500",
        respond: { body: { data: roles, next_cursor: null, has_more: false } },
      },
      {
        path: "/w/acme/api/v1/work_roles/wr_housekeeper",
        method: "PATCH",
        respond: ({ body }) => {
          roles[0] = { ...roles[0]!, ...(body as Partial<WorkRole>) };
          return { body: roles[0] };
        },
      },
    ]);

    const catalog = await findWorkRoleCatalog();
    const roleRow = within(catalog).getByLabelText("Housekeeper");
    fireEvent.click(within(roleRow).getByRole("button", { name: "Edit" }));
    fireEvent.click(within(roleRow).getByRole("button", { name: "Icon: Brush Cleaning. Edit icon" }));
    fireEvent.click(within(roleRow).getByRole("button", { name: "No icon" }));
    fireEvent.click(within(roleRow).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Housekeeper")).toBeInTheDocument();
    const patchRequest = requests.find((request) => request.method === "PATCH");
    expect(patchRequest?.body).toEqual({
      name: "Housekeeper",
      key: "housekeeper",
      description_md: "Turns guest rooms between stays.",
      icon_name: "",
    });
  });

  it("drops stale unknown work role icon names on submit", async () => {
    const legacyRole = { ...WORK_ROLE, icon_name: "LegacyRoleIcon", name: "Legacy role", key: "legacy_role" };
    const roles: WorkRole[] = [legacyRole];
    const { requests } = renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles?limit=500",
        respond: { body: { data: roles, next_cursor: null, has_more: false } },
      },
      {
        path: "/w/acme/api/v1/work_roles/wr_housekeeper",
        method: "PATCH",
        respond: ({ body }) => {
          roles[0] = { ...roles[0]!, ...(body as Partial<WorkRole>) };
          return { body: roles[0] };
        },
      },
    ]);

    const catalog = await findWorkRoleCatalog();
    const roleRow = within(catalog).getByLabelText("Legacy role");
    fireEvent.click(within(roleRow).getByRole("button", { name: "Edit" }));
    expect(within(roleRow).getByRole("button", { name: "Icon: Unknown icon. Edit icon" })).toBeInTheDocument();
    fireEvent.click(within(roleRow).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Legacy role")).toBeInTheDocument();
    const patchRequest = requests.find((request) => request.method === "PATCH");
    expect(patchRequest?.body).toEqual({
      name: "Legacy role",
      key: "legacy_role",
      description_md: "Turns guest rooms between stays.",
      icon_name: "",
    });
  });

  it("confirms soft-retire behavior before removing a role", async () => {
    const roles = [{ ...WORK_ROLE }];
    const { requests } = renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles?limit=500",
        respond: { body: { data: roles, next_cursor: null, has_more: false } },
      },
      {
        path: "/w/acme/api/v1/work_roles/wr_housekeeper",
        method: "DELETE",
        respond: () => {
          roles.length = 0;
          return { status: 204, body: null };
        },
      },
    ]);

    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
    const dialog = screen.getByRole("alertdialog", { name: "Remove work role?" });
    expect(dialog).toHaveTextContent("soft-retires Housekeeper");
    expect(dialog).toHaveTextContent("future employee assignment lists");
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove role" }));

    expect(await screen.findByLabelText("New work role")).toBeInTheDocument();
    expect(requests.some((request) => request.method === "DELETE")).toBe(true);
  });

  it("shows row saving state while removing a role", async () => {
    const deleteResponse = deferred<never>();
    renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles/wr_housekeeper",
        method: "DELETE",
        respond: () => deleteResponse.promise,
      },
    ]);

    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
    const dialog = screen.getByRole("alertdialog", { name: "Remove work role?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove role" }));

    expect(await screen.findByText("Saving")).toBeInTheDocument();
  });

  it("surfaces remove permission errors without closing confirmation", async () => {
    renderEmployees([
      {
        path: "/w/acme/api/v1/work_roles/wr_housekeeper",
        method: "DELETE",
        respond: {
          status: 403,
          body: {
            type: "https://crewday.dev/errors/forbidden",
            title: "Forbidden",
            detail: "Forbidden",
          },
        },
      },
    ]);

    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
    const dialog = screen.getByRole("alertdialog", { name: "Remove work role?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove role" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "You do not have permission to manage work roles.",
    );
    expect(screen.getByRole("alertdialog", { name: "Remove work role?" })).toBeInTheDocument();
  });
});
