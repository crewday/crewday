import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { qk } from "@/lib/queryKeys";
import { fetchJson, openApiDownload } from "@/lib/api";
import {
  fetchAllExpenseClaims,
  mapExpenseClaimPayload,
  type ExpenseClaimPayload,
} from "@/lib/expenses";
import { useDecideMutation } from "@/lib/useDecideMutation";
import { formatMoney } from "@/lib/money";
import DeskPage from "@/components/DeskPage";
import DateTime from "@/components/DateTime";
import FormField from "@/components/FormField";
import FormModal, { FormModalGrid } from "@/components/FormModal";
import { Camera, ReceiptText, ShieldCheck } from "lucide-react";
import { Chip, EmptyState, Loading, StatCard } from "@/components/common";
import { EXPENSE_STATUS_TONE } from "@/lib/tones";
import type { Expense, ExpenseCategory, ExpenseStatus } from "@/types/api";

type Decision = "approve" | "reject" | "reimburse";
type ApprovalEditBody = Partial<Pick<Expense, "total_amount_cents" | "currency" | "category">>;

type ExpenseCorrectionButtonProps = {
  expense: Expense;
  onApproved: (expense: Expense) => void;
};

const EXPENSE_CATEGORIES: ExpenseCategory[] = [
  "supplies",
  "fuel",
  "food",
  "transport",
  "maintenance",
  "other",
];

function amountInputValue(cents: number): string {
  // code-health: ignore[ccn nloc] One-line money input formatter is over-counted by lizard after TSX parser recovery.
  // Input normalization for <input type="number">; grouping separators would be invalid.
  return (cents / 100).toFixed(2);
}

function parseAmountCents(value: string): number | null {
  const trimmed = value.trim();
  if (!/^\d+(?:\.\d{1,2})?$/.test(trimmed)) return null;
  const [whole, decimal = ""] = trimmed.split(".");
  const cents = Number(whole) * 100 + Number(decimal.padEnd(2, "0"));
  if (!Number.isSafeInteger(cents) || cents <= 0) return null;
  return cents;
}

function normalizedCurrency(value: string): string | null {
  const normalized = value.trim().toUpperCase();
  return /^[A-Z]{3}$/.test(normalized) ? normalized : null;
}

function isExpenseCategory(value: string): value is ExpenseCategory {
  return EXPENSE_CATEGORIES.includes(value as ExpenseCategory);
}

function expenseCategoryLabel(value: ExpenseCategory): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function sumCents(xs: Expense[]): number {
  return xs.reduce((acc, x) => acc + x.total_amount_cents, 0);
}

function totalLabel(xs: Expense[]): string {
  if (xs.length === 0) return "0.00 total";
  const cur = xs[0]?.currency ?? "EUR";
  return formatMoney(sumCents(xs), cur) + " total";
}

function utcDayStart(iso: string): number | null {
  const time = new Date(iso).getTime();
  if (!Number.isFinite(time)) return null;
  const date = new Date(time);
  return Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate());
}

function expensesExportPath(expenses: Expense[]): string | null {
  const dayStarts = expenses
    .map((expense) => utcDayStart(expense.purchased_at))
    .filter((time): time is number => time !== null)
    .sort((a, b) => a - b);
  const sinceMs = dayStarts[0];
  const latestDayMs = dayStarts[dayStarts.length - 1];
  if (sinceMs === undefined || latestDayMs === undefined) return null;
  const params = new URLSearchParams({
    since: new Date(sinceMs).toISOString(),
    until: new Date(latestDayMs + 24 * 60 * 60 * 1000).toISOString(),
  });
  return `/api/v1/payroll/exports/expense-ledger.csv?${params.toString()}`;
}

