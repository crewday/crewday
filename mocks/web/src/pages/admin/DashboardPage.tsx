import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading, ProgressBar, StatCard } from "@/components/common";
import type {
  AdminUsageSummary,
  AdminWorkspaceRow,
  AuditEntry,
} from "@/types/api";

export default function AdminDashboardPage() {
  const summaryQ = useQuery({
    queryKey: qk.adminUsageSummary(),
    queryFn: () => fetchJson<AdminUsageSummary>("/admin/api/v1/usage/summary"),
  });
  const workspacesQ = useQuery({
    queryKey: qk.adminWorkspaces(),
    queryFn: () => fetchJson<AdminWorkspaceRow[]>("/admin/api/v1/workspaces"),
  });
  const auditQ = useQuery({
    queryKey: qk.adminAudit(),
    queryFn: () => fetchJson<AuditEntry[]>("/admin/api/v1/audit"),
  });

  const sub =
    "Deployment-wide health: spend, workspaces that need attention, and what changed recently.";

  if (summaryQ.isPending || workspacesQ.isPending || auditQ.isPending) {
    return <DeskPage title="Administration" sub={sub}><Loading /></DeskPage>;
  }
  if (!summaryQ.data || !workspacesQ.data || !auditQ.data) {
    return <DeskPage title="Administration" sub={sub}>Failed to load.</DeskPage>;
  }

  const sum = summaryQ.data;
  const workspaces = workspacesQ.data;
  const audit = auditQ.data.slice(0, 6);

  const active = workspaces.filter((w) => !w.archived_at);
  const paused = active.filter((w) => w.paused);
  const stressed = active
    .filter((w) => !w.paused && w.usage_percent >= 70)
    .sort((a, b) => b.usage_percent - a.usage_percent);

  return (
    <DeskPage title="Administration" sub={sub}>
      <section className="grid grid--stats">
        <StatCard
          label="30d LLM spend"
          value={formatMoney(Math.round(sum.deployment_spend_usd_30d * 100), "USD")}
          sub={sum.window_label}
        />
        <StatCard
          label="Workspaces"
          value={sum.workspace_count}
          sub={paused.length > 0 ? paused.length + " paused" : "all healthy"}
          warn={paused.length > 0}
        />
        <StatCard
          label="Calls (30d)"
          value={sum.deployment_call_count_30d.toLocaleString()}
          sub={"across " + sum.per_capability.length + " capabilities"}
        />
        <StatCard
          label="Default model"
          value="gemma-4-31b-it"
          sub="via OpenRouter"
        />
      </section>

      {(paused.length > 0 || stressed.length > 0) && (
        <div className="panel">
          <header className="panel__head">
            <h2>Workspaces to watch</h2>
            <Link className="btn btn--ghost" to="/admin/usage">Open Usage</Link>
          </header>
          <table className="table">
            <thead>
              <tr>
                <th>Workspace</th>
                <th>State</th>
                <th>30d spend</th>
                <th>Cap</th>
                <th>Usage</th>
              </tr>
            </thead>
            <tbody>
              {[...paused, ...stressed].map((w) => (
                <tr key={w.id}>
                  <td>
                    <Link to={"/admin/workspaces"} className="table__link">
                      {w.name}
                    </Link>
                    <div className="table__sub">{w.slug}</div>
                  </td>
                  <td>
                    {w.paused
                      ? <Chip tone="rust" size="sm">paused</Chip>
                      : <Chip tone="sand" size="sm">{w.usage_percent}%</Chip>}
                  </td>
                  <td className="mono">{formatMoney(Math.round(w.spent_usd_30d * 100), "USD")}</td>
                  <td className="mono">{formatMoney(Math.round(w.cap_usd_30d * 100), "USD")}</td>
                  <td>
                    <ProgressBar value={w.usage_percent} slim />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="panel">
        <header className="panel__head">
          <h2>Recent deployment audit</h2>
          <Link className="btn btn--ghost" to="/admin/audit">Open Audit log</Link>
        </header>
        <table className="table">
          <thead>
            <tr>
              <th>When</th>
              <th>Actor</th>
              <th>Action</th>
              <th>Target</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {audit.map((row, idx) => (
              <tr key={idx}>
                <td className="mono">{new Date(row.at).toLocaleString()}</td>
                <td>{row.actor}</td>
                <td><code className="inline-code">{row.action}</code></td>
                <td className="mono">{row.target}</td>
                <td className="muted">{row.reason ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
