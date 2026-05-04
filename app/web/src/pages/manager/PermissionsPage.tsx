import { useState } from "react";
import DeskPage from "@/components/DeskPage";
import GroupsTab from "./permissions/GroupsTab";
import PrivacyTab from "./permissions/PrivacyTab";
import RulesTab from "./permissions/RulesTab";

type Tab = "groups" | "rules" | "privacy";

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
          <button
            className={`btn btn--ghost ${tab === "privacy" ? "btn--active" : ""}`}
            onClick={() => setTab("privacy")}
          >
            Privacy
          </button>
        </div>
      }
    >
      {tab === "groups" ? <GroupsTab /> : tab === "rules" ? <RulesTab /> : <PrivacyTab />}
    </DeskPage>
  );
}
