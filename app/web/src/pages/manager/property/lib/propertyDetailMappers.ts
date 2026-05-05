import type {
  InventoryItem,
  Organization,
  Property,
  PropertyWorkspace,
  Stay,
  Task,
  TaskPriority,
  TaskStatus,
} from "@/types/api";

export interface PropertyDetailRow {
  id: string;
  name: string;
  kind: Property["kind"];
  address_json: Record<string, unknown>;
  country: string;
  locale: string | null;
  timezone: string;
  client_org_id: string | null;
  owner_user_id: string | null;
}

export interface TaskRow {
  id: string;
  workspace_id: string;
  template_id: string | null;
  schedule_id: string | null;
  property_id: string | null;
  area_id: string | null;
  title: string;
  priority: string;
  state: string;
  scheduled_for_utc: string;
  duration_minutes: number | null;
  photo_evidence: string;
  linked_instruction_ids: string[];
  assigned_user_id: string | null;
  created_by: string;
  is_personal: boolean;
  overdue: boolean;
}

export interface ReservationRow {
  id: string;
  property_id: string;
  check_in: string;
  check_out: string;
  guest_name: string | null;
  guest_count: number | null;
  status: string;
  source: string;
}

export interface InventoryItemRow {
  id: string;
  property_id: string;
  name: string;
  sku: string;
  on_hand: number;
  reorder_point: number | null;
  unit: string;
}

export interface MembershipRow {
  property_id: string;
  workspace_id: string;
  label: string;
  membership_role: PropertyWorkspace["membership_role"];
  share_guest_identity: boolean;
  created_at: string;
}

export interface OrganizationRow {
  id: string;
  workspace_id: string;
  kind: "client" | "vendor" | "mixed";
  display_name: string;
  tax_id: string | null;
  default_currency: string;
  notes_md: string | null;
}

function dateOnly(iso: string): string {
  // code-health: ignore[ccn] Tiny date helper is over-counted by lizard after TS parser recovery.
  return iso.slice(0, 10);
}

function mapTaskStatus(state: string, overdue: boolean): TaskStatus {
  if (overdue) return "overdue";
  if (state === "completed") return "completed";
  if (state === "scheduled" || state === "pending" || state === "in_progress" || state === "completed" || state === "skipped" || state === "cancelled" || state === "overdue") return state;
  return "pending";
}

function mapPriority(priority: string): TaskPriority {
  if (priority === "low" || priority === "normal" || priority === "high" || priority === "urgent") return priority;
  return "normal";
}

function mapPhotoEvidence(value: string): Task["photo_evidence"] {
  if (value === "disabled" || value === "optional" || value === "required") return value;
  return "optional";
}

function mapSource(source: string): Stay["source"] {
  if (source === "api") return "manual";
  if (source === "gcal") return "google_calendar";
  if (source === "manual" || source === "airbnb" || source === "vrbo" || source === "booking" || source === "google_calendar" || source === "ical") return source;
  return "ical";
}

function mapStatus(status: string): Stay["status"] {
  if (status === "cancelled") return "cancelled";
  if (status === "scheduled") return "confirmed";
  if (status === "checked_in") return "in_house";
  if (status === "completed") return "checked_out";
  if (status === "tentative" || status === "confirmed" || status === "in_house" || status === "checked_out") return status;
  return "confirmed";
}

export function mapTask(row: TaskRow): Task {
  return {
    id: row.id,
    title: row.title,
    property_id: row.property_id ?? "",
    area: row.area_id ?? "",
    assignee_id: row.assigned_user_id ?? "",
    scheduled_start: row.scheduled_for_utc,
    estimated_minutes: row.duration_minutes ?? 30,
    priority: mapPriority(row.priority),
    status: mapTaskStatus(row.state, row.overdue),
    checklist: [],
    photo_evidence: mapPhotoEvidence(row.photo_evidence),
    evidence_policy: "inherit",
    instructions_ids: row.linked_instruction_ids,
    template_id: row.template_id,
    schedule_id: row.schedule_id,
    turnover_bundle_id: null,
    asset_id: null,
    settings_override: {},
    assigned_user_id: row.assigned_user_id ?? "",
    workspace_id: row.workspace_id,
    created_by: row.created_by,
    is_personal: row.is_personal,
  };
}

export function mapReservation(row: ReservationRow): Stay {
  return {
    id: row.id,
    property_id: row.property_id,
    guest_name: row.guest_name ?? "Guest",
    source: mapSource(row.source),
    check_in: dateOnly(row.check_in),
    check_out: dateOnly(row.check_out),
    guests: row.guest_count ?? 0,
    status: mapStatus(row.status),
  };
}

export function mapInventoryItem(row: InventoryItemRow): InventoryItem {
  return {
    id: row.id,
    property_id: row.property_id,
    name: row.name,
    sku: row.sku,
    on_hand: row.on_hand,
    par: row.reorder_point ?? 0,
    unit: row.unit,
    area: "",
  };
}

export function mapMembership(row: MembershipRow): PropertyWorkspace {
  return {
    property_id: row.property_id,
    workspace_id: row.workspace_id,
    membership_role: row.membership_role,
    share_guest_identity: row.share_guest_identity,
    invite_id: null,
    added_at: row.created_at,
    added_by_user_id: null,
    added_via: "system",
  };
}

export function mapOrganization(row: OrganizationRow): Organization {
  return {
    id: row.id,
    name: row.display_name,
    workspace_id: row.workspace_id,
    is_client: row.kind === "client" || row.kind === "mixed",
    is_supplier: row.kind === "vendor" || row.kind === "mixed",
    legal_name: null,
    default_currency: row.default_currency,
    tax_id: row.tax_id,
    contacts: [],
    notes: row.notes_md,
    default_pay_destination_stub: null,
    portal_user_id: null,
    cancellation_window_hours: null,
    cancellation_fee_pct: null,
  };
}

export function fallbackProperty(row: PropertyDetailRow): Property {
  const city = typeof row.address_json.city === "string" ? row.address_json.city : "";
  return {
    id: row.id,
    name: row.name,
    city,
    timezone: row.timezone,
    color: "moss",
    kind: row.kind,
    areas: [],
    evidence_policy: "inherit",
    country: row.country,
    locale: row.locale ?? "",
    settings_override: {},
    client_org_id: row.client_org_id,
    owner_user_id: row.owner_user_id,
  };
}
