import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Chip, Loading } from "@/components/common";
import type {
  PermissionGroup,
  PermissionGroupMembersResponse,
} from "@/types/api";
import { useUsersIndex, useWorkspaces } from "./lib/usePermissionIndexes";

export default function GroupsTab() {
  const wss = useWorkspaces();
  const users = useUsersIndex();
  const [workspaceId, setWorkspaceId] = useState<string>("");
  const effectiveWs = workspaceId || wss.data?.[0]?.id || "";

  const groups = useQuery({
    queryKey: qk.permissionGroups("workspace", effectiveWs),
    queryFn: () =>
      fetchJson<PermissionGroup[]>(
        `/api/v1/permission_groups?scope_kind=workspace&scope_id=${encodeURIComponent(effectiveWs)}`,
      ),
    enabled: !!effectiveWs,
  });

  const [selected, setSelected] = useState<string>("");
  const selectedId = selected || groups.data?.[0]?.id || "";

  const members = useQuery({
    queryKey: qk.permissionGroupMembers(selectedId),
    queryFn: () =>
      fetchJson<PermissionGroupMembersResponse>(
        `/api/v1/permission_groups/${encodeURIComponent(selectedId)}/members`,
      ),
    enabled: !!selectedId,
  });

  if (wss.isPending || groups.isPending) return <Loading />;
  if (!wss.data || !groups.data) return <div>Failed to load.</div>;

  const selectedGroup = groups.data.find((g) => g.id === selectedId);

  return (
    <div className="permissions__split">
      <section className="panel permissions__groups">
        <header className="panel__header">
          <label className="field">
            <span>Workspace</span>
            <select value={effectiveWs} onChange={(e) => setWorkspaceId(e.target.value)}>
              {wss.data.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </select>
          </label>
        </header>
        <ul className="permissions__group-list">
          {groups.data.map((g) => (
            <li
              key={g.id}
              className={`permissions__group-row ${selectedId === g.id ? "permissions__group-row--active" : ""}`}
              onClick={() => setSelected(g.id)}
            >
              <div className="permissions__group-name">
                {g.name}
                {g.group_kind === "system" ? (
                  <Chip tone="moss" size="sm">system</Chip>
                ) : null}
                {g.is_derived ? <Chip tone="ghost" size="sm">derived</Chip> : null}
              </div>
              <div className="permissions__group-key mono muted">{g.key}</div>
            </li>
          ))}
        </ul>
      </section>

      <section className="panel permissions__members">
        {selectedGroup ? (
          <>
            <header className="panel__header">
              <h3>{selectedGroup.name}</h3>
              <div className="muted">{selectedGroup.description_md || "—"}</div>
            </header>
            {members.isPending ? (
              <Loading />
            ) : members.data ? (
              <>
                {members.data.is_derived ? (
                  <p className="muted">
                    Auto-populated from role_grants on this scope. Add or
                    remove members by editing the underlying grant.
                  </p>
                ) : null}
                {selectedGroup.key === "owners" ? (
                  <p className="muted">
                    <strong>Governance anchor.</strong> Owners can always
                    perform root-only actions; the group must have ≥1
                    active member at all times. Adding or removing members
                    requires the{" "}
                    <code>groups.manage_owners_membership</code> action.
                  </p>
                ) : null}
                <table className="table">
                  <thead>
                    <tr>
                      <th>User</th>
                      <th>Email</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {members.data.members.map((m) => {
                      const u = users.data?.[m.user_id];
                      return (
                        <tr key={m.user_id}>
                          <td>{u?.display_name ?? m.user_id}</td>
                          <td className="mono muted">{u?.email ?? ""}</td>
                          <td>
                            {selectedGroup.is_derived ? (
                              <Chip tone="ghost" size="sm">derived</Chip>
                            ) : (
                              <button className="btn btn--ghost btn--sm">Remove</button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                    {members.data.members.length === 0 ? (
                      <tr>
                        <td colSpan={3} className="muted">
                          No members.
                        </td>
                      </tr>
                    ) : null}
                  </tbody>
                </table>
                {!selectedGroup.is_derived ? (
                  <div className="panel__footer">
                    <button className="btn btn--moss btn--sm">+ Add member</button>
                  </div>
                ) : null}
              </>
            ) : (
              <div>Failed to load members.</div>
            )}
          </>
        ) : (
          <p className="muted">Pick a group.</p>
        )}
      </section>
    </div>
  );
}
