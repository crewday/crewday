import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import PreviewShell from "@/layouts/PreviewShell";
import EmployeeLayout from "@/layouts/EmployeeLayout";
import ManagerLayout from "@/layouts/ManagerLayout";
import AdminLayout from "@/layouts/AdminLayout";
import PublicLayout from "@/layouts/PublicLayout";
import { useRole } from "@/context/RoleContext";

import TodayPage from "@/pages/employee/TodayPage";
import SchedulePage from "@/pages/employee/SchedulePage";
import TaskDetailPage from "@/pages/employee/TaskDetailPage";
import ChatPage from "@/pages/employee/ChatPage";
import MyExpensesPage from "@/pages/employee/MyExpensesPage";
import MePage from "@/pages/employee/MePage";
import HistoryPage from "@/pages/employee/HistoryPage";
import IssueNewPage from "@/pages/employee/IssueNewPage";
import EmployeeAssetPage from "@/pages/employee/EmployeeAssetPage";
import AssetScanPage from "@/pages/employee/AssetScanPage";

import DashboardPage from "@/pages/manager/DashboardPage";
import PropertiesPage from "@/pages/manager/PropertiesPage";
import PropertyDetailPage from "@/pages/manager/PropertyDetailPage";
import PropertyClosuresPage from "@/pages/manager/PropertyClosuresPage";
import EmployeesPage from "@/pages/manager/EmployeesPage";
import EmployeeDetailPage from "@/pages/manager/EmployeeDetailPage";
import EmployeeLeavesPage from "@/pages/manager/EmployeeLeavesPage";
import LeavesInboxPage from "@/pages/manager/LeavesInboxPage";
import StaysPage from "@/pages/manager/StaysPage";
import ApprovalsPage from "@/pages/manager/ApprovalsPage";
import ExpensesApprovalsPage from "@/pages/manager/ExpensesApprovalsPage";
import TemplatesPage from "@/pages/manager/TemplatesPage";
import SchedulesPage from "@/pages/manager/SchedulesPage";
import InstructionsPage from "@/pages/manager/InstructionsPage";
import InstructionDetailPage from "@/pages/manager/InstructionDetailPage";
import InventoryPage from "@/pages/manager/InventoryPage";
import AssetsPage from "@/pages/manager/AssetsPage";
import AssetDetailPage from "@/pages/manager/AssetDetailPage";
import AssetTypesPage from "@/pages/manager/AssetTypesPage";
import DocumentsPage from "@/pages/manager/DocumentsPage";
import PayPage from "@/pages/manager/PayPage";
import AuditPage from "@/pages/manager/AuditPage";
import OrganizationsPage from "@/pages/manager/OrganizationsPage";
import PermissionsPage from "@/pages/manager/PermissionsPage";
import WebhooksPage from "@/pages/manager/WebhooksPage";
import ApiTokensPage from "@/pages/manager/ApiTokensPage";
import ChatChannelsPage from "@/pages/manager/ChatChannelsPage";
import SettingsPage from "@/pages/manager/SettingsPage";

import AdminDashboardPage from "@/pages/admin/DashboardPage";
import AdminChatGatewayPage from "@/pages/admin/ChatGatewayPage";
import AdminLlmPage from "@/pages/admin/LlmPage";
import AdminAgentDocsPage from "@/pages/admin/AgentDocsPage";
import AdminUsagePage from "@/pages/admin/UsagePage";
import AdminWorkspacesPage from "@/pages/admin/WorkspacesPage";
import AdminSignupsPage from "@/pages/admin/SignupsPage";
import AdminSettingsPage from "@/pages/admin/SettingsPage";
import AdminAdminsPage from "@/pages/admin/AdminsPage";
import AdminAuditPage from "@/pages/admin/AuditPage";

import LoginPage from "@/pages/public/LoginPage";
import RecoverPage from "@/pages/public/RecoverPage";
import EnrollPage from "@/pages/public/EnrollPage";
import AcceptPage from "@/pages/public/AcceptPage";
import GuestPage from "@/pages/public/GuestPage";
import SignupPage from "@/pages/public/SignupPage";
import SignupVerifyPage from "@/pages/public/SignupVerifyPage";
import SignupEnrollPage from "@/pages/public/SignupEnrollPage";

