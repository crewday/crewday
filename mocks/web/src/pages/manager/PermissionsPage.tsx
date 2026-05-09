import { useEffect, useState } from "react";
import DeskPage from "@/components/DeskPage";
import PageTabs, { type PageTab } from "@/components/PageTabs";
import GroupsTab from "./permissions/GroupsTab";
import RulesTab from "./permissions/RulesTab";

type Tab = "groups" | "rules";

const TABS = [
  { key: "groups", label: "Groups", panelId: "permissions-groups-panel" },
  { key: "rules", label: "Rules", panelId: "permissions-rules-panel" },
] satisfies Array<PageTab & { key: Tab }>;

function tabFromHash(hash: string): Tab {
  const key = hash.replace(/^#/, "");
  return TABS.find((tab) => tab.key === key)?.key ?? "groups";
}

export default function PermissionsPage() {
  const [tab, setTab] = useState<Tab>(() => tabFromHash(window.location.hash));

  useEffect(() => {
    const syncFromHash = () => setTab(tabFromHash(window.location.hash));
    window.addEventListener("hashchange", syncFromHash);
    return () => window.removeEventListener("hashchange", syncFromHash);
  }, []);

  const sub =
    "Who can do what. Groups collect users; rules attach to actions. " +
    "Root-only actions (marked) stay with owners regardless of rules.";

  return (
    <DeskPage title="Permissions" sub={sub}>
      <PageTabs
        ariaLabel="Permissions sections"
        tabs={TABS}
        hashBacked
        defaultKey="groups"
        selectedKey={tab}
        onSelect={(next) => setTab(tabFromHash(`#${next}`))}
      />
      <div id={`permissions-${tab}-panel`} role="tabpanel">
        {tab === "groups" ? <GroupsTab /> : <RulesTab />}
      </div>
    </DeskPage>
  );
}
