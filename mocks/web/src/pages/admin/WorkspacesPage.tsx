import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type { AdminWorkspaceRow } from "@/types/api";

const VERIFICATION_TONE: Record<AdminWorkspaceRow["verification_state"], "moss" | "sky" | "sand" | "ghost"> = {
  trusted: "moss",
  human_verified: "sky",
  email_verified: "sand",
  unverified: "ghost",
};

export default function AdminWorkspacesPage() {
  const qc = useQueryClient();
  const wsQ = useQuery({
    queryKey: qk.adminWorkspaces(),
    queryFn: () => fetchJson<AdminWorkspaceRow[]>("/admin/api/v1/workspaces"),
  });

  const trust = useMutation({
    mutationFn: (id: string) =>
      fetchJson<AdminWorkspaceRow>(`/admin/api/v1/workspaces/${id}/trust`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.adminWorkspaces() }),
  });
  const archive = useMutation({
    mutationFn: (id: string) =>
      fetchJson<AdminWorkspaceRow>(`/admin/api/v1/workspaces/${id}/archive`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.adminWorkspaces() }),
  });

  const sub =
    "Every workspace on this deployment. Promote verification, archive on owner request, or drill into usage.";

  if (wsQ.isPending) return <DeskPage title="Workspaces" sub={sub}><Loading /></DeskPage>;
  if (!wsQ.data) return <DeskPage title="Workspaces" sub={sub}>Failed to load.</DeskPage>;

  const active = wsQ.data.filter((w) => !w.archived_at);
  const archived = wsQ.data.filter((w) => w.archived_at);

  return (
    <DeskPage title="Workspaces" sub={sub}>
      <div className="panel">
        <header className="panel__head"><h2>Active ({active.length})</h2></header>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Workspace</th>
              <th>Plan</th>
              <th>Verification</th>
              <th>Properties</th>
              <th>Members</th>
              <th>30d spend</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {active.map((w) => (
              <tr key={w.id}>
                <td>
                  {w.name}
                  <div className="table__sub">/w/{w.slug}</div>
                </td>
                <td><Chip tone={w.plan === "free" ? "ghost" : "sky"} size="sm">{w.plan}</Chip></td>
                <td>
                  <Chip tone={VERIFICATION_TONE[w.verification_state]} size="sm">
                    {w.verification_state}
                  </Chip>
                </td>
                <td className="mono">{w.properties_count}</td>
                <td className="mono">{w.members_count}</td>
                <td className="mono">
                  {formatMoney(Math.round(w.spent_usd_30d * 100), "USD")}
                  <span className="muted"> / {formatMoney(Math.round(w.cap_usd_30d * 100), "USD")}</span>
                </td>
                <td className="mono muted">{w.created_at}</td>
                <td>
                  <div className="inline-actions">
                    {w.verification_state !== "trusted" && (
                      <button
                        type="button"
                        className="btn btn--ghost btn--sm"
                        disabled={trust.isPending}
                        onClick={() => trust.mutate(w.id)}
                      >
                        Trust
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn btn--rust btn--sm"
                      disabled={archive.isPending}
                      onClick={() => {
                        if (confirm(`Archive ${w.name}? Owner can restore from backup.`)) {
                          archive.mutate(w.id);
                        }
                      }}
                    >
                      Archive
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {archived.length > 0 && (
        <div className="panel">
          <header className="panel__head"><h2>Archived ({archived.length})</h2></header>
          <table className="table">
            <thead>
              <tr>
                <th>Workspace</th>
                <th>Plan</th>
                <th>Archived on</th>
              </tr>
            </thead>
            <tbody>
              {archived.map((w) => (
                <tr key={w.id}>
                  <td>
                    {w.name}
                    <div className="table__sub">/w/{w.slug}</div>
                  </td>
                  <td className="muted">{w.plan}</td>
                  <td className="mono muted">{w.archived_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </DeskPage>
  );
}