import SchedulerPage from "@/pages/SchedulerPage";

import ClientLayout from "@/layouts/ClientLayout";
import ClientPortfolioPage from "@/pages/client/PortfolioPage";
import ClientBillableHoursPage from "@/pages/client/BillableHoursPage";
import ClientQuotesPage from "@/pages/client/QuotesPage";
import ClientInvoicesPage from "@/pages/client/InvoicesPage";

import { RequireAuth, RequirePermission, WorkspaceGate } from "@/auth";

const STYLEGUIDE_ENABLED =
  import.meta.env.DEV ||
  import.meta.env.VITE_CREWDAY_STAGING === "1" ||
  import.meta.env.VITE_CREWDAY_STAGING === "true";
const StyleguidePage = STYLEGUIDE_ENABLED
  ? lazy(() => import("@/pages/StyleguidePage"))
  : null;

function RoleHome() {
  const { role } = useRole();
  const target =
    role === "employee" ? "/today"
    : role === "client" ? "/portfolio"
    : "/dashboard";
  return <Navigate to={target} replace />;
}

// §14 — Shared routes (/today, /schedule, /my/expenses, etc.) render
// under the viewer's role-appropriate shell. Manager / Employee /
// Client each get their own layout; only `/me` is currently shared by
// all three (every persona has a profile screen).
function Shell() {
  const { role } = useRole();
  if (role === "manager") return <ManagerLayout />;
  if (role === "client") return <ClientLayout />;
  return <EmployeeLayout />;
}

