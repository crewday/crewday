import { type FormEvent, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarOff, ClipboardList, ReceiptText } from "lucide-react";
import { useParams } from "react-router-dom";
import { ApiError, fetchJson, openApiDownload } from "@/lib/api";
import { fetchAllList } from "@/lib/fetchAllList";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import {
  type PayrollPayslipListPayload,
  mapPayrollPayslip,
} from "@/lib/payrollPayslips";
import { ForbiddenPanel } from "@/auth/RequirePermission";
import DeskPage from "@/components/DeskPage";
import DateTime from "@/components/DateTime";
import FormModal from "@/components/FormModal";
import PageTabs, { type PageTab } from "@/components/PageTabs";
import { Chip, EmptyState, Loading } from "@/components/common";
import { fmtDayMonYear, inclusiveDays } from "./leaveDisplay";
import type {
  Employee,
  EntitySettingsPayload,
  Expense,
  ExpenseStatus,
  Leave,
  PaySlip,
  Property,
  SettingDefinition,
  Task,
  TaskStatus,
  WorkRole,
} from "@/types/api";

interface EmployeeDetail {
  subject: Employee;
  subject_tasks: Task[];
  subject_expenses: Expense[];
  subject_leaves: Leave[];
  subject_payslips: PaySlip[];
}

interface LeavesPayload {
  subject: Employee;
  leaves: Leave[];
}

interface UserWorkRole {
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

interface RoleEditorPayload {
  employeeId: string;
  startedOn: string;
  addRoleIds: string[];
  removeLinkIds: string[];
}

class RoleSaveError extends Error {
  readonly original: unknown;
  readonly partial: boolean;

