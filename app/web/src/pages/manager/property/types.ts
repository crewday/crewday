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
  property_record: PropertyRecord;
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

export interface PropertyAddress {
  line1: string;
  line2: string;
  city: string;
  state_province: string;
  postal_code: string;
  country: string;
  [key: string]: unknown;
}

export interface PropertyRecord {
  id: string;
  name: string;
  kind: Property["kind"];
  address: string;
  address_json: PropertyAddress;
  country: string;
  locale: string | null;
  default_currency: string | null;
  timezone: string;
  lat: number | null;
  lon: number | null;
  client_org_id: string | null;
  owner_user_id: string | null;
  tags_json: string[];
  welcome_defaults_json: Record<string, unknown>;
  property_notes_md: string;
}

export type PropertyTab = "overview" | "areas" | "documents" | "sharing" | "settings";
