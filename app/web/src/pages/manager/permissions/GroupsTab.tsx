import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Chip, Loading } from "@/components/common";
import type { ListEnvelope } from "@/lib/listResponse";
import type {
  PermissionGroup,
  PermissionGroupMember,
} from "@/types/api";
import { useUsersIndex, useWorkspaces } from "./lib/usePermissionIndexes";

// cd-c83ap restores the derived affordances removed in cd-5bz65 once
// the production router started carrying `group_kind` again.
function isDerivedGroup(group: PermissionGroup): boolean {
  return group.group_kind === "derived";
}

export default function GroupsTab() {
  // code-health: ignore[ccn nloc] Permission group editor keeps nested role/user grant controls in one table surface.
  const wss = useWorkspaces();
  const users = useUsersIndex();
  const [workspaceId, setWorkspaceId] = useState<string>("");
  const effectiveWs = workspaceId || wss.data?.[0]?.workspace_id || "";

  const groups = useQuery({
    queryKey: qk.permissionGroups("workspace", effectiveWs),
    queryFn: () =>
      fetchJson<ListEnvelope<PermissionGroup>>(
        `/api/v1/permission_groups?scope_kind=workspace&scope_id=${encodeURIComponent(effectiveWs)}`,
      ),
    enabled: !!effectiveWs,
  });

  const [selected, setSelected] = useState<string>("");
  const selectedId = selected || groups.data?.data[0]?.id || "";

  const members = useQuery({
    queryKey: qk.permissionGroupMembers(selectedId),
    queryFn: () =>
      fetchJson<ListEnvelope<PermissionGroupMember>>(
        `/api/v1/permission_groups/${encodeURIComponent(selectedId)}/members`,
      ),
    enabled: !!selectedId,
  });

  if (wss.isPending || groups.isPending) return <Loading />;
  if (!wss.data || !groups.data) return <div>Failed to load.</div>;

  const groupRows = groups.data.data;
  const selectedGroup = groupRows.find((g) => g.id === selectedId);
  const selectedIsDerived = selectedGroup ? isDerivedGroup(selectedGroup) : false;

  return (
    <div className="permissions__split">
      <section className="panel permissions__groups">
        <header className="panel__header">
          <label className="field">
            <span>Workspace</span>
            <select value={effectiveWs} onChange={(e) => setWorkspaceId(e.target.value)}>
              {wss.data.map((w) => (
                <option key={w.workspace_id} value={w.workspace_id}>
                  {w.name}
                </option>
              ))}
            </select>
          </label>
        </header>
        <ul className="permissions__group-list">
          {groupRows.map((g) => (
            <li
              key={g.id}
              className={`permissions__group-row ${selectedId === g.id ? "permissions__group-row--active" : ""}`}
              onClick={() => setSelected(g.id)}
            >
              <div className="permissions__group-name">
                {g.name}
                {g.system ? (
                  <Chip tone="moss" size="sm">system</Chip>
                ) : null}
                {isDerivedGroup(g) ? (
                  <Chip tone="sand" size="sm">derived</Chip>
                ) : null}
              </div>
              <div className="permissions__group-key mono muted">{g.slug}</div>
            </li>
          ))}
          {groupRows.length === 0 ? (
            <li className="permissions__group-row muted">No groups.</li>
          ) : null}
        </ul>
      </section>

      <section className="panel permissions__members">
        {selectedGroup ? (
          <>
            <header className="panel__header">
              <h3>{selectedGroup.name}</h3>
              <div className="muted mono">{selectedGroup.slug}</div>
            </header>
            {members.isPending ? (
              <Loading />
            ) : members.data ? (
              <>
                {selectedGroup.slug === "owners" ? (
                  <p className="muted">
                    <strong>Governance anchor.</strong> Owners can always
                    perform root-only actions; the group must have ≥1
                    active member at all times. Adding or removing members
                    requires the{" "}
                    <code>groups.manage_owners_membership</code> action.
                  </p>
                ) : null}
                {selectedIsDerived ? (
                  <p className="muted">
                    Auto-populated from role_grants. Membership is read-only
                    here; grant or revoke the matching surface role instead.
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
                    {members.data.data.map((m) => {
                      const u = users.data?.[m.user_id];
                      return (
                        <tr key={m.user_id}>
                          <td>{u?.display_name ?? m.user_id}</td>
                          <td className="mono muted">{u?.email ?? ""}</td>
                          <td>
                            {selectedIsDerived ? (
                              <span className="muted">read-only</span>
                            ) : (
                              <button className="btn btn--ghost btn--sm">Remove</button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                    {members.data.data.length === 0 ? (
                      <tr>
                        <td colSpan={3} className="muted">
                          {selectedIsDerived ? "No matching role grants." : "No members."}
                        </td>
                      </tr>
                    ) : null}
                  </tbody>
                </table>
                {selectedIsDerived ? null : (
                  <div className="panel__footer">
                    <button className="btn btn--moss btn--sm">+ Add member</button>
                  </div>
                )}
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
