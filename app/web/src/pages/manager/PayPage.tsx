import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";
import { ApiError, fetchJson, openApiDownload } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import {
  type PayPageSlip,
  type PayrollPayslipListPayload,
  mapPayrollPayslip,
} from "@/lib/payrollPayslips";
import DeskPage from "@/components/DeskPage";
import { Avatar, Chip, Loading, StatCard } from "@/components/common";
import type { Employee, PaySlip, PendingReimbursement } from "@/types/api";

interface PayPayload {
  current: PayPageSlip[];
  previous: PayPageSlip[];
}

interface PayPeriodPayload {
  id: string;
  starts_at: string;
  ends_at: string;
  state: "open" | "locked" | "paid" | string;
  locked_at: string | null;
}

interface PayPeriodListPayload {
  data: PayPeriodPayload[];
}

interface WorkEngagementPayload {
  id: string;
  user_id: string;
  archived_on: string | null;
}

interface WorkEngagementListPayload {
  data: WorkEngagementPayload[];
}

const STATUS_TONE: Record<PaySlip["status"], "sand" | "sky" | "moss" | "rust"> = {
  draft: "sand",
  issued: "sky",
  paid: "moss",
  voided: "rust",
};

function sumGross(xs: PayPageSlip[]): number {
  // code-health: ignore[ccn nloc] One-line aggregate is over-counted by lizard after TSX parser recovery.
  return xs.reduce((acc, p) => acc + p.gross_cents, 0);
}
function sumNet(xs: PayPageSlip[]): number {
  return xs.reduce((acc, p) => acc + p.net_cents, 0);
}

function mapPayPayload(payload: PayrollPayslipListPayload): PayPayload {
  const payslips = payload.data.map(mapPayrollPayslip);
  const periodIds = Array.from(new Set(payslips.map((p) => p.pay_period_id)));
  const currentPeriodId =
    periodIds.find((periodId) =>
      payslips.some(
        (p) => p.pay_period_id === periodId && (p.status === "draft" || p.status === "issued"),
      ),
    ) ?? null;
  const previousPeriodId =
    periodIds.find((periodId) =>
      periodId !== currentPeriodId &&
      payslips.some((p) => p.pay_period_id === periodId && p.status === "paid"),
    ) ?? null;

  return {
    current:
      currentPeriodId === null
        ? []
        : payslips.filter(
            (p) =>
              p.pay_period_id === currentPeriodId &&
              (p.status === "draft" || p.status === "issued"),
          ),
    previous:
      previousPeriodId === null
        ? []
        : payslips.filter((p) => p.pay_period_id === previousPeriodId && p.status === "paid"),
  };
}

function periodLabel(period: PayPeriodPayload): string {
  const starts = new Date(period.starts_at);
  const ends = new Date(period.ends_at);
  if (Number.isNaN(starts.getTime()) || Number.isNaN(ends.getTime())) return "Open period";
  const displayEnd = new Date(ends.getTime() - 1);
  return `${starts.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  })} to ${displayEnd.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  })}`;
}

function blockerMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.detail ?? error.title ?? error.message;
  }
  if (error instanceof Error) return error.message;
  return "The period could not be closed. Review unsettled bookings, approvals, and pay rules.";
}

