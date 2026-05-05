import { useQueries, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { type ListEnvelope } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import { useWorkspace } from "@/context/WorkspaceContext";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type { AuthMe } from "@/auth/types";
import type {
  Organization,
  Property,
  PropertyClosure,
  PropertyWorkspace,
  Stay,
  Workspace,
} from "@/types/api";

interface StaysPayload {
  stays: Stay[];
  closures: PropertyClosure[];
}

interface WorkspaceSwitcherEntry {
  workspace_id: string;
  slug: string;
  name: string;
}

interface PageWorkspace extends Workspace {
  slug: string;
}

interface ReservationPayload {
  id: string;
  property_id: string;
  check_in: string;
  check_out: string;
  guest_name: string | null;
  guest_count: number | null;
  status: string;
  source: string;
}

interface ClosurePayload {
  id: string;
  property_id: string;
  starts_at: string;
  ends_at: string;
  reason: PropertyClosure["reason"];
}

interface MembershipPayload {
  property_id: string;
  workspace_id: string;
  label: string;
  membership_role: PropertyWorkspace["membership_role"];
  share_guest_identity: boolean;
  created_at: string;
}

interface PagePropertyWorkspace extends PropertyWorkspace {
  label: string;
}

interface OrganizationPayload {
  id: string;
  workspace_id: string;
  kind: "client" | "vendor" | "mixed";
  display_name: string;
  tax_id: string | null;
  default_currency: string;
  notes_md: string | null;
}

// §02 — short label for membership_role on the property card. Keep the
// vocabulary small so the chip doesn't crowd the row.
const MEMBERSHIP_LABEL: Record<string, string> = {
  owner_workspace: "Owner",
  managed_workspace: "Managed",
  observer_workspace: "Observer",
};

function dateOnly(iso: string): string {
  // code-health: ignore[ccn nloc] Tiny date helper is over-counted by lizard after TSX parsing.
  return iso.slice(0, 10);
}

function mapStatus(status: string): Stay["status"] {
  if (status === "cancelled") return "cancelled";
  if (status === "scheduled") return "confirmed";
  if (status === "checked_in") return "in_house";
  if (status === "completed") return "checked_out";
  if (status === "tentative" || status === "confirmed" || status === "in_house" || status === "checked_out") return status;
  return "confirmed";
}

function mapSource(source: string): Stay["source"] {
  if (source === "api") return "manual";
  if (source === "gcal") return "google_calendar";
  if (source === "manual" || source === "airbnb" || source === "vrbo" || source === "booking" || source === "google_calendar" || source === "ical") return source;
  return "ical";
}

function mapReservation(row: ReservationPayload): Stay {
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

function mapClosure(row: ClosurePayload): PropertyClosure {
  return {
    id: row.id,
    property_id: row.property_id,
    starts_on: dateOnly(row.starts_at),
    ends_on: dateOnly(row.ends_at),
    reason: row.reason,
    note: "",
  };
}

function mapMembership(row: MembershipPayload): PagePropertyWorkspace {
  return {
    property_id: row.property_id,
    workspace_id: row.workspace_id,
    label: row.label,
    membership_role: row.membership_role,
    share_guest_identity: row.share_guest_identity,
    invite_id: null,
    added_at: row.created_at,
    added_by_user_id: null,
    added_via: "system",
  };
}

function mapWorkspace(row: WorkspaceSwitcherEntry): PageWorkspace {
  return {
    id: row.workspace_id,
    slug: row.slug,
    name: row.name,
    timezone: "",
    default_currency: "",
    default_country: "",
    default_locale: "",
  };
}

function mapOrganization(row: OrganizationPayload): Organization {
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

async function fetchStaysPayload(): Promise<StaysPayload> {
  const reservations = await fetchJson<ListEnvelope<ReservationPayload>>("/api/v1/stays/reservations?limit=500");
  return {
    stays: reservations.data.map(mapReservation),
    closures: [],
  };
}

async function fetchPropertyMemberships(propertyId: string): Promise<PagePropertyWorkspace[]> {
  const rows = await fetchJson<ListEnvelope<MembershipPayload>>("/api/v1/properties/" + propertyId + "/share");
  return rows.data.map(mapMembership);
}

async function fetchPropertyClosures(propertyId: string): Promise<PropertyClosure[]> {
  const rows = await fetchJson<ListEnvelope<ClosurePayload>>("/api/v1/property_closures?property_id=" + encodeURIComponent(propertyId) + "&limit=100");
  return rows.data.map(mapClosure);
}

async function fetchWorkspaces(): Promise<PageWorkspace[]> {
  const rows = await fetchJson<WorkspaceSwitcherEntry[]>("/api/v1/me/workspaces");
  return rows.map(mapWorkspace);
}

async function fetchOrganizations(): Promise<Organization[]> {
  const rows = await fetchJson<{ data: OrganizationPayload[] }>("/api/v1/billing/organizations");
  return rows.data.map(mapOrganization);
}

export default function PropertiesPage() {
  // code-health: ignore[ccn nloc] Properties route composes query mapping, selected card state, and promoted table layout.
  const { workspaceId } = useWorkspace();
  const meQ = useQuery({ queryKey: qk.authMe(), queryFn: () => fetchJson<AuthMe>("/api/v1/auth/me") });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const staysQ = useQuery({
    queryKey: qk.stays(),
    queryFn: fetchStaysPayload,
  });
  const wsQ = useQuery({
    queryKey: qk.workspaces(),
    queryFn: fetchWorkspaces,
  });
  const orgsQ = useQuery({
    queryKey: qk.organizations(workspaceId ?? "active"),
    queryFn: fetchOrganizations,
  });
  const propertyIds = propsQ.data?.map((p) => p.id) ?? [];
  const pwQs = useQueries({
    queries: propertyIds.map((pid) => ({
      queryKey: qk.propertyWorkspaces(pid),
      queryFn: () => fetchPropertyMemberships(pid),
    })),
  });
  const closureQs = useQueries({
    queries: propertyIds.map((pid) => ({
      queryKey: qk.propertyClosures(pid),
      queryFn: () => fetchPropertyClosures(pid),
    })),
  });
  const pwPending = propsQ.isPending || (propsQ.data ? pwQs.some((q) => q.isPending) : false);
  const closuresPending = propsQ.isPending || (propsQ.data ? closureQs.some((q) => q.isPending) : false);

  if (propsQ.isPending || staysQ.isPending || wsQ.isPending || orgsQ.isPending || pwPending || closuresPending) {
    return (
      <DeskPage title="Properties" actions={<button className="btn btn--moss">+ Add property</button>}>
        <Loading />
      </DeskPage>
    );
  }
  if (!propsQ.data || !staysQ.data || !wsQ.data || !orgsQ.data || pwQs.some((q) => !q.data) || closureQs.some((q) => !q.data)) {
    return (
      <DeskPage title="Properties" actions={<button className="btn btn--moss">+ Add property</button>}>
        Failed to load.
      </DeskPage>
    );
  }

  const properties = propsQ.data;
  const stays = staysQ.data.stays;
  const closures = closureQs.flatMap((q) => q.data ?? staysQ.data.closures);
  const memberships = pwQs.flatMap((q) => q.data ?? []);
  const wsById = new Map(wsQ.data.map((w) => [w.id, w]));
  for (const m of memberships) {
    if (!wsById.has(m.workspace_id)) {
      wsById.set(m.workspace_id, {
        id: m.workspace_id,
        slug: "",
        name: m.workspace_id,
        timezone: "",
        default_currency: "",
        default_country: "",
        default_locale: "",
      });
    }
  }
  const orgById = new Map(orgsQ.data.map((o) => [o.id, o]));
  const activeWsId = meQ.data?.current_workspace_id ?? (workspaceId ? wsQ.data.find((w) => w.slug === workspaceId)?.id : null) ?? null;

  return (
    <DeskPage
      title="Properties"
      actions={<button className="btn btn--moss">+ Add property</button>}
    >
      <section className="grid grid--cards">
        {properties.map((p) => {
          const propStays = stays.filter((s) => s.property_id === p.id);
          const propClosures = closures.filter((c) => c.property_id === p.id);
          const propMembers = memberships.filter((m) => m.property_id === p.id);
          const ourMembership = activeWsId
            ? propMembers.find((m) => m.workspace_id === activeWsId)
            : undefined;
          const externalMembers = propMembers.filter((m) => m.workspace_id !== activeWsId);
          const clientOrg = p.client_org_id ? orgById.get(p.client_org_id) : undefined;
          return (
            <article key={p.id} className="prop-card">
              <Link className="prop-card__link" to={"/property/" + p.id}>
                <div className={"prop-card__swatch prop-card__swatch--" + p.color}>
                  <span className="prop-card__kind">{p.kind.toUpperCase()}</span>
                </div>
                <div className="prop-card__body">
                  <h3 className="prop-card__name">{p.name}</h3>
                  <div className="prop-card__city">{p.city} · {p.timezone}</div>
                  <div className="prop-card__stats">
                    <span>{propStays.length} stays</span>
                    <span>·</span>
                    <span>{p.areas.length} areas</span>
                    {propClosures.length > 0 && (
                      <>
                        <span>·</span>
                        <span className="muted">
                          {propClosures.length} closure{propClosures.length > 1 ? "s" : ""}
                        </span>
                      </>
                    )}
                  </div>
                  <div className="prop-card__chips">
                    {ourMembership && (
                      <Chip
                        size="sm"
                        tone={ourMembership.membership_role === "owner_workspace" ? "moss" : "sky"}
                      >
                        {MEMBERSHIP_LABEL[ourMembership.membership_role]}
                      </Chip>
                    )}
                    {externalMembers.map((m) => {
                      const ws = wsById.get(m.workspace_id);
                      if (!ws) return null;
                      return (
                        <Chip key={m.workspace_id} size="sm" tone="ghost">
                          {MEMBERSHIP_LABEL[m.membership_role]}: {ws.name === m.workspace_id ? m.label : ws.name}
                        </Chip>
                      );
                    })}
                    {clientOrg && (
                      <Chip size="sm" tone="sand">Client: {clientOrg.name}</Chip>
                    )}
                  </div>
                </div>
              </Link>
              <div className="prop-card__footer">
                <Link to={"/property/" + p.id} className="link">Overview</Link>
                <Link to={"/property/" + p.id + "/closures"} className="link link--muted">
                  Closures →
                </Link>
              </div>
            </article>
          );
        })}
      </section>
    </DeskPage>
  );
}
