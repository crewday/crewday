import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import type { PendingReimbursement } from "@/types/api";

// §09 "Amount owed to the employee" — destination-currency total of
// approved-but-not-yet-reimbursed claims. Refreshes alongside the
// expenses list so the worker sees the number update the moment a
// claim is approved.
//
// The whole panel hides when there's nothing owed: the empty state
// (no row, no header) keeps the page calm for the common case where
// the worker has nothing pending.

export default function OwedToYou() {
  const pending = useQuery({
    queryKey: qk.expensesPendingReimbursement("me"),
    queryFn: () =>
      fetchJson<PendingReimbursement>(
        "/api/v1/expenses/pending_reimbursement?user_id=me",
      ),
  });

  if (!pending.data || pending.data.totals_by_currency.length === 0) {
    return null;
  }

  return (
    <section className="phone__section">
      <h2 className="section-title">Owed to you</h2>
      <p className="muted">
        Approved, paid out with your next payslip. Each amount is
        shown in the currency of the account where it will land.
      </p>
      <ul className="reimbursement-totals">
        {pending.data.totals_by_currency.map((t) => (
          <li key={t.currency} className="reimbursement-totals__row">
            <span className="reimbursement-totals__amount">
              {formatMoney(t.amount_cents, t.currency)}
            </span>
            <span className="reimbursement-totals__ccy">{t.currency}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
