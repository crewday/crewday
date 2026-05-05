import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Checkbox, Chip, Loading } from "@/components/common";
import type { AdminMe, AdminTeamMember } from "@/types/api";

export default function AdminAdminsPage() {
  // code-health: ignore[nloc] Admin roster page is declarative query/form/table composition with shared primitives.
  const qc = useQueryClient();
  const meQ = useQuery({
    queryKey: qk.adminMe(),
    queryFn: () => fetchJson<AdminMe>("/admin/api/v1/me"),
  });
  const teamQ = useQuery({
    queryKey: qk.adminAdmins(),
    queryFn: () => fetchJson<AdminTeamMember[]>("/admin/api/v1/admins"),
  });
  const grant = useMutation({
    mutationFn: (body: { email: string; as_owner: boolean }) =>
      fetchJson<AdminTeamMember>("/admin/api/v1/admins", { method: "POST", body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.adminAdmins() }),
  });
  const revoke = useMutation({
    mutationFn: (id: string) =>
      fetchJson<{ ok: true }>(`/admin/api/v1/admins/${id}/revoke`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.adminAdmins() }),
  });

  const [email, setEmail] = useState("");
  const [asOwner, setAsOwner] = useState(false);

  const sub =
    "Members of owners@deployment and grant_role=admin grants. Only deployment owners can promote new owners.";

  if (teamQ.isPending || meQ.isPending) {
    return <DeskPage title="Admins" sub={sub}><Loading /></DeskPage>;
  }
  if (!teamQ.data) return <DeskPage title="Admins" sub={sub}>Failed to load.</DeskPage>;

  const isOwner = meQ.data?.is_owner ?? false;
  const team = teamQ.data;
  const owners = team.filter((m) => m.is_owner);

  return (
    <DeskPage title="Admins" sub={sub}>
      <div className="panel">
        <header className="panel__head">
          <h2>Deployment admin team ({team.length})</h2>
          <span className="muted">{owners.length} owner{owners.length === 1 ? "" : "s"}</span>
        </header>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Admin</th>
              <th>Email</th>
              <th>Role</th>
              <th>Granted</th>
              <th>Granted by</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {team.map((m) => (
              <tr key={m.id}>
                <td>{m.display_name}</td>
                <td className="mono">{m.email}</td>
                <td>
                  {m.is_owner
                    ? <Chip tone="moss" size="sm">owner</Chip>
                    : <Chip tone="sky" size="sm">admin</Chip>}
                </td>
                <td className="mono muted">{m.granted_at}</td>
                <td className="muted">{m.granted_by}</td>
                <td>
                  <button
                    type="button"
                    className="btn btn--rust btn--sm"
                    disabled={revoke.isPending || (m.is_owner && owners.length <= 1)}
                    onClick={() => {
                      if (confirm(`Revoke admin from ${m.display_name}?`)) {
                        revoke.mutate(m.id);
                      }
                    }}
                    title={m.is_owner && owners.length <= 1 ? "Cannot revoke the last owner" : undefined}
                  >
                    Revoke
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head"><h2>Grant admin</h2></header>
        <div className="form-grid form-grid--two">
          <label className="form-row">
            <span className="form-label">Email</span>
            <input
              type="email"
              className="input input--inline"
              placeholder="admin@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <div className="form-row">
            <span className="form-label">Promote to owner?</span>
            <Checkbox
              checked={asOwner}
              disabled={!isOwner}
              onChange={(e) => setAsOwner(e.target.checked)}
              label={
                isOwner
                  ? "Owners can archive workspaces and edit root-protected settings."
                  : "Only deployment owners may promote."
              }
            />
          </div>
        </div>
        <footer className="panel__foot">
          <button
            type="button"
            className="btn btn--moss"
            disabled={!email || grant.isPending}
            onClick={() => {
              grant.mutate({ email, as_owner: asOwner });
              setEmail("");
              setAsOwner(false);
            }}
          >
            Grant admin
          </button>
        </footer>
      </div>
    </DeskPage>
  );
}
