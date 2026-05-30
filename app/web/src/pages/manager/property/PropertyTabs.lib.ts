import type { PageTab } from "@/components/PageTabs";
import type { PropertyTab } from "./types";

export function panelIdFor(tab: PropertyTab): string {
  return `property-${tab}-panel`;
}

export const PROPERTY_TABS = [
  { key: "overview", label: "Overview", panelId: panelIdFor("overview") },
  { key: "areas", label: "Areas", panelId: panelIdFor("areas") },
  { key: "sharing", label: "Sharing & client", panelId: panelIdFor("sharing") },
  { key: "settings", label: "Settings", panelId: panelIdFor("settings") },
] satisfies Array<PageTab & { key: PropertyTab }>;

export type PropertyRelatedPage = "stays" | "instructions" | "closures" | "inventory" | "assets" | "schedules";
