import { ApiError, fetchJson } from "@/lib/api";
import { type ListEnvelope } from "@/lib/listResponse";
import type { AuthMe } from "@/auth/types";
import type {
  Asset,
  AssetDocument,
  Property,
  Workspace,
} from "@/types/api";
import {
  fallbackProperty,
  mapMembership,
  mapOrganization,
  mapReservation,
  mapTask,
  type MembershipRow,
  type OrganizationRow,
  type PropertyDetailRow,
  type ReservationRow,
  type TaskRow,
} from "./lib/propertyDetailMappers";
import type { PropertyDetail } from "./types";

interface WorkspaceSwitcherEntry {
  workspace_id: string;
  slug: string;
}

async function emptyListOnNotFound<T>(
  request: Promise<ListEnvelope<T>>,
): Promise<ListEnvelope<T>> {
  try {
    return await request;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      return { data: [], next_cursor: null, has_more: false };
    }
    throw err;
  }
}

async function emptyDataOnNotFound<T>(
  request: Promise<{ data: T[] }>,
): Promise<{ data: T[] }> {
  try {
    return await request;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return { data: [] };
    throw err;
  }
}

export async function fetchPropertyDetail(
  pid: string,
  activeWorkspaceSlug: string | null,
): Promise<PropertyDetail> {
  const me = await fetchJson<AuthMe>("/api/v1/auth/me");
  const workspaceEntries = await fetchJson<WorkspaceSwitcherEntry[]>("/api/v1/me/workspaces");
  const properties = await fetchJson<Property[]>("/api/v1/properties");
  const propertyRow = await fetchJson<PropertyDetailRow>("/api/v1/properties/" + pid);
  const tasks = await emptyListOnNotFound(
    fetchJson<ListEnvelope<TaskRow>>("/api/v1/tasks?property_id=" + encodeURIComponent(pid) + "&limit=100"),
  );
  const reservations = await emptyListOnNotFound(
    fetchJson<ListEnvelope<ReservationRow>>(
      "/api/v1/stays/reservations?property_id=" + encodeURIComponent(pid) + "&limit=100",
    ),
  );
  const memberships = await fetchJson<ListEnvelope<MembershipRow>>("/api/v1/properties/" + pid + "/share");
  const organizations = await emptyDataOnNotFound(
    fetchJson<{ data: OrganizationRow[] }>("/api/v1/billing/organizations"),
  );

  const property = properties.find((p) => p.id === pid) ?? fallbackProperty(propertyRow);
  const membershipRows = memberships.data.map(mapMembership);
  const namesByMembershipId = new Map(memberships.data.map((m) => [m.workspace_id, m.label]));
  const workspaceIdBySlug = Object.fromEntries(
    workspaceEntries.map((entry) => [entry.slug, entry.workspace_id]),
  );
  const workspaceSlugById = Object.fromEntries(
    workspaceEntries.map((entry) => [entry.workspace_id, entry.slug]),
  );
  const membershipWorkspaces: Workspace[] = membershipRows.map((m) => ({
    id: m.workspace_id,
    name: namesByMembershipId.get(m.workspace_id) ?? m.workspace_id,
    timezone: property.timezone,
    default_currency: organizations.data[0]?.default_currency ?? "EUR",
    default_country: property.country,
    default_locale: property.locale,
  }));
  const clientOrg = organizations.data.find((o) => o.id === property.client_org_id);

  return {
    property,
    property_tasks: tasks.data.map(mapTask),
    stays: reservations.data.map(mapReservation),
    inventory: [],
    instructions: [],
    closures: [],
    assets: [] as Asset[],
    asset_documents: [] as AssetDocument[],
    memberships: membershipRows,
    membership_workspaces: membershipWorkspaces,
    workspace_id_by_slug: workspaceIdBySlug,
    workspace_slug_by_id: workspaceSlugById,
    client_org: clientOrg ? mapOrganization(clientOrg) : null,
    owner_user: null,
    active_workspace_id: (activeWorkspaceSlug && workspaceIdBySlug[activeWorkspaceSlug]) || me.current_workspace_id || "",
  };
}
