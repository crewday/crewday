import type {
  Asset,
  AssetDocument,
  Instruction,
  InventoryItem,
  Organization,
  Property,
  PropertyClosure,
  PropertyWorkspace,
  Stay,
  Task,
  User,
  Workspace,
} from "@/types/api";

export interface PropertyDetail {
  property: Property;
  property_tasks: Task[];
  stays: Stay[];
  inventory: InventoryItem[];
  instructions: Instruction[];
  closures: PropertyClosure[];
  assets: Asset[];
  asset_documents: AssetDocument[];
  memberships: PropertyWorkspace[];
  membership_workspaces: Workspace[];
  workspace_id_by_slug: Record<string, string>;
  workspace_slug_by_id: Record<string, string>;
  client_org: Organization | null;
  owner_user: User | null;
  active_workspace_id: string;
}

export type PropertyTab = "overview" | "assets" | "sharing" | "settings";
