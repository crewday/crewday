import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import { installFetchRouteHandlers } from "@/test/helpers";
import type { ApprovalRequest, DashboardPayload, Employee, Issue, Leave, Property, Task } from "@/types/api";
import DashboardPage from "./DashboardPage";

const EMPLOYEE = {
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
  workspaces: ["ws_1"],
  villas: [],
  language: "en",
  weekly_availability: {},
  evidence_policy: "inherit",
  preferred_locale: null,
  settings_override: {},
} satisfies Employee;

const PROPERTY = {
  id: "prop_1",
  name: "Villa Azul",
  city: "Lagos",
  timezone: "Europe/Lisbon",
  color: "moss",
  kind: "vacation",
  areas: ["Kitchen", "Pool"],
  evidence_policy: "inherit",
  country: "PT",
  locale: "en-GB",
  settings_override: {},
  client_org_id: null,
  owner_user_id: null,
} satisfies Property;

const TASK = {
  id: "task_1",
  title: "Reset pool towels",
  property_id: PROPERTY.id,
  area: "Pool",
  assignee_id: EMPLOYEE.id,
  scheduled_start: "2026-04-29T09:00:00Z",
  estimated_minutes: 30,
  priority: "normal",
  status: "pending",
  checklist: [],
  photo_evidence: "optional",
  evidence_policy: "inherit",
  instructions_ids: [],
  template_id: null,
  schedule_id: null,
  turnover_bundle_id: null,
  asset_id: null,
  settings_override: {},
  assigned_user_id: "usr_worker_1",
  workspace_id: "ws_1",
  created_by: "usr_1",
  is_personal: false,
} satisfies Task;

const APPROVAL = {
  id: "approval_1",
  agent: "ops",
  action: "Message staff",
  target: "All housekeepers",
  reason: "Storm watch broadcast needs approval.",
  requested_at: "2026-04-29T08:30:00Z",
  risk: "medium",
  diff: [],
  gate_source: "workspace_configurable",
  gate_destination: "desk",
  inline_channel: "desk_only",
  card_summary: "Broadcast storm watch",
  card_fields: [],
  for_user_id: null,
  resolved_user_mode: null,
} satisfies ApprovalRequest;

const ISSUE = {
  id: "issue_1",
  reported_by: EMPLOYEE.id,
  property_id: PROPERTY.id,
  area: "Kitchen",
  severity: "high",
  category: "broken",
  title: "Dishwasher leaking",
  body: "Water under the appliance.",
  reported_at: "2026-04-29T08:00:00Z",
  status: "open",
} satisfies Issue;

const LEAVE = {
  id: "leave_1",
  employee_id: EMPLOYEE.id,
  starts_on: "2026-05-03",
  ends_on: "2026-05-05",
  category: "vacation",
  note: "Family trip",
  approved_at: null,
} satisfies Leave;

function emptyDashboard(): DashboardPayload {
  return {
    on_booking: [],
    by_status: { completed: [], in_progress: [], pending: [] },
    pending_approvals: [],
    pending_expenses: [],
    pending_leaves: [],
    open_issues: [],
    stays_today: [],
    properties: [],
    employees: [EMPLOYEE],
  };
}

function installFetch(dashboard: DashboardPayload = emptyDashboard()) {
  return installFetchRouteHandlers([
    {
      path: "/api/v1/auth/me",
      respond: {
        body: {
          user_id: "usr_1",
          display_name: "Mina",
          email: "mina@example.com",
          available_workspaces: [],
          current_workspace_id: "ws_1",
        },
      },
    },
    {
      path: "/api/v1/me/workspaces",
      respond: {
        body: [
          {
            workspace_id: "ws_1",
            slug: "acme",
            name: "Acme",
            current_role: "manager",
            last_seen_at: null,
            settings_override: {},
          },
        ],
      },
    },
    {
      path: "/w/acme/api/v1/me",
      respond: {
        body: {
          role: "manager",
          theme: "system",
          agent_sidebar_collapsed: false,
          employee: EMPLOYEE,
          manager_name: "Mina Patel",
          today: "2026-04-29",
          now: "2026-04-29T10:00:00Z",
          user_id: "usr_1",
          agent_approval_mode: "ask",
          current_workspace_id: "ws_1",
          available_workspaces: [],
          client_binding_org_ids: [],
          is_deployment_admin: false,
          is_deployment_owner: false,
        },
      },
    },
    {
      path: "/w/acme/api/v1/dashboard",
      respond: {
        body: dashboard,
      },
    },
    { path: "/w/acme/api/v1/properties", respond: { body: [] } },
    {
      path: "/w/acme/api/v1/messaging/broadcast/recipients",
      respond: {
        body: {
          data: [
            {
              user_id: "usr_worker_1",
              display_name: "Maya Santos",
              email: "maya@example.com",
            },
            {
              user_id: "usr_worker_2",
              display_name: "Ivo Costa",
              email: "ivo@example.com",
            },
          ],
          total: 2,
        },
      },
    },
    {
      path: "/w/acme/api/v1/messaging/broadcast",
      method: "POST",
      respond: {
        body: {
          status: "pending_approval",
          recipient_count: 2,
          notification_ids: [],
          approval_request_id: "appr_1",
          expires_at: "2026-05-12T12:00:00Z",
        },
      },
    },
  ]);
}