function ExpenseCorrectionButton({ expense, onApproved }: ExpenseCorrectionButtonProps) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState(() => ({
    amount: "",
    currency: "",
    category: "other" as ExpenseCategory,
    error: null as string | null,
  }));
  const { amount, currency, category, error: formError } = form;

  const initialForm = () => ({
    amount: amountInputValue(expense.total_amount_cents),
    currency: expense.currency,
    category: isExpenseCategory(expense.category) ? expense.category : "other",
    error: null,
  });

  const openDialog = () => {
    setForm(initialForm());
    setOpen(true);
  };

  const approveWithEdits = useMutation({
    mutationFn: async (body: ApprovalEditBody) => {
      const payload = await fetchJson<ExpenseClaimPayload>(
        `/api/v1/expenses/${expense.id}/approve`,
        {
          method: "POST",
          body,
        },
      );
      return mapExpenseClaimPayload(payload);
    },
    onSuccess: (updated) => {
      qc.setQueryData<Expense[]>(qk.expenses("all"), (prev) =>
        prev?.map((item) => (item.id === updated.id ? updated : item)),
      );
      qc.invalidateQueries({ queryKey: qk.expenses("all") });
      qc.invalidateQueries({ queryKey: qk.dashboard() });
      onApproved(updated);
      setOpen(false);
    },
    onError: (error) => {
      setForm((current) => ({
        ...current,
        error: error instanceof Error ? error.message : "Could not approve the correction.",
      }));
    },
  });

  const amountCents = parseAmountCents(amount);
  const cleanCurrency = normalizedCurrency(currency);
  const cleanCategory = isExpenseCategory(category) ? category : null;
  const amountError =
    amount.trim() === "" || amountCents !== null
      ? null
      : "Enter a positive amount with no more than two decimal places.";
  const currencyError =
    currency.trim() === "" || cleanCurrency !== null
      ? null
      : "Enter a three-letter ISO currency code.";
  const categoryError = cleanCategory === null ? "Choose a supported expense category." : null;
  const validationError = amountError ?? currencyError ?? categoryError;
  const body: ApprovalEditBody = {};
  if (amountCents !== null && amountCents !== expense.total_amount_cents) {
    body.total_amount_cents = amountCents;
  }
  if (cleanCurrency !== null && cleanCurrency !== expense.currency) {
    body.currency = cleanCurrency;
  }
  if (cleanCategory !== null && cleanCategory !== expense.category) {
    body.category = cleanCategory;
  }
  const hasEdits = Object.keys(body).length > 0;

  return (
    <>
      <button
        className="btn btn--ghost"
        type="button"
        onClick={openDialog}
      >
        Correct and approve
      </button>

      <FormModal
        open={open}
        title={`Correct ${expense.vendor}`}
        eyebrow="Expense approval"
        subtitle={
          "This approves the claim with corrected values. The submitted claim is not rewritten; the approval audit row records the before and after values."
        }
        formClassName="expense-correction-form"
        onClose={() => {
          setOpen(false);
          setForm(initialForm());
        }}
        onSubmit={(event) => {
          event.preventDefault();
          setForm((current) => ({ ...current, error: null }));
          if (validationError !== null) {
            setForm((current) => ({ ...current, error: validationError }));
            return;
          }
          if (!hasEdits) {
            setForm((current) => ({
              ...current,
              error: "Change the amount, currency, or category before approving with corrections.",
            }));
            return;
          }
          approveWithEdits.mutate(body);
        }}
        actions={
          <>
            <button type="button" className="btn btn--ghost" onClick={() => setOpen(false)}>
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn--moss"
              disabled={approveWithEdits.isPending || validationError !== null || !hasEdits}
            >
              {approveWithEdits.isPending ? "Approving..." : "Approve corrected claim"}
            </button>
          </>
        }
      >
          <FormModalGrid className="expense-correction-form__grid">
          <FormField label="Amount" requirement="required" className="expense-correction-form__field sheet-form__field">
            <input
              inputMode="decimal"
              required
              value={amount}
              aria-invalid={amountError !== null}
              aria-describedby={
                amountError !== null
                  ? `expense-correction-amount-error-${expense.id}`
                  : undefined
              }
              onChange={(event) => setForm((current) => ({ ...current, amount: event.target.value }))}
             aria-label="Amount"/>
          {amountError !== null && (
            <p id={`expense-correction-amount-error-${expense.id}`} className="form-field-error">
              {amountError}
            </p>
          )}
          </FormField>

          <FormField label="Currency" requirement="required" className="expense-correction-form__field sheet-form__field">
            <input
              maxLength={3}
              required
              value={currency}
              aria-invalid={currencyError !== null}
              aria-describedby={
                currencyError !== null
                  ? `expense-correction-currency-error-${expense.id}`
                  : undefined
              }
              onChange={(event) => setForm((current) => ({ ...current, currency: event.target.value }))}
             aria-label="Currency"/>
          {currencyError !== null && (
            <p id={`expense-correction-currency-error-${expense.id}`} className="form-field-error">
              {currencyError}
            </p>
          )}
          </FormField>
          </FormModalGrid>

          <FormField label="Category" requirement="required" className="expense-correction-form__field sheet-form__field">
            <select
              value={category}
              aria-invalid={categoryError !== null}
              aria-describedby={
                categoryError !== null
                  ? `expense-correction-category-error-${expense.id}`
                  : undefined
              }
              onChange={(event) => setForm((current) => ({
                ...current,
                category: event.target.value as ExpenseCategory,
              }))} aria-label="Category"
            >
              {EXPENSE_CATEGORIES.map((value) => (
                <option key={value} value={value}>{expenseCategoryLabel(value)}</option>
              ))}
            </select>
          {categoryError !== null && (
            <p id={`expense-correction-category-error-${expense.id}`} className="form-field-error">
              {categoryError}
            </p>
          )}
          </FormField>

          {formError !== null && (
            <p className="login__notice login__notice--danger" role="alert">
              {formError}
            </p>
          )}
      </FormModal>
    </>
  );
}

