import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import AgentPreferencesPanel from "@/components/AgentPreferencesPanel";
import { Loading } from "@/components/common";
import { useWorkspace } from "@/context/WorkspaceContext";
import type { AuthMe } from "@/auth/types";
import type {
  Employee,
  EntitySettingsPayload,
  SettingDefinition,
  WorkspaceSettings,
} from "@/types/api";
import AssetsPanel from "./property/AssetsPanel";
import OverviewPanel from "./property/OverviewPanel";
import SettingsOverridePanel from "./property/SettingsOverridePanel";
import SharingPanel from "./property/SharingPanel";
import { fetchPropertyDetail } from "./property/propertyDetailApi";
import type { PropertyTab } from "./property/types";

export default function PropertyDetailPage() {
  // code-health: ignore[nloc] Property detail route is a declarative shell around extracted detail sections.
  const { pid = "" } = useParams<{ pid: string }>();
  const [activeTab, setActiveTab] = useState<PropertyTab>("overview");
  const { workspaceId } = useWorkspace();

  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<AuthMe>("/api/v1/auth/me") });
  const detailQ = useQuery({
    queryKey: qk.property(pid),
    queryFn: () => fetchPropertyDetail(pid, workspaceId),
    enabled: pid !== "",
  });
  const empsQ = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });
  const settingsQ = useQuery({
    queryKey: qk.propertySettings(pid),
    queryFn: async (): Promise<EntitySettingsPayload> => {
      const settings = await fetchJson<WorkspaceSettings>("/api/v1/settings");
      const overrides = detailQ.data?.property.settings_override ?? {};
      return {
        overrides,
        resolved: Object.fromEntries(
          Object.entries(settings.defaults).map(([key, value]) => [key, { value, source: "workspace" }]),
        ),
      };
    },
    enabled: pid !== "" && activeTab === "settings",
  });
  const catalogQ = useQuery({
    queryKey: qk.settingsCatalog(),
    queryFn: () => fetchJson<SettingDefinition[]>("/api/v1/settings/catalog"),
    enabled: activeTab === "settings",
  });
  if (detailQ.isPending || empsQ.isPending) {
    return <DeskPage title="Property"><Loading /></DeskPage>;
  }
  if (!detailQ.data || !empsQ.data) {
    return <DeskPage title="Property">Failed to load.</DeskPage>;
  }

  const detail = detailQ.data;
  const { property } = detail;

  return (
    <DeskPage
      title={property.name}
      sub={property.city + " · " + property.timezone}
      actions={<button className="btn btn--moss">Edit property</button>}
      overflow={[{ label: "New task", onSelect: () => undefined }]}
    >
      <nav className="tabs tabs--h">
        <a
          className={"tab-link" + (activeTab === "overview" ? " tab-link--active" : "")}
          onClick={() => setActiveTab("overview")}
        >
          Overview
        </a>
        <a className="tab-link">Areas</a>
        <a className="tab-link">Stays</a>
        <a
          className={"tab-link" + (activeTab === "assets" ? " tab-link--active" : "")}
          onClick={() => setActiveTab("assets")}
        >
          Assets
        </a>
        <a className="tab-link">Instructions</a>
        <a className="tab-link">Closures</a>
        <a
          className={"tab-link" + (activeTab === "sharing" ? " tab-link--active" : "")}
          onClick={() => setActiveTab("sharing")}
        >
          Sharing &amp; client
        </a>
        <a
          className={"tab-link" + (activeTab === "settings" ? " tab-link--active" : "")}
          onClick={() => setActiveTab("settings")}
        >
          Settings
        </a>
      </nav>

      {activeTab === "overview" && (
        <OverviewPanel detail={detail} employees={empsQ.data} />
      )}

      {activeTab === "settings" && (
        <>
          {(settingsQ.isPending || catalogQ.isPending) ? (
            <Loading />
          ) : settingsQ.data && catalogQ.data ? (
            <SettingsOverridePanel
              overrides={settingsQ.data.overrides}
              resolved={settingsQ.data.resolved}
              catalog={catalogQ.data}
            />
          ) : (
            <p>Failed to load settings.</p>
          )}
        </>
      )}

      {activeTab === "assets" && (
        <AssetsPanel detail={detail} />
      )}

      {activeTab === "sharing" && (
        <SharingPanel
          detail={detail}
          meAvailable={meQ.data?.available_workspaces ?? []}
        />
      )}

      <AgentPreferencesPanel
        scope="property"
        scopeId={property.id}
        title={"Agent preferences — " + property.name}
        subtitle="Sits between workspace and user preferences when the agent discusses this property. Soft guidance only — hard rules belong in the settings cascade above."
      />
    </DeskPage>
  );
}
