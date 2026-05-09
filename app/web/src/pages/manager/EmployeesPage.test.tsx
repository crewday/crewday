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

function renderEmployees(routes: FetchRoute[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const fetchEnv = installFetchRouteHandlers([
    { path: "/w/acme/api/v1/employees", respond: { body: [EMPLOYEE] } },
    { path: "/w/acme/api/v1/properties", respond: { body: [PROPERTY] } },
    { path: "/w/acme/api/v1/bookings", respond: { body: [] } },
    { path: "/w/acme/api/v1/me", respond: { body: ME } },
    ...routes,
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
    fireEvent.change(within(dialog).getByLabelText("Full name"), {
      target: { value: "Riley Chen" },
    });
    fireEvent.change(within(dialog).getByLabelText("Email"), {
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
    fireEvent.change(within(dialog).getByLabelText("Full name"), {
      target: { value: "Riley Chen" },
    });
    fireEvent.change(within(dialog).getByLabelText("Email"), {
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
    fireEvent.change(within(dialog).getByLabelText("Full name"), {
      target: { value: "Riley Chen" },
    });
    fireEvent.change(within(dialog).getByLabelText("Email"), {
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
    fireEvent.change(within(dialog).getByLabelText("Full name"), {
      target: { value: "Riley Chen" },
    });
    fireEvent.change(within(dialog).getByLabelText("Email"), {
      target: { value: "riley@example.test" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Send invite" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Invite mailer failed",
    );
    expect(within(dialog).getByRole("button", { name: "Send invite" })).toBeEnabled();
  });
});
