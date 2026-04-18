import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading, ProgressBar, StatCard } from "@/components/common";
import type { AdminUsageSummary, AdminWorkspaceRow } from "@/types/api";

export default function AdminUsagePage() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const [draftCap, setDraftCap] = useState<string>("");

  const summaryQ = useQuery({
    queryKey: qk.adminUsageSummary(),
    queryFn: () => fetchJson<AdminUsageSummary>("/admin/api/v1/usage/summary"),
  });
  const rowsQ = useQuery({
    queryKey: qk.adminUsageWorkspaces(),
    queryFn: () => fetchJson<AdminWorkspaceRow[]>("/admin/api/v1/usage/workspaces"),
  });

  const setCap = useMutation({
    mutationFn: ({ id, cap }: { id: string; cap: number }) =>
      fetchJson<AdminWorkspaceRow>(`/admin/api/v1/usage/workspaces/${id}/cap`, {
        method: "PUT",
        body: { cap_usd_30d: cap },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.adminUsageWorkspaces() });
      qc.invalidateQueries({ queryKey: qk.adminUsageSummary() });
      qc.invalidateQueries({ queryKey: qk.adminWorkspaces() });
      setEditing(null);
      setDraftCap("");
    },
  });

  const sub =
    "Rolling-30-day LLM spend per workspace. Adjust a workspace's cap to raise or tighten its envelope.";

  if (summaryQ.isPending || rowsQ.isPending) {
    return <DeskPage title="Usage" sub={sub}><Loading /></DeskPage>;
  }
  if (!summaryQ.data || !rowsQ.data) {
    return <DeskPage title="Usage" sub={sub}>Failed to load.</DeskPage>;
  }

  const sum = summaryQ.data;
  const rows = rowsQ.data;

  return (
    <DeskPage title="Usage" sub={sub}>
      <section className="grid grid--stats">
        <StatCard
          label="30d spend"
          value={formatMoney(Math.round(sum.deployment_spend_usd_30d * 100), "USD")}
          sub={sum.window_label}
        />
        <StatCard
          label="Workspaces"
          value={sum.workspace_count}
          sub={sum.paused_workspaces + " paused"}
          warn={sum.paused_workspaces > 0}
        />
        <StatCard
          label="Calls (30d)"
          value={sum.deployment_call_count_30d.toLocaleString()}
        />
        <StatCard
          label="Top capability"
          value={sum.per_capability[0]?.capability ?? "—"}
          sub={
            sum.per_capability[0]
              ? formatMoney(Math.round(sum.per_capability[0].spend_usd_30d * 100), "USD")
              : undefined
          }
        />
      </section>

      <div className="panel">
        <header className="panel__head"><h2>Per workspace</h2></header>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Workspace</th>
              <th>Plan</th>
              <th>Verification</th>
              <th>30d spend</th>
              <th>Cap</th>
              <th>Usage</th>
              <th>State</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((w) => (
              <tr key={w.id}>
                <td>
                  {w.name}
                  <div className="table__sub">{w.slug}</div>
                </td>
                <td><Chip tone={w.plan === "free" ? "ghost" : "sky"} size="sm">{w.plan}</Chip></td>
                <td className="muted">{w.verification_state}</td>
                <td className="mono">{formatMoney(Math.round(w.spent_usd_30d * 100), "USD")}</td>
                <td>
                  {editing === w.id ? (
                    <input
                      className="input input--inline"
                      type="number"
                      step="0.5"
                      min="0"
                      max="10000"
                      value={draftCap}
                      onChange={(e) => setDraftCap(e.target.value)}
                      autoFocus
                    />
                  ) : (
                    <span className="mono">{formatMoney(Math.round(w.cap_usd_30d * 100), "USD")}</span>
                  )}
                </td>
                <td>
                  <ProgressBar value={w.usage_percent} slim />
                  <span className="muted"> {w.usage_percent}%</span>
                </td>
                <td>
                  {w.paused
                    ? <Chip tone="rust" size="sm">paused</Chip>
                    : <Chip tone="moss" size="sm">active</Chip>}
                </td>
                <td>
                  {editing === w.id ? (
                    <div className="inline-actions">
                      <button
                        type="button"
                        className="btn btn--moss btn--sm"
                        disabled={setCap.isPending || !draftCap}
                        onClick={() => setCap.mutate({ id: w.id, cap: Number(draftCap) })}
                      >
                        Save
                      </button>
                      <button
                        type="button"
                        className="btn btn--ghost btn--sm"
                        onClick={() => {
                          setEditing(null);
                          setDraftCap("");
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      className="btn btn--ghost btn--sm"
                      onClick={() => {
                        setEditing(w.id);
                        setDraftCap(w.cap_usd_30d.toString());
                      }}
                    >
                      Edit cap
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head"><h2>Per capability (30d)</h2></header>
        <table className="table">
          <thead>
            <tr>
              <th>Capability</th><th>Calls</th><th>Spend</th>
            </tr>
          </thead>
          <tbody>
            {sum.per_capability
              .slice()
              .sort((a, b) => b.spend_usd_30d - a.spend_usd_30d)
              .map((c) => (
                <tr key={c.capability}>
                  <td><code className="inline-code">{c.capability}</code></td>
                  <td className="mono">{c.calls_30d.toLocaleString()}</td>
                  <td className="mono">
                    {formatMoney(Math.round(c.spend_usd_30d * 100), "USD")}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
