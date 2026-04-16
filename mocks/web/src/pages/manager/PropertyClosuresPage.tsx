import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type { Me, Property, PropertyClosure, Stay } from "@/types/api";

interface ClosuresPayload {
  property: Property;
  closures: PropertyClosure[];
  stays: Stay[];
}

function fmtDayMon(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

export default function PropertyClosuresPage() {
  const { pid = "" } = useParams<{ pid: string }>();
  const dataQ = useQuery({
    queryKey: qk.propertyClosures(pid),
    queryFn: () =>
      fetchJson<ClosuresPayload>("/api/v1/property_closures?property_id=" + pid),
    enabled: pid !== "",
  });
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });

  if (dataQ.isPending || meQ.isPending) {
    return <DeskPage title="Closures"><Loading /></DeskPage>;
  }
  if (!dataQ.data || !meQ.data) {
    return <DeskPage title="Closures">Failed to load.</DeskPage>;
  }

  const { property, closures, stays } = dataQ.data;
  const today = new Date(meQ.data.today);
  const todayDay = today.getDate();

  const days: number[] = [];
  for (let d = 1; d <= 30; d += 1) days.push(d);

  return (
    <DeskPage
      title={property.name + " — closures"}
      sub={
        <>
          <Link to={"/property/" + property.id} className="link">← Back to property</Link>{" "}
          · iCal "Not available" / "Blocked" events upsert here automatically.
        </>
      }
      actions={<button className="btn btn--moss">+ Add closure</button>}
    >
      <div className="panel">
        <table className="table table--roomy">
          <thead>
            <tr><th>Dates</th><th>Reason</th><th>Note</th><th>Source</th><th></th></tr>
          </thead>
          <tbody>
            {closures.length === 0 ? (
              <tr>
                <td colSpan={5} className="empty-state empty-state--quiet">
                  No closures scheduled.
                </td>
              </tr>
            ) : (
              closures.map((c) => {
                const ical = c.reason === "ical_unavailable";
                return (
                  <tr key={c.id}>
                    <td className="mono">
                      {fmtDayMon(c.starts_on)} → {fmtDayMon(c.ends_on)}
                    </td>
                    <td>
                      <Chip tone={ical ? "sky" : "ghost"} size="sm">{c.reason}</Chip>
                    </td>
                    <td className="table__sub">{c.note}</td>
                    <td>
                      {ical ? (
                        <Chip tone="sky" size="sm">Airbnb / VRBO</Chip>
                      ) : (
                        <Chip tone="ghost" size="sm">manual</Chip>
                      )}
                    </td>
                    <td>
                      {ical ? (
                        <span className="muted">Read-only — edit in Airbnb / VRBO</span>
                      ) : (
                        <button className="btn btn--sm btn--ghost">Edit</button>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head">
          <h2>Calendar view</h2>
          <span className="muted">April 2026</span>
        </header>
        <div className="mini-cal mini-cal--wide">
          {days.map((d) => {
            let closed = false;
            let reason: PropertyClosure["reason"] | null = null;
            for (const c of closures) {
              const cs = new Date(c.starts_on).getDate();
              const ce = new Date(c.ends_on).getDate();
              if (cs <= d && d <= ce) {
                closed = true;
                reason = c.reason;
              }
            }
            const cls = [
              "mini-cal__day",
              closed ? "mini-cal__day--closed" : "",
              d === todayDay ? "mini-cal__day--today" : "",
            ]
              .filter(Boolean)
              .join(" ");
            return (
              <div key={d} className={cls}>
                <span className="mini-cal__num">{d}</span>
                {closed && (
                  <span
                    className="mini-cal__bar mini-cal__bar--closed"
                    title={reason ?? undefined}
                  />
                )}
                {stays.map((s) => {
                  const ci = new Date(s.check_in).getDate();
                  const co = new Date(s.check_out).getDate();
                  if (ci <= d && d <= co) {
                    return (
                      <span
                        key={s.id}
                        className={"mini-cal__bar mini-cal__bar--" + property.color}
                        title={s.guest + " (" + s.source + ")"}
                      />
                    );
                  }
                  return null;
                })}
              </div>
            );
          })}
        </div>
      </div>
    </DeskPage>
  );
}
