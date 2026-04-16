import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Loading } from "@/components/common";
import type { Employee, Me } from "@/types/api";

interface ShiftHistoryRow {
  date: string;
  in: string;
  out: string;
  hours: string;
}

// Hardcoded history list — matches the original Jinja mock. When the
// real endpoint arrives, swap this for a query.
const HISTORY: ShiftHistoryRow[] = [
  { date: "Mon 07 Apr", in: "08:02", out: "16:12", hours: "8h 10m" },
  { date: "Sat 05 Apr", in: "09:01", out: "13:30", hours: "4h 29m" },
  { date: "Fri 04 Apr", in: "08:10", out: "17:05", hours: "8h 55m" },
  { date: "Thu 03 Apr", in: "08:05", out: "16:55", hours: "8h 50m" },
];

function hhmm(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export default function ShiftsPage() {
  const qc = useQueryClient();
  const me = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });

  const toggle = useMutation({
    mutationFn: () =>
      fetchJson<Employee>("/api/v1/shifts/toggle", { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.me() });
      qc.invalidateQueries({ queryKey: qk.today() });
    },
  });

  if (me.isPending) return <section className="phone__section"><Loading /></section>;
  if (me.isError || !me.data) {
    return <section className="phone__section"><p className="muted">Failed to load.</p></section>;
  }

  const { employee } = me.data;
  const clockedIn = Boolean(employee.clocked_in_at);
  const cardCls = "shift-card" + (clockedIn ? " shift-card--active" : "");

  return (
    <>
      <section className="phone__section">
        <h2 className="section-title">Shift</h2>
        <div className={cardCls}>
          {clockedIn ? (
            <>
              <div className="shift-card__state">
                You clocked in at {hhmm(employee.clocked_in_at!)}
              </div>
              <div className="shift-card__dur">2h 00m on shift</div>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  toggle.mutate();
                }}
              >
                <button className="btn btn--rust btn--lg" type="submit">Clock out</button>
              </form>
            </>
          ) : (
            <>
              <div className="shift-card__state">Not clocked in</div>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  toggle.mutate();
                }}
              >
                <button className="btn btn--moss btn--lg" type="submit">Clock in now</button>
              </form>
            </>
          )}
        </div>
      </section>

      <section className="phone__section">
        <h2 className="section-title">Recent shifts</h2>
        <table className="table table--compact">
          <thead>
            <tr>
              <th>Date</th>
              <th>In</th>
              <th>Out</th>
              <th>Hours</th>
            </tr>
          </thead>
          <tbody>
            {HISTORY.map((row) => (
              <tr key={row.date}>
                <td>{row.date}</td>
                <td>{row.in}</td>
                <td>{row.out}</td>
                <td>{row.hours}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
