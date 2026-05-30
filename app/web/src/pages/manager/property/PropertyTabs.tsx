import PageTabs, { type PageTabLink } from "@/components/PageTabs";
import { workspaceRouteForPathname } from "@/lib/workspaceRoutes";
import { PROPERTY_TABS, type PropertyRelatedPage } from "./PropertyTabs.lib";
import type { PropertyTab } from "./types";

function propertySectionRoute(propertyId: string, tab: PropertyTab): string {
  return "/property/" + propertyId + (tab === "overview" ? "" : "#" + tab);
}

interface PropertyTabsProps {
  pathname: string;
  propertyId: string;
  activeSection?: PropertyTab;
  activeRelatedPage?: PropertyRelatedPage;
  onSectionSelect?: (key: string) => void;
}

export default function PropertyTabs({
  pathname,
  propertyId,
  activeSection,
  activeRelatedPage,
  onSectionSelect,
}: PropertyTabsProps) {
  const relatedTabs = [
    {
      key: "stays",
      label: "Stays",
      to: workspaceRouteForPathname(pathname, "/property/" + propertyId + "/stays"),
    },
    {
      key: "instructions",
      label: "Instructions",
      to: workspaceRouteForPathname(pathname, "/property/" + propertyId + "/instructions"),
    },
    {
      key: "closures",
      label: "Closures",
      to: workspaceRouteForPathname(pathname, "/property/" + propertyId + "/closures"),
    },
    {
      key: "inventory",
      label: "Inventory",
      to: workspaceRouteForPathname(pathname, "/property/" + propertyId + "/inventory"),
    },
  ] satisfies PageTabLink[];

  return (
    <div className="property-tabs">
      {onSectionSelect ? (
        <PageTabs
          ariaLabel="Property sections"
          tabs={PROPERTY_TABS}
          hashBacked
          defaultKey="overview"
          selectedKey={activeSection}
          onSelect={onSectionSelect}
          className="property-tabs__sections"
        />
      ) : (
        <PageTabs
          ariaLabel="Property sections"
          tabs={PROPERTY_TABS.map((tab) => ({
            key: tab.key,
            label: tab.label,
            to: workspaceRouteForPathname(pathname, propertySectionRoute(propertyId, tab.key)),
          }))}
          activeKey={activeSection}
          className="property-tabs__sections"
        />
      )}
      <PageTabs
        ariaLabel="Related property pages"
        tabs={relatedTabs}
        activeKey={activeRelatedPage}
        className="property-tabs__links"
      />
    </div>
  );
}