/**
 * Manager-side expense approvals desk.
 *
 * Reads the workspace-wide queue from `GET /api/v1/expenses` (cd-t6y2).
 * The server returns the cursor-paginated `{data, next_cursor, has_more}`
 * envelope from spec §12; `fetchAllExpenseClaims` walks every page and
 * returns the flattened `Expense[]` so the client-side filter still
 * sees the full set. Per-page driving will land alongside cd-mh4p's
 * pending-reimbursement panel rework.
 *
 * The payload no longer carries a `claimant`/`employee_id` field
 * (engagement → user resolution lives in cd-g6nf). Until that lands
 * the desk shows the bound `work_engagement_id` short-form rather
 * than a name + avatar, surfacing *something* identifiable is
 * better than a blank, and keeps the row expressive enough for a
 * manager to triage. The avatar slot returns once the roster
 * endpoint is wired.
 *
 * Likewise the agent-autofill confidence chip (`ocr_confidence` on the
 * legacy mock shape) is hidden until cd-95zb surfaces a per-claim
 * extraction confidence on the server payload, guessing locally
 * would make the chip lie.
 */
export default function ExpensesApprovalsPage() {
  const [decisionNotice, setDecisionNotice] = useState<string | null>(null);
  const expensesQ = useQuery({
    queryKey: qk.expenses("all"),
    queryFn: () => fetchAllExpenseClaims(),
  });

  const decide = useDecideMutation<Expense[], Decision>({
    queryKey: qk.expenses("all"),
    endpoint: (id, decision) => "/api/v1/expenses/" + id + "/" + decision,
    applyOptimistic: (prev, id, decision) => {
      const nextState: ExpenseStatus =
        decision === "approve" ? "approved" : decision === "reject" ? "rejected" : "reimbursed";
      return prev.map((x) => (x.id === id ? { ...x, state: nextState } : x));
    },
  });

  const sub = "Review submitted claims. Agent autofill flags low-confidence fields; approving snaps the exchange rate and attaches to the current open pay period.";
  const overflow = [
    {
      label: "Export CSV",
      onSelect: () => undefined,
      disabledReason: "Expense data is still loading.",
    },
  ];

  if (expensesQ.isPending) {
    return <DeskPage title="Expense approvals" sub={sub} overflow={overflow}><Loading /></DeskPage>;
  }
  if (!expensesQ.data) {
    return <DeskPage title="Expense approvals" sub={sub} overflow={overflow}>Failed to load.</DeskPage>;
  }

  const all = expensesQ.data;
  const exportPath = expensesExportPath(all);
  const exportOverflow = [
    {
      label: "Export CSV",
      onSelect: () => {
        if (exportPath) openApiDownload(exportPath);
      },
      disabledReason: exportPath ? undefined : "No visible expenses are available to export.",
    },
  ];
  const pending = all.filter((x) => x.state === "submitted");
  const approved = all.filter((x) => x.state === "approved");
  const rejected = all.filter((x) => x.state === "rejected");
  const reimbursed = all.filter((x) => x.state === "reimbursed");

  return (
    <DeskPage title="Expense approvals" sub={sub} overflow={exportOverflow}>
      <section className="grid grid--stats">
        <StatCard
          label="Needs decision"
          value={pending.length}
          sub={totalLabel(pending)}
          warn={pending.length > 0}
        />
        <StatCard
          label="Approved (this period)"
          value={approved.length}
          sub={totalLabel(approved) + " · pay out on payslip"}
        />
        <StatCard
          label="Reimbursed"
          value={reimbursed.length}
          sub="paid out via March payslip"
        />
        <StatCard label="Rejected (90d)" value={rejected.length} sub="," />
      </section>

      <div className="panel">
        <header className="panel__head">
          <h2>Pending · {pending.length}</h2>
          <span className="muted">Primary queue, work top to bottom.</span>
        </header>
        {decisionNotice !== null && (
          <output className="login__notice">
            {decisionNotice}
          </output>
        )}

        <ul className="approval-list approval-list--wide">
          {pending.length === 0 && (
            <li>
              <EmptyState
                icon={ReceiptText}
                title="Queue empty"
                copy="All submitted claims have been decided."
                variant="quiet"
              />
            </li>
          )}
          {pending.map((x) => {
            const cls = "approval" + (x.total_amount_cents >= 10000 ? " approval--medium" : "");
            const category = x.category || "other";
            // `submitted_at` is non-null for any row in the
            // `submitted` filter above, but TS can't narrow off a
            // discriminated state literal, guard inline so the chip
            // shows a sensible fallback if the server ever returns a
            // misaligned row (e.g. a draft slipped past the filter).
            return (
              <li key={x.id} className={cls}>
                <div className="approval__head">
                  <strong>{x.vendor}</strong>
                  <Chip tone="ghost" size="sm">{x.work_engagement_id}</Chip>
                  <span className="approval__time">
                    submitted <DateTime value={x.submitted_at} showTime empty="draft" />
                  </span>
                </div>

                <div className="expense-approval__grid">
                  <div className="expense-approval__amount">
                    <span className="expense-approval__value">{formatMoney(x.total_amount_cents, x.currency)}</span>
                    <span className="expense-approval__currency mono">{x.currency}</span>
                  </div>
                  <div className="expense-approval__body">
                    <p className="expense-approval__note">{x.note_md}</p>
                    <div className="expense-approval__meta">
                      <span>Category: <strong>{category}</strong></span>
                      <span>· Attaches to <strong>April 2026</strong> pay period</span>
                    </div>
                  </div>
                  <div className="expense-approval__receipt">
                    <div className="receipt-thumb" aria-hidden="true">
                      <Camera size={20} strokeWidth={1.6} />
                    </div>
                    <span className="muted mono">
                      {x.attachments.length === 0
                        ? "no receipt"
                        : x.attachments.length === 1
                          ? "receipt · 1 page"
                          : `${x.attachments.length} receipts`}
                    </span>
                  </div>
                </div>

                <div className="approval__actions">
                  <button
                    className="btn btn--moss"
                    type="button"
                    onClick={() => decide.mutate({ id: x.id, decision: "approve" })}
                  >
                    Approve
                  </button>
                  <button
                    className="btn btn--ghost"
                    type="button"
                    onClick={() => decide.mutate({ id: x.id, decision: "reject" })}
                  >
                    Reject with reason
                  </button>
                  <ExpenseCorrectionButton
                    expense={x}
                    onApproved={(updated) => {
                      setDecisionNotice(
                        `Approved corrected claim for ${updated.vendor}. `
                          + "The approval audit log records the before and after values.",
                      );
                    }}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      </div>

      <div className="panel">
        <header className="panel__head">
          <h2>Recent decisions</h2>
          <span className="muted">History, not actionable.</span>
        </header>
        <table className="table">
          <thead>
            <tr><th>Worker</th><th>Vendor</th><th>Amount</th><th>Submitted</th><th>State</th></tr>
          </thead>
          <tbody>
            {[...approved, ...reimbursed, ...rejected].length === 0 && (
              <tr>
                <td colSpan={5}>
                  <EmptyState
                    icon={ShieldCheck}
                    title="No decisions yet"
                    copy="Approved, reimbursed, and rejected claims will appear here."
                    variant="quiet"
                  />
                </td>
              </tr>
            )}
            {[...approved, ...reimbursed, ...rejected].map((x) => {
              // Already filtered to approved | reimbursed | rejected
              // above, but the cast narrows the literal so the tone
              // map look-up stays type-safe without a non-null
              // fallback branch.
              const state = x.state as Exclude<ExpenseStatus, "draft" | "submitted">;
              return (
                <tr key={x.id}>
                  <td className="mono">{x.work_engagement_id}</td>
                  <td>{x.vendor}<div className="table__sub">{x.note_md}</div></td>
                  <td className="mono">{formatMoney(x.total_amount_cents, x.currency)}</td>
                  <td><DateTime value={x.submitted_at} showTime className="mono" empty="," /></td>
                  <td><Chip tone={EXPENSE_STATUS_TONE[state]} size="sm">{x.state}</Chip></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