function Harness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/w/acme/dashboard"]}>
        <WorkspaceProvider>
          <DashboardPage />
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function panelNamed(name: string): Promise<HTMLElement> {
  const panel = (await screen.findByRole("heading", { name })).closest(".panel");
  if (!(panel instanceof HTMLElement)) {
    throw new Error(`Panel not found: ${name}`);
  }
  return panel;
}

beforeEach(() => {
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("acme");
  HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  });
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<DashboardPage>", () => {
  it("renders shared empty states for every empty dashboard pane", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      expect(within(await panelNamed("Today's tasks")).getByText("No tasks scheduled for today.")).toBeInTheDocument();
      expect(within(await panelNamed("Agent approvals")).getByText("No agent approvals waiting.")).toBeInTheDocument();
      expect(within(await panelNamed("Open issues")).getByText("No open issues.")).toBeInTheDocument();
      expect(within(await panelNamed("Pending leaves")).getByText("No pending leave requests.")).toBeInTheDocument();
      expect(document.querySelectorAll(".empty-state.empty-state--quiet")).toHaveLength(4);
      expect(document.querySelector("li.empty-state")).not.toBeInTheDocument();
      expect(within(await panelNamed("Today's tasks")).queryByRole("table")).not.toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("keeps non-empty dashboard rows and actions", async () => {
    const fake = installFetch({
      ...emptyDashboard(),
      by_status: { completed: [], in_progress: [], pending: [TASK] },
      pending_approvals: [APPROVAL],
      pending_leaves: [LEAVE],
      open_issues: [ISSUE],
      properties: [PROPERTY],
      employees: [EMPLOYEE],
    });
    try {
      render(<Harness />);

      const tasks = await panelNamed("Today's tasks");
      expect(await within(tasks).findByRole("table")).toBeInTheDocument();
      expect(within(tasks).getByText("Reset pool towels")).toBeInTheDocument();
      expect(within(tasks).getByText("Villa Azul")).toBeInTheDocument();

      const approvals = await panelNamed("Agent approvals");
      expect(within(approvals).getByText(/Message staff/)).toBeInTheDocument();
      expect(within(approvals).getByRole("button", { name: "Approve" })).toBeInTheDocument();
      expect(within(approvals).getByRole("button", { name: "Reject" })).toBeInTheDocument();

      const issues = await panelNamed("Open issues");
      expect(within(issues).getByText("Dishwasher leaking")).toBeInTheDocument();
      expect(within(issues).getByText("high")).toBeInTheDocument();

      const leaves = await panelNamed("Pending leaves");
      expect(within(leaves).getByText("Maya Santos")).toBeInTheDocument();
      expect(within(leaves).getByRole("button", { name: "Approve" })).toBeInTheDocument();
      expect(within(leaves).getByRole("button", { name: "Reject" })).toBeInTheDocument();
      expect(screen.queryByText("No tasks scheduled for today.")).not.toBeInTheDocument();
    } finally {
      fake.restore();
    }
  });

  it("opens the real new-task and broadcast dialogs", async () => {
    const fake = installFetch();
    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "+ New task" }));
      expect(screen.getByRole("link", { name: /By property/ })).toHaveAttribute("href", "/w/acme/properties");
      const allLinks = screen.getAllByRole("link", { name: /All/ });
      expect(allLinks.map((link) => link.getAttribute("href"))).toEqual([
        "/w/acme/approvals",
        "/w/acme/leaves",
      ]);
      expect(await screen.findByRole("heading", { name: "New task" })).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "More actions" }));
      fireEvent.click(screen.getByRole("menuitem", { name: /Broadcast message/ }));

      const dialog = await screen.findByRole("dialog", { name: "Broadcast message" });
      expect(await within(dialog).findByText(/2 recipients/)).toBeInTheDocument();
      fireEvent.change(within(dialog).getByLabelText("Subject"), {
        target: { value: "Storm watch" },
      });
      fireEvent.change(within(dialog).getByLabelText("Body"), {
        target: { value: "Bring patio furniture inside before 16:00." },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Request approval" }));

      await waitFor(() => {
        expect(within(dialog).getByRole("status")).toHaveTextContent(
          "Queued for approval before sending to 2 recipients.",
        );
      });
      expect(screen.queryByText("Broadcast messaging is not implemented yet.")).not.toBeInTheDocument();
      expect(fake.requests).toContainEqual(
        expect.objectContaining({
          method: "POST",
          path: "/w/acme/api/v1/messaging/broadcast",
          body: expect.objectContaining({
            target: "all_staff",
            confirmed_recipient_count: 2,
            subject: "Storm watch",
          }),
        }),
      );
    } finally {
      fake.restore();
    }
  });
});
