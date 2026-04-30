import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { type ListEnvelope } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { formatMoney } from "@/lib/money";
import type { Me } from "@/types/api";

interface ClientBillableHoursRow {
  work_order_id: string;
  property_id: string;
  property_name: string;
  week_start: string;
  hours_decimal: string;
  total_cents: number;
  currency: string;
}

function billableMinutes(row: ClientBillableHoursRow): number {
  return Math.round(Number(row.hours_decimal) * 60);
}

function hourlyCents(row: ClientBillableHoursRow): number {
  const minutes = billableMinutes(row);
  if (minutes <= 0) return 0;
  return Math.round((row.total_cents * 60) / minutes);
}

export default function ClientBillableHoursPage() {
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const enabled = meQ.data?.role === "client";
  const billingQ = useQuery({
    queryKey: qk.bookingBillings("client-portal"),
    queryFn: async () => {
      const rows = await fetchJson<ListEnvelope<ClientBillableHoursRow>>(
        "/api/v1/client/billable-hours?limit=500",
      );
      return rows.data;
    },
    enabled,
  });

  if (meQ.isPending) {
    return <DeskPage title="Billable hours"><Loading /></DeskPage>;
  }
  if (!meQ.data) {
    return <DeskPage title="Billable hours">Failed to load.</DeskPage>;
  }
  if (meQ.data.role !== "client") {
    return (
      <DeskPage title="Billable hours">
        <div className="panel">
          <p className="muted">This page is only available to client portal users.</p>
        </div>
      </DeskPage>
    );
  }
  if (billingQ.isPending) {
    return <DeskPage title="Billable hours"><Loading /></DeskPage>;
  }
  if (!billingQ.data) {
    return <DeskPage title="Billable hours">Failed to load.</DeskPage>;
  }

  const rows = billingQ.data;
  const totalsByCurrency = rows.reduce<Record<string, number>>((acc, r) => {
    acc[r.currency] = (acc[r.currency] ?? 0) + r.total_cents;
    return acc;
  }, {});

  return (
    <DeskPage
      title="Billable hours"
      sub="What the agency has charged for work on your properties."
    >
      <section className="grid grid--stats">
        {Object.entries(totalsByCurrency).map(([ccy, total]) => (
          <div key={ccy} className="stat-card">
            <div className="stat-card__label">Total · {ccy}</div>
            <div className="stat-card__value">{formatMoney(total, ccy)}</div>
            <div className="stat-card__sub">
              {rows.filter((r) => r.currency === ccy).reduce((m, r) => m + billableMinutes(r), 0)} min
            </div>
          </div>
        ))}
      </section>

      <div className="panel">
        <header className="panel__head"><h2>Recent bookings</h2></header>
        {rows.length === 0 ? (
          <p className="muted">No bookings billed to you yet.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Client org</th>
                <th>Minutes</th>
                <th>Hourly</th>
                <th>Subtotal</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.work_order_id + ":" + r.property_id + ":" + r.week_start}>
                  <td>{r.property_name}</td>
                  <td className="table__mono">{billableMinutes(r)}</td>
                  <td className="table__mono">{formatMoney(hourlyCents(r), r.currency)}</td>
                  <td className="table__mono">{formatMoney(r.total_cents, r.currency)}</td>
                  <td>
                    <Chip size="sm" tone="ghost">
                      work order
                    </Chip>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </DeskPage>
  );
}
