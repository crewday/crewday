import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Chip, Loading } from "@/components/common";
import type {
  ActionCatalogEntry,
  PermissionGroup,
  PermissionRule,
} from "@/types/api";
import { useUsersIndex, useWorkspaces } from "./lib/usePermissionIndexes";
import RuleChip from "./RuleChip";
import WhoCanDoThis from "./WhoCanDoThis";

export default function RulesTab() {
  const wss = useWorkspaces();
  const users = useUsersIndex();
  const [workspaceId, setWorkspaceId] = useState<string>("");
  const effectiveWs = workspaceId || wss.data?.[0]?.id || "";

  const catalog = useQuery({
    queryKey: qk.actionCatalog(),
    queryFn: () => fetchJson<ActionCatalogEntry[]>("/api/v1/permissions/action_catalog"),
  });

  const rules = useQuery({
    queryKey: qk.permissionRules("workspace", effectiveWs),
    queryFn: () =>
      fetchJson<PermissionRule[]>(
        `/api/v1/permission_rules?scope_kind=workspace&scope_id=${encodeURIComponent(effectiveWs)}`,
      ),
    enabled: !!effectiveWs,
  });

  const groups = useQuery({
    queryKey: qk.permissionGroups("workspace", effectiveWs),
    queryFn: () =>
      fetchJson<PermissionGroup[]>(
        `/api/v1/permission_groups?scope_kind=workspace&scope_id=${encodeURIComponent(effectiveWs)}`,
      ),
    enabled: !!effectiveWs,
  });

  const groupsById = useMemo(() => {
    return Object.fromEntries((groups.data ?? []).map((g) => [g.id, g]));
  }, [groups.data]);

  if (wss.isPending || catalog.isPending || rules.isPending) return <Loading />;
  if (!wss.data || !catalog.data || !rules.data) return <div>Failed to load.</div>;

  const rulesByAction: Record<string, PermissionRule[]> = {};
  for (const r of rules.data) {
    const bucket = rulesByAction[r.action_key] ?? [];
    bucket.push(r);
    rulesByAction[r.action_key] = bucket;
  }

  return (
    <>
      <section className="panel">
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

        <WhoCanDoThis
          users={Object.values(users.data ?? {})}
          actions={catalog.data}
          scopeKind="workspace"
          scopeId={effectiveWs}
        />
      </section>

      <section className="panel">
        <table className="table table--roomy permissions__rules">
          <thead>
            <tr>
              <th>Action</th>
              <th>Default (if no rule matches)</th>
              <th>Active rules on this workspace</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {catalog.data.map((entry) => {
              const rs = rulesByAction[entry.key] ?? [];
              return (
                <tr key={entry.key}>
                  <td>
                    <div className="mono">{entry.key}</div>
                    <div className="table__sub">{entry.description}</div>
                    <div>
                      {entry.root_only ? (
                        <Chip tone="rust" size="sm">owners only</Chip>
                      ) : null}
                      {entry.root_protected_deny ? (
                        <Chip tone="sand" size="sm">owners immune to deny</Chip>
                      ) : null}
                    </div>
                  </td>
                  <td>
                    {entry.default_allow.length === 0 ? (
                      <span className="muted">no default</span>
                    ) : (
                      entry.default_allow.map((k) => (
                        <Chip key={k} tone="moss" size="sm">{k}</Chip>
                      ))
                    )}
                  </td>
                  <td>
                    {rs.length === 0 ? (
                      <span className="muted">— default applies —</span>
                    ) : (
                      rs.map((r) => (
                        <RuleChip
                          key={r.id}
                          rule={r}
                          groupLabel={groupsById[r.subject_id]?.name}
                          userLabel={users.data?.[r.subject_id]?.display_name}
                        />
                      ))
                    )}
                  </td>
                  <td>
                    {entry.root_only ? (
                      <span className="muted">—</span>
                    ) : (
                      <button className="btn btn--ghost btn--sm">+ Rule</button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </>
  );
}