export default function App() {
  const { role } = useRole();

  return (
    <Routes>
      <Route element={<PreviewShell />}>
        {/* Public routes — login, recover, accept, guest. Rendered
            without `<RequireAuth>` / `<WorkspaceGate>` so an
            anonymous user can sign in, redeem an invite, or land on
            a guest share link without being bounced. */}
        <Route element={<PublicLayout />}>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/recover" element={<RecoverPage />} />
          <Route path="/recover/enroll" element={<EnrollPage />} />
          <Route path="/signup" element={<SignupPage />} />
          <Route path="/signup/verify" element={<SignupVerifyPage />} />
          <Route path="/signup/enroll" element={<SignupEnrollPage />} />
          {/* Generic magic-link URL emitted by the default mailer
              template (`app/mail/templates/auth/magic_link.*.j2`).
              The mailer
              uses this shape for ALL purposes that go through the
              shared template — signup_verify, email_change_confirm,
              email_change_revert, grant_invite, workspace_verify_ownership.
              Today only signup_verify has an SPA handler; the other
              purposes will surface a `purpose_mismatch` 400 from
              `/api/v1/signup/verify` and the verify page renders the
              standard "expired / invalid" copy. Recovery is the
              exception — it emits its own `/recover/enroll?token=…`
              URL and never lands on this route. */}
          <Route path="/auth/magic/:token" element={<SignupVerifyPage />} />
          <Route path="/accept/:token" element={<AcceptPage />} />
          <Route path="/guest/:token" element={<GuestPage />} />
          <Route path="/w/:slug/guest/:token" element={<GuestPage />} />
        </Route>

        {/* Styleguide — public dev surface; render without auth so
            designers can land on it directly. Same posture as
            `mocks/web/`. */}
        {StyleguidePage ? (
          <Route
            path="/styleguide"
            element={
              <Suspense fallback={null}>
                <StyleguidePage />
              </Suspense>
            }
          />
        ) : null}

        {/* Everything else sits behind the auth gate.
            `<RequireAuth>` resolves the session (loading | login |
            authenticated). Once authenticated, `<WorkspaceGate>`
            holds the protected tree until a workspace slug is
            picked (auto-adopted for single-workspace users). */}
        <Route element={<RequireAuth />}>
          <Route element={<WorkspaceGate />}>
            <Route path="/" element={<RoleHome />} />

            {/* Shared routes — any role. Shell picks the right layout. */}
            <Route element={<Shell />}>
              <Route path="/today" element={<TodayPage />} />
              <Route path="/schedule" element={<SchedulePage />} />
              {/* Legacy URLs — spec §14 collapses Week and /me/schedule
                  into /schedule. Keep redirects so deep-links, CLI
                  output, and agent tool refs continue to land on the
                  right page. */}
              <Route path="/week" element={<Navigate to="/schedule" replace />} />
              <Route path="/me/schedule" element={<Navigate to="/schedule" replace />} />
              <Route path="/task/:tid" element={<TaskDetailPage />} />
              <Route path="/my/expenses" element={<MyExpensesPage />} />
              <Route path="/me" element={<MePage />} />
              <Route path="/scheduler" element={<SchedulerPage />} />
              <Route path="/w/:slug/scheduler" element={<SchedulerPage />} />
              {/* Legacy /bookings and /shifts URLs — spec §14 collapses
                  the standalone bookings page into the /schedule day
                  drawer (§09 bookings render alongside rota / tasks /
                  leaves). Redirect for bookmarks and agent tool refs. */}
              <Route path="/bookings" element={<Navigate to="/schedule" replace />} />
              <Route path="/shifts" element={<Navigate to="/schedule" replace />} />
              <Route path="/history" element={<HistoryPage />} />
              <Route path="/issues/new" element={<IssueNewPage />} />
              {role === "manager" ? null : (
                <Route path="/asset/:aid" element={<EmployeeAssetPage />} />
              )}
            </Route>

            {/* Worker-only surfaces. /chat is the worker mobile full-
                screen chat entry; on desktop both shells use
                AgentSidebar instead. */}
            <Route element={<EmployeeLayout />}>
              <Route path="/chat" element={<ChatPage />} />
              <Route path="/asset/scan" element={<AssetScanPage />} />
              <Route path="/asset/scan/:token" element={<AssetScanPage />} />
            </Route>

            <Route element={<RequirePermission actionKey="approvals.read" />}>
              <Route element={<ManagerLayout />}>
                <Route path="/approvals" element={<ApprovalsPage />} />
              </Route>
            </Route>

            <Route element={<RequirePermission actionKey="employees.read" />}>
              <Route element={<ManagerLayout />}>
                <Route path="/dashboard" element={<DashboardPage />} />
              </Route>
            </Route>

            <Route element={<RequirePermission actionKey="leaves.view_others" />}>
              <Route element={<ManagerLayout />}>
                <Route path="/leaves" element={<LeavesInboxPage />} />
              </Route>
              <Route element={<RequirePermission actionKey="employees.read" />}>
                <Route element={<ManagerLayout />}>
                  <Route path="/employee/:eid/leaves" element={<EmployeeLeavesPage />} />
                  <Route path="/user/:eid/leaves" element={<EmployeeLeavesPage />} />
                  <Route path="/w/:slug/employee/:eid/leaves" element={<EmployeeLeavesPage />} />
                  <Route path="/w/:slug/user/:eid/leaves" element={<EmployeeLeavesPage />} />
                </Route>
              </Route>
            </Route>

            <Route element={<RequirePermission actionKey="scope.edit_settings" />}>
              <Route element={<ManagerLayout />}>
                <Route path="/settings" element={<SettingsPage />} />
                <Route path="/webhooks" element={<WebhooksPage />} />
              </Route>
            </Route>

            <Route element={<RequirePermission actionKey="chat_gateway.read" />}>
              <Route element={<ManagerLayout />}>
                <Route path="/chat/channels" element={<ChatChannelsPage />} />
                <Route path="/w/:slug/chat/channels" element={<ChatChannelsPage />} />
              </Route>
            </Route>

            <Route element={<RequirePermission actionKey="payroll.view_other" />}>
              <Route element={<ManagerLayout />}>
                <Route path="/pay" element={<PayPage />} />
              </Route>
            </Route>

            <Route element={<RequirePermission actionKey="stays.read" />}>
              <Route element={<ManagerLayout />}>
                <Route path="/stays" element={<StaysPage />} />
              </Route>
            </Route>

            <Route element={<RequirePermission actionKey="instructions.edit" />}>
              <Route element={<ManagerLayout />}>
                <Route path="/instructions" element={<InstructionsPage />} />
                <Route path="/instructions/:iid" element={<InstructionDetailPage />} />
              </Route>
            </Route>

            <Route element={<RequirePermission actionKey="properties.read" />}>
              <Route element={<ManagerLayout />}>
                <Route path="/properties" element={<PropertiesPage />} />
                <Route path="/property/:pid" element={<PropertyDetailPage />} />
                <Route path="/property/:pid/closures" element={<PropertyClosuresPage />} />
              </Route>
            </Route>

            <Route element={<RequirePermission actionKey="assets.manage_documents" />}>
              <Route element={<ManagerLayout />}>
                <Route path="/documents" element={<DocumentsPage />} />
              </Route>
            </Route>

            {role === "manager" ? (
              <Route element={<RequirePermission actionKey="scope.view" />}>
                <Route element={<ManagerLayout />}>
                  <Route path="/asset/:aid" element={<AssetDetailPage />} />
                  <Route path="/w/:slug/asset/:aid" element={<AssetDetailPage />} />
                </Route>
              </Route>
            ) : null}

            <Route element={<RequirePermission actionKey="scope.view" />}>
              <Route element={<ManagerLayout />}>
                <Route path="/assets" element={<AssetsPage />} />
                <Route path="/inventory" element={<InventoryPage />} />
                <Route path="/asset_types" element={<AssetTypesPage />} />
                <Route path="/w/:slug/asset_types" element={<AssetTypesPage />} />
                <Route path="/organizations" element={<OrganizationsPage />} />
                <Route path="/w/:slug/organizations" element={<OrganizationsPage />} />
              </Route>
            </Route>

            <Route element={<RequirePermission actionKey="employees.read" />}>
              <Route element={<ManagerLayout />}>
                <Route path="/employees" element={<EmployeesPage />} />
                <Route path="/employee/:eid" element={<EmployeeDetailPage />} />
              </Route>
            </Route>

            <Route element={<ManagerLayout />}>
              <Route
                path="/expenses"
                element={
                  role === "manager" ? <ExpensesApprovalsPage /> : <Navigate to="/my/expenses" replace />
                }
              />
              <Route path="/templates" element={<TemplatesPage />} />
              <Route path="/schedules" element={<SchedulesPage />} />
            </Route>

            <Route element={<RequirePermission actionKey="permissions.edit_rules" />}>
              <Route element={<ManagerLayout />}>
                <Route path="/permissions" element={<PermissionsPage />} />
              </Route>
            </Route>

            <Route element={<RequirePermission actionKey="api_tokens.manage" />}>
              <Route element={<ManagerLayout />}>
                <Route path="/tokens" element={<ApiTokensPage />} />
              </Route>
            </Route>

            <Route element={<RequirePermission actionKey="audit_log.view" />}>
              <Route element={<ManagerLayout />}>
                <Route path="/audit" element={<AuditPage />} />
              </Route>
            </Route>

            <Route element={<ClientLayout />}>
              <Route path="/portfolio" element={<ClientPortfolioPage />} />
              <Route path="/billable_hours" element={<ClientBillableHoursPage />} />
              <Route path="/quotes" element={<ClientQuotesPage />} />
              <Route path="/invoices" element={<ClientInvoicesPage />} />
            </Route>
          </Route>

          {/* /admin — bare-host deployment admin shell (§14 "Admin
              shell"). Sits inside `<RequireAuth>` (the deployment
              admin must be signed in) but **outside** `<WorkspaceGate>`:
              admin is a deployment-scope surface and intentionally
              has no workspace slug. */}
          <Route element={<AdminLayout />}>
            <Route path="/admin" element={<Navigate to="/admin/dashboard" replace />} />
            <Route path="/admin/dashboard" element={<AdminDashboardPage />} />
            <Route path="/admin/chat-gateway" element={<AdminChatGatewayPage />} />
            <Route path="/admin/llm" element={<AdminLlmPage />} />
            <Route path="/admin/agent-docs" element={<AdminAgentDocsPage />} />
            <Route path="/admin/usage" element={<AdminUsagePage />} />
            <Route path="/admin/workspaces" element={<AdminWorkspacesPage />} />
            <Route path="/admin/signups" element={<AdminSignupsPage />} />
            <Route path="/admin/signup" element={<Navigate to="/admin/settings#signup" replace />} />
            <Route path="/admin/settings" element={<AdminSettingsPage />} />
            <Route path="/admin/admins" element={<AdminAdminsPage />} />
            <Route path="/admin/audit" element={<AdminAuditPage />} />
          </Route>
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
