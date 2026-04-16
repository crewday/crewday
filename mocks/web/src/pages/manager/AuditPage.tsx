import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type { AuditEntry } from "@/types/api";

const ACTOR_TONE: Record<AuditEntry["actor_kind"], "moss" | "sky" | "ghost" | "sand"> = {
  manager: "moss",
  employee: "sand",
  agent: "sky",
  system: "ghost",
};

function hms(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}
function dayMon(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

export default function AuditPage() {
  const q = useQuery({
    queryKey: qk.audit(),
    queryFn: () => fetchJson<AuditEntry[]>("/api/v1/audit"),
  });

  const sub = "Append-only. Every mutation by a manager, employee, agent, or system process.";
  const actions = <button className="btn btn--ghost">Export JSONL</button>;

  if (q.isPending) return <DeskPage title="Audit log" sub={sub} actions={actions}><Loading /></DeskPage>;
  if (!q.data) return <DeskPage title="Audit log" sub={sub} actions={actions}>Failed to load.</DeskPage>;

  const entries = q.data;
  const countBy = (kind: AuditEntry["actor_kind"]): number =>
    entries.filter((e) => e.actor_kind === kind).length;

  return (
    <DeskPage title="Audit log" sub={sub} actions={actions}>
      <section className="panel">
        <div className="desk-filters">
          <span className="chip chip--ghost chip--sm chip--active">All</span>
          <span className="chip chip--ghost chip--sm">Manager · {countBy("manager")}</span>
          <span className="chip chip--ghost chip--sm">Employee · {countBy("employee")}</span>
          <span className="chip chip--ghost chip--sm">Agent · {countBy("agent")}</span>
          <span className="chip chip--ghost chip--sm">System · {countBy("system")}</span>
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Via</th><th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e, idx) => (
              <tr key={idx}>
                <td className="mono">
                  {hms(e.at)}
                  <div className="table__sub">{dayMon(e.at)}</div>
                </td>
                <td>
                  <Chip tone={ACTOR_TONE[e.actor_kind]} size="sm">{e.actor_kind}</Chip>{" "}
                  {e.actor}
                </td>
                <td className="mono">{e.action}</td>
                <td className="mono muted">{e.target}</td>
                <td><Chip tone="ghost" size="sm">{e.via}</Chip></td>
                <td className="table__sub">{e.reason ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </DeskPage>
  );
}
