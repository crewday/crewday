import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import { fmtDate } from "@/lib/dates";
import { Loading } from "@/components/common";
import type { Expense } from "@/types/api";
import { STATUS_TONE } from "./lib/expenseHelpers";

// "My recent expenses" — always visible below the form so the worker
// can see what the past week's claims look like (and their status)
// without leaving the page. Pending / approved / reimbursed all flow
// through the same list; the chip tone (`STATUS_TONE`) is the only
// thing that distinguishes them.

export default function RecentExpenses() {
  const q = useQuery({
    queryKey: qk.expenses("mine"),
    queryFn: () => fetchJson<Expense[]>("/api/v1/expenses?mine=true"),
  });

  return (
    <section className="phone__section">
      <h2 className="section-title">My recent expenses</h2>
      {q.isPending ? (
        <Loading />
      ) : q.isError || !q.data ? (
        <p className="muted">Failed to load.</p>
      ) : (
        <ul className="expense-list">
          {q.data.map((x) => {
            // Show the converted total only when the destination
            // currency differs — if the worker filed in EUR and is
            // paid in EUR, the second line is redundant noise.
            const converted =
              x.owed_currency &&
              x.owed_amount_cents != null &&
              x.owed_currency !== x.currency;
            return (
              <li key={x.id} className="expense-row">
                <div className="expense-row__main">
                  <strong>{x.merchant}</strong>
                  <span className="expense-row__note">{x.note}</span>
                  <span className="expense-row__time">
                    {fmtDate(x.submitted_at)}
                  </span>
                </div>
                <div className="expense-row__side">
                  <span className="expense-row__amount">
                    {formatMoney(x.amount_cents, x.currency)}
                  </span>
                  {converted && (
                    <span
                      className="expense-row__owed"
                      title={`Snapped at approval: 1 ${x.currency} = ${x.owed_exchange_rate} ${x.owed_currency} (${x.owed_rate_source})`}
                    >
                      {/* `owed_amount_cents` and `owed_currency` are
                          guaranteed non-null inside the `converted`
                          branch above, but TS narrows on each access
                          rather than on the boolean alias. */}
                      = {formatMoney(x.owed_amount_cents!, x.owed_currency!)}
                    </span>
                  )}
                  <span className={"chip chip--sm chip--" + STATUS_TONE[x.status]}>
                    {x.status}
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
