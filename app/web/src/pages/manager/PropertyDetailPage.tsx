import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation, useParams } from "react-router-dom";
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
import AreasPanel from "./property/AreasPanel";
import OverviewPanel from "./property/OverviewPanel";
import PropertyEditDialog from "./property/PropertyEditDialog";
import {
  buildPropertyPatchBody,
  type PropertyEditDraft,
} from "./property/PropertyEditDialog.lib";
import SettingsOverridePanel from "./property/SettingsOverridePanel";
import SharingPanel from "./property/SharingPanel";
import PropertyTabs from "./property/PropertyTabs";
import { panelIdFor, PROPERTY_TABS } from "./property/PropertyTabs.lib";
import { fetchPropertyDetail } from "./property/propertyDetailApi";
import type { PropertyRecord, PropertyTab } from "./property/types";

function tabFromHash(hash: string): PropertyTab {
  const key = hash.replace(/^#/, "");
  return PROPERTY_TABS.find((tab) => tab.key === key)?.key ?? "overview";
}

export default function PropertyDetailPage() {
  // code-health: ignore[nloc] Property detail route is a declarative shell around extracted detail sections.
  const { pid = "" } = useParams<{ pid: string }>();
  const { pathname } = useLocation();
  const [activeTab, setActiveTab] = useState<PropertyTab>(() => tabFromHash(window.location.hash));
  const [editingProperty, setEditingProperty] = useState(false);
  const [propertySaveError, setPropertySaveError] = useState<string | null>(null);
  const { workspaceId } = useWorkspace();
  const queryClient = useQueryClient();

  useEffect(() => {
    const syncFromHash = () => setActiveTab(tabFromHash(window.location.hash));
    syncFromHash();
    window.addEventListener("hashchange", syncFromHash);
    return () => window.removeEventListener("hashchange", syncFromHash);
  }, [pid]);

  function selectTab(next: string): void {
    setActiveTab(tabFromHash(`#${next}`));
  }

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
  const saveProperty = useMutation({
    mutationFn: (draft: PropertyEditDraft) => {
      const record = detailQ.data?.property_record;
      if (!record) throw new Error("Property is not loaded.");
      return fetchJson<PropertyRecord>("/api/v1/properties/" + pid, {
        method: "PATCH",
        body: buildPropertyPatchBody(record, draft),
      });
    },
    onSuccess: async () => {
      setPropertySaveError(null);
      setEditingProperty(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: qk.property(pid) }),
        queryClient.invalidateQueries({ queryKey: qk.properties() }),
        queryClient.invalidateQueries({ queryKey: qk.propertyAreas(pid) }),
      ]);
    },
    onError: (err) => {
      setPropertySaveError(err instanceof Error ? err.message : "Property could not be saved.");
    },
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
      actions={
        <button
          type="button"
          className="btn btn--moss"
          onClick={() => {
            setPropertySaveError(null);
            setEditingProperty(true);
          }}
        >
          Edit property
        </button>
      }
      overflow={[
        {
          label: "New task",
          onSelect: () => undefined,
          disabledReason: "Create tasks from Tasks or Today until property-scoped quick add ships.",
        },
      ]}
    >
      <PropertyTabs
        pathname={pathname}
        propertyId={property.id}
        activeSection={activeTab}
        onSectionSelect={selectTab}
      />

      {activeTab === "overview" && (
        <div id={panelIdFor("overview")} role="tabpanel">
          <OverviewPanel detail={detail} employees={empsQ.data} />
        </div>
      )}

      {activeTab === "areas" && (
        <div id={panelIdFor("areas")} role="tabpanel">
          <AreasPanel propertyId={property.id} />
        </div>
      )}

      {activeTab === "settings" && (
        <div id={panelIdFor("settings")} role="tabpanel">
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
        </div>
      )}

      {activeTab === "sharing" && (
        <div id={panelIdFor("sharing")} role="tabpanel">
          <SharingPanel
            detail={detail}
            meAvailable={meQ.data?.available_workspaces ?? []}
          />
        </div>
      )}

      <AgentPreferencesPanel
        scope="property"
        scopeId={property.id}
        title={"Agent preferences, " + property.name}
        subtitle="Sits between workspace and user preferences when the agent discusses this property. Soft guidance only, hard rules belong in the settings cascade above."
      />

      {editingProperty && (
        <PropertyEditDialog
          open={editingProperty}
          property={detail.property_record}
          saving={saveProperty.isPending}
          error={propertySaveError}
          onSubmit={(draft) => saveProperty.mutate(draft)}
          onClose={() => setEditingProperty(false)}
        />
      )}
    </DeskPage>
  );
}
