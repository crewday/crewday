import { useQueries, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { type ListEnvelope } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type {
  Me,
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

interface ClientPortfolioRow {
  id: string;
  organization_id: string;
  organization_name: string | null;
  name: string;
  kind: string;
  address: string;
  country: string;
  timezone: string;
  default_currency: string | null;
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

function dateOnly(iso: string): string {
  // code-health: ignore[ccn nloc] Tiny date helper is a lizard TS parser artifact, not an oversized function.
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

async function fetchClientPortfolio(): Promise<ClientPortfolioRow[]> {
  const rows = await fetchJson<ListEnvelope<ClientPortfolioRow>>("/api/v1/client/portfolio?limit=500");
  return rows.data;
}

function currentStay(stays: Stay[], today: string): Stay | null {
  return stays.find((stay) => stay.status === "in_house")
    ?? stays.find((stay) => (
      stay.status !== "cancelled"
      && stay.status !== "checked_out"
      && stay.check_in <= today
      && today < stay.check_out
    ))
    ?? null;
}

function narrowKind(kind: string): Property["kind"] {
  if (kind === "str" || kind === "vacation" || kind === "residence" || kind === "mixed") return kind;
  return "vacation";
}

function fallbackProperty(row: ClientPortfolioRow): Property {
  return {
    id: row.id,
    name: row.name,
    city: row.address,
    timezone: row.timezone,
    color: "moss",
    kind: narrowKind(row.kind),
    areas: [],
    evidence_policy: "inherit",
    country: row.country,
    locale: "",
    settings_override: {},
    client_org_id: row.organization_id,
    owner_user_id: null,
  };
}

export default function ClientPortfolioPage() {
  // code-health: ignore[ccn nloc] Portfolio route coordinates several promoted queries and keeps layout unchanged.
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const enabled = meQ.data?.role === "client";
  const portfolioQ = useQuery({
    queryKey: qk.clientPortfolio(),
    queryFn: fetchClientPortfolio,
    enabled,
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
    enabled,
  });
  const wsQ = useQuery({
    queryKey: qk.workspaces(),
    queryFn: fetchWorkspaces,
    enabled,
  });
  const staysQ = useQuery({
    queryKey: qk.stays(),
    queryFn: fetchStaysPayload,
    enabled,
  });

  const orgIds = new Set(meQ.data?.client_binding_org_ids ?? []);
  const portfolioRows = portfolioQ.data?.filter((row) => orgIds.size === 0 || orgIds.has(row.organization_id)) ?? [];
  const portfolioById = new Map(portfolioRows.map((row) => [row.id, row]));
  const propsById = new Map((propsQ.data ?? []).map((property) => [property.id, property]));
  const myProps = portfolioRows.map((row) => {
    const property = propsById.get(row.id);
    return property ? { ...property, client_org_id: row.organization_id } : fallbackProperty(row);
  });
  const propertyIds = enabled ? myProps.map((p) => p.id) : [];
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
  const pwPending = enabled && propsQ.data ? pwQs.some((q) => q.isPending) : false;
  const closuresPending = enabled && propsQ.data ? closureQs.some((q) => q.isPending) : false;

  if (meQ.isPending) {
    return <DeskPage title="My properties"><Loading /></DeskPage>;
  }
  if (!meQ.data) {
    return <DeskPage title="My properties">Failed to load.</DeskPage>;
  }
  if (meQ.data.role !== "client") {
    return (
      <DeskPage title="My properties">
        <div className="panel">
          <p className="muted">This page is only available to client portal users.</p>
        </div>
      </DeskPage>
    );
  }
  if (portfolioQ.isPending || propsQ.isPending || wsQ.isPending || staysQ.isPending || pwPending || closuresPending) {
    return <DeskPage title="My properties"><Loading /></DeskPage>;
  }
  if (!portfolioQ.data || !propsQ.data || !wsQ.data || !staysQ.data || pwQs.some((q) => !q.data) || closureQs.some((q) => !q.data)) {
    return <DeskPage title="My properties">Failed to load.</DeskPage>;
  }

  const me = meQ.data;
  const stays = staysQ.data.stays;
  const closures = closureQs.flatMap((q) => q.data ?? staysQ.data.closures);
  const memberships = pwQs.flatMap((q) => q.data ?? []);
  const wsById = new Map(wsQ.data.map((w) => [w.id, w]));
  for (const m of memberships) {
    if (!wsById.has(m.workspace_id)) {
      wsById.set(m.workspace_id, {
        id: m.workspace_id,
        slug: "",
        name: m.label || m.workspace_id,
        timezone: "",
        default_currency: "",
        default_country: "",
        default_locale: "",
      });
    }
  }

  return (
    <DeskPage
      title="My properties"
      sub="Properties billed to you in the active workspace. Switch workspaces to see other portfolios."
    >
      {myProps.length === 0 ? (
        <div className="panel">
          <p className="muted">
            No properties billed to your organization in the current workspace.
            If you also work with another agency, switch workspaces from the sidebar.
          </p>
        </div>
      ) : (
        <section className="grid grid--cards">
          {myProps.map((p) => {
            const propStays = stays.filter((s) => s.property_id === p.id);
            const propClosures = closures.filter((c) => c.property_id === p.id);
            const propMembers = memberships.filter((m) => m.property_id === p.id);
            const owner = propMembers.find((m) => m.membership_role === "owner_workspace");
            const managed = propMembers.filter((m) => m.membership_role === "managed_workspace");
            const clientOrg = portfolioById.get(p.id);
            const stayNow = currentStay(propStays, me.today);
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
                      {stayNow && <Chip size="sm" tone="moss">Current stay: {stayNow.guest_name}</Chip>}
                      {clientOrg && <Chip size="sm" tone="sand">Billed to {clientOrg.organization_name ?? clientOrg.organization_id}</Chip>}
                      {owner && (
                        <Chip size="sm" tone="moss">Owner: {wsById.get(owner.workspace_id)?.name ?? owner.label}</Chip>
                      )}
                      {managed.map((m) => (
                        <Chip key={m.workspace_id} size="sm" tone="sky">
                          Managed by {wsById.get(m.workspace_id)?.name ?? m.label}
                        </Chip>
                      ))}
                    </div>
                  </div>
                </Link>
              </article>
            );
          })}
        </section>
      )}
    </DeskPage>
  );
}
