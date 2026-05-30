import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Chip, Loading } from "@/components/common";
import type { ListEnvelope } from "@/lib/listResponse";
import type {
  ActionCatalogEntry,
  PermissionGroup,
  PermissionRule,
} from "@/types/api";
import { useActiveWorkspaceScope, useUsersIndex } from "./lib/usePermissionIndexes";
import RuleChip from "./RuleChip";
import WhoCanDoThis from "./WhoCanDoThis";

interface ActionCatalogResponse {
  entries: ActionCatalogEntry[];
  count: number;
}

export default function RulesTab() {
  // code-health: ignore[ccn nloc] Permission rules tab is a compact editor for one rule list and its grant matrix.
  const activeWorkspace = useActiveWorkspaceScope();
  const users = useUsersIndex();
  const effectiveWs = activeWorkspace.workspaceId;

  const catalog = useQuery({
    queryKey: qk.actionCatalog(),
    queryFn: () =>
      fetchJson<ActionCatalogResponse>("/api/v1/permissions/action_catalog"),
    enabled: !!effectiveWs,
  });

  const rules = useQuery({
    queryKey: qk.permissionRules("workspace", effectiveWs),
    queryFn: () =>
      fetchJson<ListEnvelope<PermissionRule>>(
        `/api/v1/permission_rules?scope_kind=workspace&scope_id=${encodeURIComponent(effectiveWs)}`,
      ),
    enabled: !!effectiveWs,
  });

  const groups = useQuery({
    queryKey: qk.permissionGroups("workspace", effectiveWs),
    queryFn: () =>
      fetchJson<ListEnvelope<PermissionGroup>>(
        `/api/v1/permission_groups?scope_kind=workspace&scope_id=${encodeURIComponent(effectiveWs)}`,
      ),
    enabled: !!effectiveWs,
  });

  const groupsById = useMemo(() => {
    return Object.fromEntries((groups.data?.data ?? []).map((g) => [g.id, g]));
  }, [groups.data]);

  if (
    activeWorkspace.isPending ||
    (effectiveWs && (catalog.isPending || rules.isPending))
  ) return <Loading />;
  if (activeWorkspace.isError) return <div>Failed to load active workspace.</div>;
  if (!effectiveWs) {
    return (
      <>
        <section className="panel">
          <header className="panel__head permissions__section-head">
            <div className="panel__head-stack">
              <h2>Permission resolver</h2>
              <p className="panel__sub">Preview a user, action, and scope before editing rules.</p>
            </div>
          </header>
          <p className="muted permissions__empty">No active workspace selected.</p>
        </section>
        <section className="panel">
          <header className="panel__head permissions__section-head">
            <div className="panel__head-stack">
              <h2>Rule matrix</h2>
              <p className="panel__sub">Defaults, owner protections, and active workspace rules.</p>
            </div>
          </header>
          <p className="muted">Pick a workspace from the shell to inspect rules.</p>
        </section>
      </>
    );
  }
  if (!catalog.data || !rules.data) return <div>Failed to load.</div>;

  const ruleRows = rules.data.data;
  const catalogRows = catalog.data.entries;
  const rulesByAction: Record<string, PermissionRule[]> = {};
  for (const r of ruleRows) {
    const bucket = rulesByAction[r.action_key] ?? [];
    bucket.push(r);
    rulesByAction[r.action_key] = bucket;
  }

  return (
    <>
      <section className="panel">
        <header className="panel__head permissions__section-head">
          <div className="panel__head-stack">
            <h2>Permission resolver</h2>
            <p className="panel__sub">Preview a user, action, and scope before editing rules.</p>
          </div>
        </header>

        <WhoCanDoThis
          users={Object.values(users.data ?? {})}
          actions={catalogRows}
          scopeKind="workspace"
          scopeId={effectiveWs}
        />
      </section>

      <section className="panel">
        <header className="panel__head permissions__section-head">
          <div className="panel__head-stack">
            <h2>Rule matrix</h2>
            <p className="panel__sub">Defaults, owner protections, and active workspace rules.</p>
          </div>
        </header>
        {ruleRows.length === 0 ? (
          <p className="muted">
            No rules on this workspace, defaults apply for every action below.
          </p>
        ) : null}
        <table className="table table--roomy permissions__rules">
          <thead>
            <tr>
              <th>Action</th>
              <th>Default (if no rule matches)</th>
              <th>Active rules on this workspace</th>
              <th aria-label="Actions"></th>
            </tr>
          </thead>
          <tbody>
            {catalogRows.map((entry) => {
              const rs = rulesByAction[entry.key] ?? [];
              return (
                <tr key={entry.key}>
                  <td>
                    <div className="mono">{entry.key}</div>
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
                      <span className="muted">, default applies,</span>
                    ) : (
                      rs.map((r) => (
                        <RuleChip
                          key={r.id}
                          rule={r}
                          group={groupsById[r.subject_id]}
                          groupLabel={groupsById[r.subject_id]?.name}
                          userLabel={users.data?.[r.subject_id]?.display_name}
                        />
                      ))
                    )}
                  </td>
                  <td>
                    {entry.root_only ? (
                      <span className="muted">,</span>
                    ) : (
                      <button type="button" className="btn btn--ghost btn--sm">
                        <Plus size={13} strokeWidth={2} /> Rule
                      </button>
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