  constructor(original: unknown, partial: boolean) {
    super("Role changes could not be saved.");
    this.name = "RoleSaveError";
    this.original = original;
    this.partial = partial;
  }
}

const STATUS_TONE: Record<TaskStatus, "moss" | "sky" | "ghost" | "rust" | "sand"> = {
  scheduled: "ghost",
  pending: "ghost",
  in_progress: "sky",
  completed: "moss",
  skipped: "rust",
  cancelled: "rust",
  overdue: "sand",
};

const EXPENSE_TONE: Record<ExpenseStatus, "sand" | "moss" | "rust" | "sky" | "ghost"> = {
  draft: "ghost",
  submitted: "sand",
  approved: "moss",
  rejected: "rust",
  reimbursed: "sky",
};

const PAYSLIP_TONE: Record<PaySlip["status"], "sand" | "sky" | "moss" | "rust"> = {
  draft: "sand",
  issued: "sky",
  paid: "moss",
  voided: "rust",
};

function formatValue(value: unknown): string {
  // code-health: ignore[ccn nloc params] Small display formatter is over-counted by lizard after TSX parser recovery.
  if (value === true) return "yes";
  if (value === false) return "no";
  if (value === null || value === undefined) return "--";
  return String(value);
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function roleErrorMessage(error: unknown): string {
  if (error instanceof RoleSaveError) {
    const base = roleErrorMessage(error.original);
    if (!error.partial) return base;
    return base + " Some role changes may have been saved; review the refreshed selection before saving again.";
  }
  if (error instanceof ApiError) {
    if (error.status === 403) return "You do not have permission to edit work roles.";
    if (error.status === 422) return error.message || "The selected role change is not valid.";
    return error.message || "Role changes could not be saved.";
  }
  if (error instanceof Error && error.message) return error.message;
  return "Role changes could not be saved.";
}

async function saveEmployeeRoles({
  employeeId,
  startedOn,
  addRoleIds,
  removeLinkIds,
}: RoleEditorPayload): Promise<void> {
  let savedChanges = 0;

  try {
    for (const roleId of addRoleIds) {
      await fetchJson<UserWorkRole>("/api/v1/user_work_roles", {
        method: "POST",
        body: {
          user_id: employeeId,
          work_role_id: roleId,
          started_on: startedOn,
        },
      });
      savedChanges += 1;
    }
    for (const linkId of removeLinkIds) {
      await fetchJson<void>("/api/v1/user_work_roles/" + encodeURIComponent(linkId), {
        method: "DELETE",
      });
      savedChanges += 1;
    }
  } catch (error) {
    throw new RoleSaveError(error, savedChanges > 0);
  }
}

function SettingsOverridePanel({
  overrides,
  resolved,
  catalog,
}: {
  overrides: Record<string, unknown>;
  resolved: Record<string, { value: unknown; source: string }>;
  catalog: SettingDefinition[];
}) {
  const employeeScoped = catalog.filter((d) => d.override_scope.includes("E"));

  return (
    <div className="panel">
      <header className="panel__head"><h2>Settings overrides</h2></header>
      <p className="muted">
        Employee-scoped settings. Overridden values take precedence over property and workspace defaults.
      </p>
      <table className="table">
        <thead>
          <tr>
            <th>Setting</th>
            <th>Effective value</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {employeeScoped.map((def) => {
            const hasOverride = def.key in overrides;
            const res = resolved[def.key];
            return (
              <tr key={def.key}>
                <td title={def.description}>
                  <code className="inline-code">{def.key}</code>
                  <span className="muted setting-label">{def.label}</span>
                </td>
                <td>
                  {hasOverride ? (
                    <strong>{formatValue(res?.value)}</strong>
                  ) : (
                    <span className="muted">{formatValue(res?.value)}</span>
                  )}
                </td>
                <td>
                  {hasOverride ? (
                    <Chip tone="moss" size="sm">overridden</Chip>
                  ) : (
                    <span className="muted">inherited ({res?.source ?? "catalog"})</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

type Tab = "overview" | "payslips" | "leaves" | "settings";

function panelIdFor(tab: Tab): string {
  return `employee-${tab}-panel`;
}

const EMPLOYEE_TABS = [
  { key: "overview", label: "Overview", panelId: panelIdFor("overview") },
  { key: "payslips", label: "Payslips", panelId: panelIdFor("payslips") },
  { key: "leaves", label: "Leaves", panelId: panelIdFor("leaves") },
  { key: "settings", label: "Settings", panelId: panelIdFor("settings") },
] satisfies Array<PageTab & { key: Tab }>;

function tabFromHash(hash: string): Tab {
  // code-health: ignore[ccn nloc] Tiny hash mapper is misattributed by lizard across the surrounding TSX module.
  let key: string;
  try {
    key = decodeURIComponent(hash.replace(/^#/, ""));
  } catch {
    key = "";
  }
  return EMPLOYEE_TABS.find((tab) => tab.key === key)?.key ?? "overview";
}

function normalizeEmployeeTabHash(tab: Tab): void {
  const nextHash = `#${encodeURIComponent(tab)}`;
  if (window.location.hash === "" || window.location.hash === nextHash) return;
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${nextHash}`);
}

export default function EmployeeDetailPage() {
  const { eid = "" } = useParams<{ eid: string }>();
  const [activeTab, setActiveTab] = useState<Tab>(() => tabFromHash(window.location.hash));
  const [roleDialogOpen, setRoleDialogOpen] = useState(false);
  const [selectedRoleIds, setSelectedRoleIds] = useState<Set<string>>(new Set());
  const qc = useQueryClient();

  useEffect(() => {
    const syncFromHash = () => {
      const nextTab = tabFromHash(window.location.hash);
      normalizeEmployeeTabHash(nextTab);
      setActiveTab(nextTab);
    };
    syncFromHash();
    window.addEventListener("hashchange", syncFromHash);
    return () => window.removeEventListener("hashchange", syncFromHash);
  }, []);

  function selectTab(next: string): void {
    setActiveTab(tabFromHash(`#${next}`));
  }

  const detailQ = useQuery({
    queryKey: qk.employee(eid),
    queryFn: () => fetchJson<EmployeeDetail>("/api/v1/employees/" + eid),
    enabled: eid !== "",
  });
  const workRolesQ = useQuery({
    queryKey: qk.workRoles(),
    queryFn: () => fetchAllList<WorkRole>("/api/v1/work_roles"),
    enabled: roleDialogOpen,
  });
  const userWorkRolesQ = useQuery({
    queryKey: [...qk.employee(eid), "user_work_roles"],
    queryFn: () => fetchAllList<UserWorkRole>("/api/v1/users/" + eid + "/user_work_roles"),
    enabled: eid !== "" && roleDialogOpen,
  });
  const invalidateEmployeeRoleQueries = () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: qk.employee(eid) }),
      qc.invalidateQueries({ queryKey: qk.employees() }),
      qc.invalidateQueries({ queryKey: [...qk.employee(eid), "user_work_roles"] }),
    ]);
  const roleSave = useMutation({
    mutationFn: saveEmployeeRoles,
    onError: invalidateEmployeeRoleQueries,
    onSuccess: async () => {
      await invalidateEmployeeRoleQueries();
      setRoleDialogOpen(false);
    },
  });

  useEffect(() => {
    if (!roleDialogOpen || !userWorkRolesQ.data) return;
    setSelectedRoleIds(new Set(userWorkRolesQ.data.map((link) => link.work_role_id)));
  }, [roleDialogOpen, userWorkRolesQ.data]);
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const settingsQ = useQuery({
    queryKey: qk.employeeSettings(eid),
    queryFn: () => fetchJson<EntitySettingsPayload>("/api/v1/employees/" + eid + "/settings"),
    enabled: eid !== "" && activeTab === "settings",
  });
  const catalogQ = useQuery({
    queryKey: qk.settingsCatalog(),
    queryFn: () => fetchJson<SettingDefinition[]>("/api/v1/settings/catalog"),
    enabled: activeTab === "settings",
  });
  const leavesQ = useQuery({
    queryKey: qk.employeeLeaves(eid),
    queryFn: () => fetchJson<LeavesPayload>("/api/v1/employees/" + eid + "/leaves"),
    enabled: eid !== "" && activeTab === "leaves",
    retry: false,
  });
  const payslipsQ = useQuery({
    queryKey: qk.payslips(),
    queryFn: () => fetchJson<PayrollPayslipListPayload>("/api/v1/payroll/payslips"),
    enabled: eid !== "" && activeTab === "payslips",
    retry: false,
  });

  if (detailQ.isPending || propsQ.isPending) {
    return <DeskPage title="Employee"><Loading /></DeskPage>;
  }
  if (!detailQ.data || !propsQ.data) {
    return <DeskPage title="Employee">Failed to load.</DeskPage>;
  }

  const { subject, subject_tasks, subject_expenses } = detailQ.data;
  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));
  const employeePayslips = (payslipsQ.data?.data ?? [])
    .filter((p) => p.user_id === subject.id)
    .map(mapPayrollPayslip);
  const roleRows = workRolesQ.data ?? [];
  const currentLinks = userWorkRolesQ.data ?? [];
  const currentRoleIds = new Set(currentLinks.map((link) => link.work_role_id));

  function openRoleDialog() {
    setRoleDialogOpen(true);
    roleSave.reset();
  }

  function closeRoleDialog() {
    setRoleDialogOpen(false);
  }

  function toggleRole(roleId: string, checked: boolean) {
    setSelectedRoleIds((prev) => {
      const next = new Set(prev);
      if (checked) {
        next.add(roleId);
      } else {
        next.delete(roleId);
      }
      return next;
    });
  }

  function submitRoleDialog(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const addRoleIds = [...selectedRoleIds].filter((roleId) => !currentRoleIds.has(roleId));
    const removeLinkIds = currentLinks
      .filter((link) => !selectedRoleIds.has(link.work_role_id))
      .map((link) => link.id);
    roleSave.mutate({
      employeeId: subject.id,
      startedOn: subject.started_on || todayIso(),
      addRoleIds,
      removeLinkIds,
    });
  }

  return (
    <DeskPage
      title={subject.name}
      sub={subject.roles.join(" · ") + " · " + subject.phone}
      actions={
        <button type="button" className="btn btn--ghost" onClick={openRoleDialog}>
          Edit roles
        </button>
      }
    >
      <PageTabs
        ariaLabel="Employee sections"
        tabs={EMPLOYEE_TABS}
        hashBacked
        defaultKey="overview"
        selectedKey={activeTab}
        onSelect={selectTab}
      />

      <FormModal
        open={roleDialogOpen}
        title="Edit work roles"
        titleId="employee-role-dialog-title"
        eyebrow="Employee roles"
        subtitle="These are scheduling and assignment roles from the workspace work-role catalog."
        formClassName="employee-role-form"
        onClose={() => {
          setRoleDialogOpen(false);
          roleSave.reset();
        }}
        onSubmit={submitRoleDialog}
        actions={
          <>
            <button type="button" className="btn btn--ghost" onClick={closeRoleDialog}>
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn--moss"
              disabled={
                roleSave.isPending ||
                workRolesQ.isPending ||
                userWorkRolesQ.isPending ||
                workRolesQ.isError ||
                userWorkRolesQ.isError
              }
            >
              {roleSave.isPending ? "Saving..." : "Save roles"}
            </button>
          </>
        }
      >
          {workRolesQ.isPending || userWorkRolesQ.isPending ? (
            <Loading />
          ) : workRolesQ.isError || userWorkRolesQ.isError ? (
            <p className="form-error" role="alert">
              Work roles could not be loaded.
            </p>
          ) : roleRows.length === 0 ? (
            <p className="muted">No work roles exist in this workspace.</p>
          ) : (
            <fieldset className="field form-field form-field--optional employee-role-form__field sheet-form__field">
              <legend className="form-field__label">
                Work roles <span className="form-field__requirement form-field__requirement--optional">Optional</span>
              </legend>
              <div className="employee-role-form__checks">
              {roleRows.map((role) => (
                <label key={role.id} className="field--inline">
                  <input
                    type="checkbox"
                    checked={selectedRoleIds.has(role.id)}
                    onChange={(event) => toggleRole(role.id, event.currentTarget.checked)}
                  />
                  <span>{role.name}</span>
                  <code className="inline-code">{role.key}</code>
                </label>
              ))}
              </div>
            </fieldset>
          )}
          {roleSave.isError ? (
            <p className="form-error" role="alert">
              {roleErrorMessage(roleSave.error)}
            </p>
          ) : null}
      </FormModal>

      {activeTab === "overview" && (
        <section id={panelIdFor("overview")} className="grid grid--split" role="tabpanel">
          <div className="panel">
            <header className="panel__head"><h2>Tasks</h2></header>
            {subject_tasks.length > 0 ? (
              <ul className="task-list task-list--desk">
                {subject_tasks.map((t) => {
                  const prop = propsById.get(t.property_id);
                  return (
                    <li key={t.id} className="task-row">
                      <span className="task-row__time table__mono">
                        <DateTime value={t.scheduled_start} showTime />
                      </span>
                      <span className="task-row__title">
                        <strong>{t.title}</strong>
                        <span className="task-row__area">{t.area}</span>
                      </span>
                      {prop && <Chip tone={prop.color} size="sm">{prop.name}</Chip>}
                      <Chip tone={STATUS_TONE[t.status]} size="sm">{t.status}</Chip>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <EmptyState
                icon={ClipboardList}
                title="No tasks scheduled"
                copy="Assigned work for this employee will appear here once it is scheduled."
                variant="compact"
              />
            )}
          </div>

          <div className="panel">
            <header className="panel__head"><h2>Recent expenses</h2></header>
            {subject_expenses.length > 0 ? (
              <ul className="expense-list">
                {subject_expenses.map((x) => (
                  <li key={x.id} className="expense-row">
                    <div className="expense-row__main">
                      <strong>{x.vendor}</strong>
                      <span className="expense-row__note">{x.note_md}</span>
                      <span className="expense-row__time">
                        <DateTime value={x.submitted_at} showTime empty="draft" />
                      </span>
                    </div>
                    <div className="expense-row__side">
                      <span className="expense-row__amount">{formatMoney(x.total_amount_cents, x.currency)}</span>
                      <Chip tone={EXPENSE_TONE[x.state]} size="sm">{x.state}</Chip>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState
                icon={ReceiptText}
                title="No recent expenses"
                copy="Submitted reimbursements and purchases for this employee will appear here."
                variant="compact"
              />
            )}
          </div>
        </section>
      )}

      {activeTab === "settings" && (
        <div id={panelIdFor("settings")} role="tabpanel">
          {(settingsQ.isPending || catalogQ.isPending) ? (
            <Loading />
          ) : settingsQ.data && catalogQ.data ? (
            <SettingsOverridePanel
              overrides={settingsQ.data.overrides}
              resolved={settingsQ.data.resolved}
              catalog={catalogQ.data}
            />
          ) : (
            <p>Failed to load settings.</p>
          )}
        </div>
      )}

      {activeTab === "leaves" && (
        <div id={panelIdFor("leaves")} role="tabpanel">
          {leavesQ.isPending ? (
            <Loading />
          ) : leavesQ.error instanceof ApiError && leavesQ.error.status === 403 ? (
            <ForbiddenPanel detail="You do not have permission to view this employee's leave ledger." />
          ) : leavesQ.isError || !leavesQ.data ? (
            <p>Failed to load leaves.</p>
          ) : (
            <div className="panel">
              <header className="panel__head"><h2>Leave ledger</h2></header>
              {leavesQ.data.leaves.length === 0 ? (
                <EmptyState
                  icon={CalendarOff}
                  title="No leave on file"
                  copy="Approved and pending leave for this employee will appear here."
                  variant="compact"
                />
              ) : (
                <table className="table table--roomy">
                  <thead>
                    <tr>
                      <th>Dates</th>
                      <th>Days</th>
                      <th>Category</th>
                      <th>Note</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {leavesQ.data.leaves.map((lv) => (
                      <tr key={lv.id}>
                        <td className="mono">
                          {fmtDayMonYear(lv.starts_on)} → {fmtDayMonYear(lv.ends_on)}
                        </td>
                        <td>{inclusiveDays(lv.starts_on, lv.ends_on)}</td>
                        <td><Chip tone="ghost" size="sm">{lv.category}</Chip></td>
                        <td className="table__sub">{lv.note}</td>
                        <td>
                          <Chip tone={lv.approved_at ? "moss" : "sand"} size="sm">
                            {lv.approved_at ? "Approved" : "Pending"}
                          </Chip>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </div>
      )}

      {activeTab === "payslips" && (
        <div id={panelIdFor("payslips")} role="tabpanel">
          {payslipsQ.isPending ? (
            <Loading />
          ) : payslipsQ.error instanceof ApiError && payslipsQ.error.status === 403 ? (
            <ForbiddenPanel detail="You do not have permission to view this employee's payslips." />
          ) : payslipsQ.isError || !payslipsQ.data ? (
            <p>Failed to load payslips.</p>
          ) : (
            <div className="panel">
              <header className="panel__head"><h2>Payslips</h2></header>
              {employeePayslips.length === 0 ? (
                <EmptyState
                  icon={ReceiptText}
                  title="No payslips on file"
                  copy="Draft, issued, and paid payslips for this employee will appear here."
                  variant="compact"
                />
              ) : (
                <table className="table table--roomy">
                  <thead>
                    <tr>
                      <th>Pay period</th>
                      <th>Hours</th>
                      <th>Overtime</th>
                      <th>Gross</th>
                      <th>Net</th>
                      <th>Status</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {employeePayslips.map((p) => (
                      <tr key={p.id}>
                        <td className="mono">{p.pay_period_id}</td>
                        <td className="mono">{p.hours} h</td>
                        <td className="mono">{p.overtime} h</td>
                        <td className="mono">{formatMoney(p.gross_cents, p.currency)}</td>
                        <td className="mono"><strong>{formatMoney(p.net_cents, p.currency)}</strong></td>
                        <td><Chip tone={PAYSLIP_TONE[p.status]} size="sm">{p.status}</Chip></td>
                        <td>
                          <button
                            type="button"
                            className="btn btn--sm btn--ghost"
                            onClick={() => openApiDownload(`/api/v1/payroll/payslips/${p.id}/pdf`)}
                          >
                            Preview PDF
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </div>
      )}

    </DeskPage>
  );
}