export default function PayPage() {
  // code-health: ignore[ccn nloc] Pay route coordinates payslip and reimbursement query data while preserving existing table layout.
  const queryClient = useQueryClient();
  const closeDialogRef = useRef<HTMLDialogElement>(null);
  const [closeTarget, setCloseTarget] = useState<PayPeriodPayload | null>(null);
  const [closeBlocker, setCloseBlocker] = useState<string | null>(null);
  const payQ = useQuery({
    queryKey: qk.payslips(),
    queryFn: () => fetchJson<PayrollPayslipListPayload>("/api/v1/payroll/payslips"),
    select: mapPayPayload,
  });
  const periodsQueryKey = qk.payPeriods();
  const periodsQ = useQuery({
    queryKey: periodsQueryKey,
    queryFn: () => fetchJson<PayPeriodListPayload>("/api/v1/payroll/pay-periods"),
  });
  const employeesQ = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });
  // §09 "Amount owed to the employee" — workspace-wide aggregate of
  // approved-but-not-yet-reimbursed claims. Grouped by owed_currency
  // (the destination's currency, not the claim's). ``by_user`` drives
  // the per-employee breakdown table.
  const pendingQ = useQuery({
    queryKey: qk.expensesPendingReimbursement("all"),
    queryFn: () =>
      fetchJson<PendingReimbursement>("/api/v1/expenses/pending_reimbursement"),
  });
  const pending = pendingQ.data;
  const pendingByUser = pending?.by_user ?? [];
  const engagementQs = useQueries({
    queries: pendingByUser.map((row) => {
      const params = new URLSearchParams({ user_id: row.user_id, active: "true" });
      return {
        queryKey: qk.workEngagementActive(row.user_id),
        queryFn: () =>
          fetchJson<WorkEngagementListPayload>(
            `/api/v1/work_engagements?${params.toString()}`,
          ),
        enabled: pendingQ.isSuccess,
      };
    }),
  });
  const openPeriods = periodsQ.data?.data.filter((period) => period.state === "open") ?? [];
  const openPeriod = openPeriods.length === 1 ? openPeriods[0] : null;
  const closeMutation = useMutation({
    mutationFn: (periodId: string) =>
      fetchJson<PayPeriodPayload>(`/api/v1/payroll/pay-periods/${periodId}/lock`, {
        method: "POST",
      }),
    onSuccess: async (lockedPeriod) => {
      setCloseBlocker(null);
      closeDialogRef.current?.close();
      setCloseTarget(null);
      queryClient.setQueryData<PayPeriodListPayload>(periodsQueryKey, (existing) =>
        existing
          ? {
              data: existing.data.map((period) =>
                period.id === lockedPeriod.id ? lockedPeriod : period,
              ),
            }
          : existing,
      );
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: qk.payslips() }),
        queryClient.invalidateQueries({ queryKey: periodsQueryKey }),
        queryClient.invalidateQueries({ queryKey: qk.expensesPendingReimbursement("all") }),
      ]);
    },
    onError: (error) => {
      setCloseBlocker(blockerMessage(error));
    },
  });

  const closeDisabledReason = (() => {
    if (periodsQ.isPending) return "Payroll periods are still loading.";
    if (periodsQ.isError) return "Payroll periods could not be loaded.";
    if (openPeriods.length === 0) return "No open payroll period is available.";
    if (openPeriods.length > 1) return "Multiple open payroll periods need review before closing.";
    return undefined;
  })();
  const openCloseDialog = () => {
    if (!openPeriod) return;
    setCloseTarget(openPeriod);
    setCloseBlocker(null);
    closeDialogRef.current?.showModal();
  };
  const submitClose = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (closeTarget) closeMutation.mutate(closeTarget.id);
  };

  const sub = "Periods, payslips, pay rules. Gross only — taxes and social contributions are out of scope.";
  const actions = closeDisabledReason ? (
    <span className="page-action-disabled">
      <button
        type="button"
        className="btn btn--moss"
        disabled
        aria-describedby="pay-close-period-disabled-reason"
      >
        Close period
      </button>
      <span id="pay-close-period-disabled-reason" className="page-action-disabled__reason">
        {closeDisabledReason}
      </span>
    </span>
  ) : (
    <button
      type="button"
      className="btn btn--moss"
      onClick={openCloseDialog}
      disabled={closeMutation.isPending}
    >
      {closeMutation.isPending ? "Closing..." : "Close period"}
    </button>
  );
  const overflow = [
    {
      label: "Export CSV",
      onSelect: () => undefined,
      disabledReason: "Payroll data is still loading.",
    },
  ];

  if (payQ.isPending || employeesQ.isPending) {
    return <DeskPage title="Pay" sub={sub} actions={actions} overflow={overflow}><Loading /></DeskPage>;
  }
  if (!payQ.data || !employeesQ.data) {
    return <DeskPage title="Pay" sub={sub} actions={actions} overflow={overflow}>Failed to load.</DeskPage>;
  }

  const empById = new Map(employeesQ.data.map((e) => [e.id, e]));
  const { current, previous } = payQ.data;
  const currentPeriodId = current[0]?.pay_period_id ?? null;
  const exportOverflow = [
    {
      label: "Export CSV",
      onSelect: () => {
        if (!currentPeriodId) return;
        const params = new URLSearchParams({ period_id: currentPeriodId });
        openApiDownload(`/api/v1/payroll/exports/payslips.csv?${params.toString()}`);
      },
      disabledReason: currentPeriodId ? undefined : "No open payroll period is available to export.",
    },
  ];
  const defaultCurrency = current[0]?.currency ?? "EUR";
  const pendingTotals = pending?.totals_by_currency ?? [];
  const engagementUserById = new Map<string, string>();
  for (const q of engagementQs) {
    for (const row of q.data?.data ?? []) {
      if (row.archived_on === null) engagementUserById.set(row.id, row.user_id);
    }
  }
  const pendingClaimCountByUser = new Map<string, number>();
  for (const claim of pending?.claims ?? []) {
    const userId = engagementUserById.get(claim.work_engagement_id);
    if (userId) pendingClaimCountByUser.set(userId, (pendingClaimCountByUser.get(userId) ?? 0) + 1);
  }
  const pendingCountsPending = engagementQs.some((q) => q.isPending);
  const pendingCountsError = engagementQs.some((q) => q.isError);
  const pendingHeadline =
    pendingTotals.length === 0
      ? formatMoney(0, defaultCurrency)
      : pendingTotals
          .map((t) => formatMoney(t.amount_cents, t.currency))
          .join(" + ");
  const closeSucceeded = closeMutation.isSuccess && !openPeriod;

  return (
    <DeskPage title="Pay" sub={sub} actions={actions} overflow={exportOverflow}>
      <dialog
        className="modal"
        ref={closeDialogRef}
        aria-label="Close pay period"
        onClose={() => {
          if (!closeMutation.isPending) {
            setCloseTarget(null);
            setCloseBlocker(null);
          }
        }}
      >
        <form className="modal__body" onSubmit={submitClose}>
          <h3 className="modal__title">Close pay period</h3>
          <p className="modal__sub">
            {closeTarget
              ? `Lock ${periodLabel(closeTarget)} and compute draft payslips. The server will block this if bookings, approvals, or pay rules still need attention.`
              : "No open payroll period is selected."}
          </p>
          {closeBlocker && (
            <p role="alert" className="form-error">
              {closeBlocker}
            </p>
          )}
          <div className="modal__actions">
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => closeDialogRef.current?.close()}
              disabled={closeMutation.isPending}
            >
              Cancel
            </button>
            <button type="submit" className="btn btn--moss" disabled={!closeTarget || closeMutation.isPending}>
              {closeMutation.isPending ? "Closing..." : "Close period"}
            </button>
          </div>
        </form>
      </dialog>
      {closeSucceeded && (
        <p role="status" className="muted">
          Period locked. Pay data refreshed.
        </p>
      )}

      <section className="grid grid--stats">
        <StatCard label="Current period" value="April 2026" sub="open · closes 30 Apr" />
        <StatCard label="Drafts" value={current.length} sub="payslips pending issue" />
        <StatCard
          label="April gross (est.)"
          value={formatMoney(sumGross(current), defaultCurrency)}
          sub="before reimbursements"
        />
        <StatCard
          label="Last period"
          value={formatMoney(sumNet(previous), previous[0]?.currency ?? defaultCurrency)}
          sub="March · all paid"
        />
        <StatCard
          label="Pending reimbursements"
          value={pendingHeadline}
          sub={
            pendingByUser.length === 0
              ? "nothing owed right now"
              : `${pendingByUser.length} employee${pendingByUser.length === 1 ? "" : "s"} · destination currency`
          }
        />
      </section>

      <div className="panel">
        <header className="panel__head"><h2>April 2026 — drafts</h2></header>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Employee</th><th>Hours</th><th>Overtime</th><th>Gross</th>
              <th>Reimbursements</th><th>Net</th><th>Status</th><th></th>
            </tr>
          </thead>
          <tbody>
            {current.map((p) => {
              const emp = empById.get(p.employee_id);
              return (
                <tr key={p.id}>
                  <td>
                    {emp && <><Avatar url={emp.avatar_url} initials={emp.avatar_initials} size="xs" alt={emp.name} /> {emp.name}</>}
                  </td>
                  <td className="mono">{p.hours} h</td>
                  <td className="mono">{p.overtime} h</td>
                  <td className="mono">{formatMoney(p.gross_cents, p.currency)}</td>
                  <td className="mono">{formatMoney(p.reimbursements_cents, p.currency)}</td>
                  <td className="mono"><strong>{formatMoney(p.net_cents, p.currency)}</strong></td>
                  <td><Chip tone={STATUS_TONE[p.status]} size="sm">{p.status}</Chip></td>
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
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head">
          <div className="panel__head-stack">
            <h2>Pending reimbursements</h2>
            <p className="panel__sub muted">
              Approved expense claims waiting to roll into a payslip.
              Each row shows what the employee is owed in the currency
              of the account the reimbursement will land in.
            </p>
          </div>
        </header>
        {pendingQ.isPending || pendingCountsPending ? (
          <Loading />
        ) : pendingQ.isError || pendingCountsError ? (
          <>Failed to load.</>
        ) : pendingByUser.length === 0 ? (
          <p className="muted">Nothing owed right now — all approved claims are already on a payslip.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Employee</th>
                <th>Claims</th>
                <th>Owed</th>
              </tr>
            </thead>
            <tbody>
              {pendingByUser.map((row) => {
                const emp = empById.get(row.user_id);
                const claimCount = pendingClaimCountByUser.get(row.user_id) ?? 0;
                return (
                  <tr key={row.user_id}>
                    <td>
                      {emp ? (
                        <>
                          <Avatar url={emp.avatar_url} initials={emp.avatar_initials} size="xs" alt={emp.name} /> {emp.name}
                        </>
                      ) : (
                        row.user_name
                      )}
                    </td>
                    <td className="mono">{claimCount}</td>
                    <td className="mono">
                      {row.totals_by_currency.map((t, i) => (
                        <span key={t.currency}>
                          {i > 0 && " + "}
                          <strong>{formatMoney(t.amount_cents, t.currency)}</strong>
                        </span>
                      ))}
                    </td>
                  </tr>
                );
              })}
              <tr className="table__foot">
                <td><strong>Total</strong></td>
                <td className="mono">
                  <strong>{pending?.claims.length ?? 0}</strong>
                </td>
                <td className="mono">
                  <strong>
                    {pendingTotals.map((t, i) => (
                      <span key={t.currency}>
                        {i > 0 && " + "}
                        {formatMoney(t.amount_cents, t.currency)}
                      </span>
                    ))}
                  </strong>
                </td>
              </tr>
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <header className="panel__head"><h2>March 2026 — paid</h2></header>
        <table className="table">
          <thead>
            <tr>
              <th>Employee</th><th>Gross</th><th>Reimb.</th><th>Net</th><th>Paid</th>
            </tr>
          </thead>
          <tbody>
            {previous.map((p) => {
              const emp = empById.get(p.employee_id);
              return (
                <tr key={p.id}>
                  <td>{emp?.name}</td>
                  <td className="mono">{formatMoney(p.gross_cents, p.currency)}</td>
                  <td className="mono">{formatMoney(p.reimbursements_cents, p.currency)}</td>
                  <td className="mono"><strong>{formatMoney(p.net_cents, p.currency)}</strong></td>
                  <td><Chip tone="moss" size="sm">paid</Chip></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel panel--danger">
        <header className="panel__head"><h2>Always-gated actions</h2></header>
        <p className="muted">These payroll actions always require a manager passkey — the agent approval flow cannot bypass them.</p>
        <ul className="danger-list">
          <li><code className="inline-code">payout_destination.create</code> · <code className="inline-code">payout_destination.update</code></li>
          <li><code className="inline-code">work_engagement.set_default_pay_destination</code></li>
          <li>
            <code className="inline-code">POST /payslips/:id/payout_manifest</code>{" "}
            <Chip tone="rust" size="sm">session-only</Chip>
          </li>
        </ul>
      </div>
    </DeskPage>
  );
}
