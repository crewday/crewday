import { useEffect, useState } from "react";
import DeskPage from "@/components/DeskPage";
import GroupsTab from "./permissions/GroupsTab";
import PrivacyTab from "./permissions/PrivacyTab";
import RulesTab from "./permissions/RulesTab";

type Tab = "groups" | "rules" | "privacy";

const TABS: { key: Tab; label: string }[] = [
  { key: "groups", label: "Groups" },
  { key: "rules", label: "Rules" },
  { key: "privacy", label: "Privacy" },
];

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

  function selectTab(next: Tab): void {
    setTab(next);
    if (window.location.hash !== `#${next}`) window.location.hash = next;
  }

  const sub =
    "Who can do what. Groups collect users; rules attach to actions. " +
    "Root-only actions (marked) stay with owners regardless of rules.";

  return (
    <DeskPage
      title="Permissions"
      sub={sub}
      actions={
        <div className="permissions__tabs">
          {TABS.map((item) => (
            <a
              key={item.key}
              className={`btn btn--ghost ${tab === item.key ? "btn--active" : ""}`}
              href={`#${item.key}`}
              aria-current={tab === item.key ? "page" : undefined}
              onClick={() => selectTab(item.key)}
            >
              {item.label}
            </a>
          ))}
        </div>
      }
    >
      {tab === "groups" ? <GroupsTab /> : tab === "rules" ? <RulesTab /> : <PrivacyTab />}
    </DeskPage>
  );
}
