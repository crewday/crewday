import { useState } from "react";
import DeskPage from "@/components/DeskPage";
import type { RoleGrant } from "@/types/api";
import GroupsTab from "./permissions/GroupsTab";
import RulesTab from "./permissions/RulesTab";

type Tab = "groups" | "rules";

export default function PermissionsPage() {
  const [tab, setTab] = useState<Tab>("groups");

  const sub =
    "Who can do what. Groups collect users; rules attach to actions. " +
    "Root-only actions (marked) stay with owners regardless of rules.";

  return (
    <DeskPage
      title="Permissions"
      sub={sub}
      actions={
        <div className="permissions__tabs">
          <button
            className={`btn btn--ghost ${tab === "groups" ? "btn--active" : ""}`}
            onClick={() => setTab("groups")}
          >
            Groups
          </button>
          <button
            className={`btn btn--ghost ${tab === "rules" ? "btn--active" : ""}`}
            onClick={() => setTab("rules")}
          >
            Rules
          </button>
        </div>
      }
    >
      {tab === "groups" ? <GroupsTab /> : <RulesTab />}
    </DeskPage>
  );
}

// Re-export `RoleGrant` so the type is referenced (keeps linter quiet
// when future extensions consult role_grants from this page).
export type { RoleGrant };
