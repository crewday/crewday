import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import FormField from "@/components/FormField";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Chip, Loading } from "@/components/common";
import type {
  ActionCatalogEntry,
  ResolvedPermission,
} from "@/types/api";
import type { UserIndexRow } from "./lib/usePermissionIndexes";

// Live "who can do this?" preview — calls the resolver.
export default function WhoCanDoThis({
  users,
  actions,
  scopeKind,
  scopeId,
}: {
  users: UserIndexRow[];
  actions: ActionCatalogEntry[];
  scopeKind: "workspace" | "property" | "organization";
  scopeId: string;
}) {
  // code-health: ignore[nloc] Permission explanation panel is declarative render composition over computed rows.
  const [userId, setUserId] = useState<string>(users[0]?.id ?? "");
  const [actionKey, setActionKey] = useState<string>(actions[0]?.key ?? "");

  useEffect(() => {
    if (users.length === 0) {
      if (userId) setUserId("");
      return;
    }
    if (!users.some((u) => u.id === userId)) setUserId(users[0]?.id ?? "");
  }, [userId, users]);

  useEffect(() => {
    if (actions.length === 0) {
      if (actionKey) setActionKey("");
      return;
    }
    if (!actions.some((a) => a.key === actionKey)) setActionKey(actions[0]?.key ?? "");
  }, [actionKey, actions]);

  const resolved = useQuery({
    queryKey: qk.permissionResolved(userId, actionKey, scopeKind, scopeId),
    queryFn: () =>
      fetchJson<ResolvedPermission>(
        `/api/v1/permissions/resolved?user_id=${encodeURIComponent(userId)}` +
          `&action_key=${encodeURIComponent(actionKey)}` +
          `&scope_kind=${scopeKind}&scope_id=${encodeURIComponent(scopeId)}`,
      ),
    enabled: !!userId && !!actionKey && !!scopeId,
  });

  if (users.length === 0) {
    return (
      <div className="permissions__resolver">
        <h4>Who can do this?</h4>
        <p className="muted">
          No users found for this workspace.
        </p>
      </div>
    );
  }

  return (
    <div className="permissions__resolver">
      <h4>Who can do this?</h4>
      <div className="permissions__resolver-fields">
        <FormField label="User" requirement="required">
          <select value={userId} onChange={(e) => setUserId(e.target.value)}>
            {users.map((u) => (
              <option key={u.id} value={u.id}>
                {u.display_name}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Action" requirement="required">
          <select value={actionKey} onChange={(e) => setActionKey(e.target.value)}>
            {actions.map((a) => (
              <option key={a.key} value={a.key}>
                {a.key}
              </option>
            ))}
          </select>
        </FormField>
      </div>
      {resolved.isPending ? (
        <Loading />
      ) : resolved.data ? (
        <div className="permissions__resolver-result">
          <Chip tone={resolved.data.effect === "allow" ? "moss" : "rust"}>
            {resolved.data.effect}
          </Chip>{" "}
          <span className="mono muted">
            via <strong>{resolved.data.source_layer}</strong>
          </span>
          {resolved.data.matched_groups.length > 0 ? (
            <span className="muted">
              {" "}
              · matched{" "}
              {resolved.data.matched_groups.map((g) => (
                <Chip key={g} tone="ghost" size="sm">{g}</Chip>
              ))}
            </span>
          ) : null}
          {resolved.data.source_rule_id ? (
            <div className="mono muted">rule: {resolved.data.source_rule_id}</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
