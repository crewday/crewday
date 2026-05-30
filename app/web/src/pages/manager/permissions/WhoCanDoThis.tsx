import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import SearchableSelect, { type SearchableSelectOption } from "@/components/SearchableSelect";
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
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);
  const [selectedActionKey, setSelectedActionKey] = useState<string | null>(null);
  const userOptions = useMemo(() => users.map(userOption), [users]);
  const actionOptions = useMemo(() => actions.map(actionOption), [actions]);
  const userId = users.some((user) => user.id === selectedUserId) ? selectedUserId ?? "" : users[0]?.id ?? "";
  const actionKey = actions.some((action) => action.key === selectedActionKey)
    ? selectedActionKey ?? ""
    : actions[0]?.key ?? "";

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
        <SearchableSelect
          label="User"
          value={userId}
          options={userOptions}
          onChange={setSelectedUserId}
          required
        />
        <SearchableSelect
          label="Action"
          value={actionKey}
          options={actionOptions}
          onChange={setSelectedActionKey}
          required
        />
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

function userOption(user: UserIndexRow): SearchableSelectOption {
  return {
    value: user.id,
    label: user.display_name,
    secondaryText: user.email,
    searchText: [user.display_name, user.email, user.id].filter(Boolean).join(" "),
  };
}

function actionOption(action: ActionCatalogEntry): SearchableSelectOption {
  const scopes = action.valid_scope_kinds.join(", ");
  return {
    value: action.key,
    label: action.key,
    secondaryText: scopes,
    searchText: `${action.key} ${scopes}`,
  };
}
