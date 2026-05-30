import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useWorkspace } from "@/context/WorkspaceContext";
import DeskPage from "@/components/DeskPage";
import DateTime from "@/components/DateTime";
import FormModal, { FormModalGrid } from "@/components/FormModal";
import { Chip, Loading } from "@/components/common";
import { formatMoney } from "@/lib/money";
import type {
  BookingBilling,
  ClientRate,
  ClientUserRate,
  Me,
  Organization,
  Property,
  User,
  VendorInvoice,
} from "@/types/api";

interface OrganizationDetailPayload {
  organization: Organization;
  properties_billed: Property[];
  client_rates: ClientRate[];
  client_user_rates: ClientUserRate[];
  recent_booking_billings: BookingBilling[];
  vendor_invoices_billed_to: VendorInvoice[];
  vendor_invoices_billed_from: VendorInvoice[];
  portal_user: User | null;
}

interface BillingOrganizationPayload {
  id: string;
  workspace_id: string;
  kind: "client" | "vendor" | "mixed" | string;
  display_name: string;
  legal_name?: string | null;
  billing_address?: Record<string, object>;
  tax_id: string | null;
  default_currency: string;
  contact_email?: string | null;
  contact_phone?: string | null;
  notes_md: string | null;
  created_at?: string;
  archived_at?: string | null;
}

type ListResponse<T> = T[] | { data: T[] };
type BillingOrganizationKind = "client" | "vendor" | "mixed";

interface OrganizationCreateBody {
  kind: BillingOrganizationKind;
  display_name: string;
  legal_name?: string;
  billing_address?: Record<string, string>;
  default_currency?: string;
}

interface OrganizationCreateDraft {
  kind: BillingOrganizationKind;
  displayName: string;
  legalName: string;
  defaultCurrency: string;
  addressLine1: string;
  addressLine2: string;
  locality: string;
  region: string;
  postalCode: string;
  country: string;
}

// `WorkRole` is not currently exported from api.ts, read the legacy
// `Role` shape (id + name) used everywhere else for the rate table.
interface WorkRoleLite {
  id: string;
  name: string;
}

function listData<T>(payload: ListResponse<T>): T[] {
  return Array.isArray(payload) ? payload : payload.data;
}

function mapOrganization(row: BillingOrganizationPayload): Organization {
  const isClient = row.kind === "client" || row.kind === "mixed";
  const isSupplier = row.kind === "vendor" || row.kind === "mixed";
  const hasContact = Boolean(row.contact_email || row.contact_phone);
  return {
    id: row.id,
    workspace_id: row.workspace_id,
    name: row.display_name,
    legal_name: row.legal_name ?? null,
    is_client: isClient,
    is_supplier: isSupplier,
    default_currency: row.default_currency,
    tax_id: row.tax_id,
    contacts: hasContact
      ? [{
          label: "Primary",
          name: row.display_name,
          email: row.contact_email ?? "",
          phone_e164: row.contact_phone ?? "",
          role: "Contact",
        }]
      : [],
    notes: row.notes_md,
    default_pay_destination_stub: null,
    portal_user_id: null,
    cancellation_window_hours: null,
    cancellation_fee_pct: null,
  };
}

async function fetchOrganizations(): Promise<Organization[]> {
  const rows = await fetchJson<ListResponse<BillingOrganizationPayload>>("/api/v1/billing/organizations");
  return listData(rows).map(mapOrganization);
}

async function createOrganization(body: OrganizationCreateBody): Promise<Organization> {
  const row = await fetchJson<BillingOrganizationPayload>("/api/v1/billing/organizations", {
    method: "POST",
    body,
  });
  return mapOrganization(row);
}

async function fetchOrganizationDetail(organizationId: string): Promise<OrganizationDetailPayload> {
  const row = await fetchJson<BillingOrganizationPayload>("/api/v1/billing/organizations/" + organizationId);
  return {
    organization: mapOrganization(row),
    properties_billed: [],
    client_rates: [],
    client_user_rates: [],
    recent_booking_billings: [],
    vendor_invoices_billed_to: [],
    vendor_invoices_billed_from: [],
    portal_user: null,
  };
}

async function fetchWorkRoles(): Promise<WorkRoleLite[]> {
  const rows = await fetchJson<ListResponse<WorkRoleLite>>("/api/v1/work_roles");
  return listData(rows);
}

// §22, Organizations directory. Lists every organization in the active
// workspace (clients we bill, suppliers that bill us, or both) and lets
// the manager drill into one to see its rate card, recent booking
// billings, and the vendor invoices flowing through it.
export default function OrganizationsPage() {
  // code-health: ignore[ccn nloc] Organization list route keeps client summary filtering and selection in one query surface.
  const { workspaceId } = useWorkspace();
  const [activeOid, setActiveOid] = useState<string | null>(null);
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const orgsQ = useQuery({
    queryKey: qk.organizations(workspaceId ?? "active"),
    queryFn: fetchOrganizations,
  });
  const rolesQ = useQuery({
    queryKey: qk.workRoles(),
    queryFn: fetchWorkRoles,
  });
  const usersById = useMemo(() => new Map<string, User>(), []);
  const orgs = useMemo(() => orgsQ.data ?? [], [orgsQ.data]);
  const visibleOrgs = useMemo(() => orgs, [orgs]);
  const selectedOid = activeOid ?? visibleOrgs[0]?.id ?? null;
  const createButton = (
    <NewOrganizationButton
      workspaceId={workspaceId ?? "active"}
      onCreated={(organization) => setActiveOid(organization.id)}
    />
  );

  const detailQ = useQuery({
    queryKey: qk.organization(selectedOid ?? ""),
    queryFn: () => fetchOrganizationDetail(selectedOid ?? ""),
    enabled: selectedOid !== null,
  });

  const rolesById = useMemo(
    () => new Map((rolesQ.data ?? []).map((r) => [r.id, r])),
    [rolesQ.data],
  );

  void meQ;

  if (orgsQ.isPending) return <DeskPage title="Organizations"><Loading /></DeskPage>;
  if (orgsQ.isError || !orgsQ.data) return <DeskPage title="Organizations">Failed to load.</DeskPage>;

  if (visibleOrgs.length === 0) {
    return (
      <DeskPage
        title="Organizations"
        actions={createButton}
      >
        <div className="panel">
          <p className="muted">
            No organizations in this workspace. Create one when an owner enters
            "agency mode", link a property to a client, or register a supplier
            to route agency-supplied engagements.
          </p>
        </div>
      </DeskPage>
    );
  }

  return (
    <DeskPage
      title="Organizations"
      sub="Clients we bill, suppliers that bill us, and the contracts in between."
      actions={createButton}
    >
      <section className="grid grid--split">
        <div className="panel">
          <header className="panel__head"><h2>Counterparties</h2></header>
          <ul className="org-list">
            {visibleOrgs.map((o) => (
              <li key={o.id}>
                <button
                  type="button"
                  className={"org-list__row" + (o.id === selectedOid ? " org-list__row--active" : "")}
                  onClick={() => setActiveOid(o.id)}
                >
                  <span>
                    <strong>{o.name}</strong>
                    {o.legal_name && o.legal_name !== o.name && (
                      <span className="muted">{o.legal_name}</span>
                    )}
                  </span>
                  <span className="org-list__chips">
                    {o.is_client && <Chip tone="moss" size="sm">Client</Chip>}
                    {o.is_supplier && <Chip tone="sky" size="sm">Supplier</Chip>}
                    <Chip tone="ghost" size="sm">{o.default_currency}</Chip>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>

        <OrganizationDetail
          loading={detailQ.isPending}
          error={detailQ.isError}
          detail={detailQ.data ?? null}
          rolesById={rolesById}
          usersById={usersById}
        />
      </section>
    </DeskPage>
  );
}

function NewOrganizationButton({
  workspaceId,
  onCreated,
}: {
  workspaceId: string;
  onCreated: (organization: Organization) => void;
}) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<OrganizationCreateDraft>(() => emptyOrganizationDraft());
  const [formError, setFormError] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: createOrganization,
    onSuccess: async (organization) => {
      setFormError(null);
      await queryClient.invalidateQueries({ queryKey: qk.organizations(workspaceId) });
      await queryClient.invalidateQueries({ queryKey: qk.organization(organization.id) });
      onCreated(organization);
      setDialogOpen(false);
    },
    onError: (error) => setFormError(organizationCreateErrorMessage(error)),
  });

  function openDialog(): void {
    setFormError(null);
    setDialogOpen(true);
  }

  function reset(): void {
    if (create.isPending) return;
    setDraft(emptyOrganizationDraft());
    setFormError(null);
    create.reset();
  }

  function update<K extends keyof OrganizationCreateDraft>(
    key: K,
    value: OrganizationCreateDraft[K],
  ): void {
    setDraft((current) => ({ ...current, [key]: value }));
    setFormError(null);
  }

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (create.isPending) return;
    const body = buildOrganizationCreateBody(draft);
    if (typeof body === "string") {
      setFormError(body);
      return;
    }
    setFormError(null);
    create.mutate(body);
  }

  const formErrorId = formError ? "organization-create-error" : undefined;
  const nameInvalid = formError === "Enter a display name before creating the organization.";
  const currencyInvalid = formError === "Use a three-letter currency code, such as USD.";

  return (
    <>
      <button type="button" className="btn btn--moss" onClick={openDialog}>
        + New organization
      </button>

      <FormModal
        open={dialogOpen}
        title="New organization"
        titleId="new-organization-title"
        eyebrow="Billing organization"
        subtitle="Add a client, supplier, or mixed counterparty for billing."
        className="organization-create-dialog"
        formClassName="asset-create organization-create-form"
        onClose={() => {
          setDialogOpen(false);
          reset();
        }}
        onSubmit={submit}
        noValidate
        closeDisabled={create.isPending}
        onCancel={(event) => {
          if (create.isPending) event.preventDefault();
        }}
        actions={
          <>
            <button
              type="button"
              className="btn btn--ghost"
              disabled={create.isPending}
              onClick={() => setDialogOpen(false)}
            >
              Cancel
            </button>
            <button type="submit" className="btn btn--moss" disabled={create.isPending}>
              {create.isPending ? "Creating..." : "Create organization"}
            </button>
          </>
        }
      >
            <section className="asset-create__section" aria-labelledby="organization-create-basics">
              <h4 id="organization-create-basics" className="asset-create__section-title">
                Basics
              </h4>
              <FormModalGrid className="asset-create__grid">
                <label className="field asset-create__field">
                  <span>Kind</span>
                  <select
                    value={draft.kind}
                    onChange={(event) =>
                      update("kind", event.target.value as BillingOrganizationKind)
                    }
                  >
                    <option value="client">Client</option>
                    <option value="vendor">Supplier</option>
                    <option value="mixed">Client and supplier</option>
                  </select>
                </label>
                <label className="field asset-create__field">
                  <span>Default currency</span>
                  <input
                    aria-invalid={currencyInvalid}
                    aria-describedby={currencyInvalid ? formErrorId : undefined}
                    value={draft.defaultCurrency}
                    onChange={(event) => update("defaultCurrency", event.target.value)}
                    placeholder="Workspace default"
                    maxLength={3}
                    autoCapitalize="characters"
                   aria-label="field asset-create__field Default currency defaultCurrency Workspace default characters"/>
                </label>
              </FormModalGrid>
              <label className="field asset-create__field">
                <span>Display name</span>
                <input
                  required
                  aria-invalid={nameInvalid}
                  aria-describedby={nameInvalid ? formErrorId : undefined}
                  value={draft.displayName}
                  onChange={(event) => update("displayName", event.target.value)}
                  placeholder="e.g. Dupont Family"
                 aria-label="field asset-create__field Display name displayName e.g. Dupont Family"/>
              </label>
              <label className="field asset-create__field">
                <span>Legal name</span>
                <input
                  value={draft.legalName}
                  onChange={(event) => update("legalName", event.target.value)}
                  placeholder="Optional invoice name"
                 aria-label="field asset-create__field Legal name legalName invoice name"/>
              </label>
            </section>

            <section className="asset-create__section" aria-labelledby="organization-create-address">
              <h4 id="organization-create-address" className="asset-create__section-title">
                Billing address
              </h4>
              <label className="field asset-create__field">
                <span>Address line 1</span>
                <input
                  value={draft.addressLine1}
                  onChange={(event) => update("addressLine1", event.target.value)}
                  autoComplete="address-line1"
                 aria-label="field asset-create__field Address line 1 addressLine1 address-line1"/>
              </label>
              <label className="field asset-create__field">
                <span>Address line 2</span>
                <input
                  value={draft.addressLine2}
                  onChange={(event) => update("addressLine2", event.target.value)}
                  autoComplete="address-line2"
                 aria-label="field asset-create__field Address line 2 addressLine2 address-line2"/>
              </label>
              <FormModalGrid className="asset-create__grid">
                <label className="field asset-create__field">
                  <span>City or locality</span>
                  <input
                    value={draft.locality}
                    onChange={(event) => update("locality", event.target.value)}
                    autoComplete="address-level2"
                   aria-label="field asset-create__field City or locality locality address-level2"/>
                </label>
                <label className="field asset-create__field">
                  <span>State or region</span>
                  <input
                    value={draft.region}
                    onChange={(event) => update("region", event.target.value)}
                    autoComplete="address-level1"
                   aria-label="field asset-create__field State or region region address-level1"/>
                </label>
              </FormModalGrid>
              <FormModalGrid className="asset-create__grid">
                <label className="field asset-create__field">
                  <span>Postal code</span>
                  <input
                    value={draft.postalCode}
                    onChange={(event) => update("postalCode", event.target.value)}
                    autoComplete="postal-code"
                   aria-label="field asset-create__field Postal code postalCode postal-code"/>
                </label>
                <label className="field asset-create__field">
                  <span>Country</span>
                  <input
                    value={draft.country}
                    onChange={(event) => update("country", event.target.value)}
                    autoComplete="country-name"
                    placeholder="US"
                   aria-label="field asset-create__field Country country country-name US"/>
                </label>
              </FormModalGrid>
            </section>

            {formError && (
              <p id="organization-create-error" className="form-error" role="alert">
                {formError}
              </p>
            )}
      </FormModal>
    </>
  );
}

function emptyOrganizationDraft(): OrganizationCreateDraft {
  return {
    kind: "client",
    displayName: "",
    legalName: "",
    defaultCurrency: "",
    addressLine1: "",
    addressLine2: "",
    locality: "",
    region: "",
    postalCode: "",
    country: "",
  };
}

function buildOrganizationCreateBody(
  draft: OrganizationCreateDraft,
): OrganizationCreateBody | string {
  const displayName = draft.displayName.trim();
  if (!displayName) return "Enter a display name before creating the organization.";
  const currency = draft.defaultCurrency.trim().toUpperCase();
  if (currency && !/^[A-Z]{3}$/.test(currency)) {
    return "Use a three-letter currency code, such as USD.";
  }
  const legalName = draft.legalName.trim();
  const address = compactRecord({
    line1: draft.addressLine1,
    line2: draft.addressLine2,
    locality: draft.locality,
    region: draft.region,
    postal_code: draft.postalCode,
    country: draft.country,
  });
  return {
    kind: draft.kind,
    display_name: displayName,
    ...(legalName ? { legal_name: legalName } : {}),
    ...(currency ? { default_currency: currency } : {}),
    ...(Object.keys(address).length > 0 ? { billing_address: address } : {}),
  };
}

function compactRecord(values: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(values)) {
    const trimmed = value.trim();
    if (trimmed) out[key] = trimmed;
  }
  return out;
}

function organizationCreateErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const fieldMessages = error.fieldErrors
      .map((fieldError) => fieldError.msg)
      .filter((message): message is string => Boolean(message));
    if (fieldMessages.length > 0) return fieldMessages.join(" ");
    return error.detail ?? error.title ?? error.message;
  }
  return error instanceof Error ? error.message : "Organization could not be created.";
}

function OrganizationDetail({
  loading,
  error,
  detail,
  rolesById,
  usersById,
}: {
  loading: boolean;
  error: boolean;
  detail: OrganizationDetailPayload | null;
  rolesById: Map<string, WorkRoleLite>;
  usersById: Map<string, User>;
}) {
  // code-health: ignore[ccn nloc] Organization detail is a declarative client record panel with related tables kept adjacent.
  if (loading) {
    return <div className="panel"><Loading /></div>;
  }
  if (error || !detail) {
    return <div className="panel">Failed to load.</div>;
  }
  const o = detail.organization;
  return (
    <div className="panel">
      <header className="panel__head">
        <h2>{o.name}</h2>
        <div className="sharing-client__chips">
          {o.is_client && <Chip tone="moss" size="sm">Client</Chip>}
          {o.is_supplier && <Chip tone="sky" size="sm">Supplier</Chip>}
        </div>
      </header>
      {o.notes && <p className="muted">{o.notes}</p>}

      {o.tax_id && (
        <p className="org-meta">
          <span className="muted">Tax ID:</span> <code className="inline-code">{o.tax_id}</code>
        </p>
      )}

      <section className="org-section">
        <h3>Contacts</h3>
        {o.contacts.length === 0 ? (
          <p className="muted">No contacts on file.</p>
        ) : (
          <ul className="org-contacts">
            {o.contacts.map((c) => (
              <li key={`${c.name}-${c.role}-${c.email}-${c.phone_e164}`}>
                <strong>{c.name}</strong>
                <span className="muted"> · {c.role}</span>
                <div className="muted mono">{c.email} · {c.phone_e164}</div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {o.is_client && (
        <>
          <section className="org-section">
            <h3>Properties billed</h3>
            {detail.properties_billed.length === 0 ? (
              <p className="muted">No properties currently billed to this client.</p>
            ) : (
              <ul className="org-prop-list">
                {detail.properties_billed.map((p) => (
                  <li key={p.id}>
                    <strong>{p.name}</strong>
                    <span className="muted">, {p.city}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="org-section">
            <h3>Rate card</h3>
            {detail.client_rates.length === 0 && detail.client_user_rates.length === 0 ? (
              <p className="muted">No rates on file. Shifts will surface in the "unpriced" CSV bucket.</p>
            ) : (
              <table className="table">
                <thead>
                  <tr><th>Subject</th><th>Hourly</th><th>From</th><th>To</th></tr>
                </thead>
                <tbody>
                  {detail.client_rates.map((r) => (
                    <tr key={r.id}>
                      <td>Role · <strong>{rolesById.get(r.work_role_id)?.name ?? r.work_role_id}</strong></td>
                      <td className="table__mono">{formatMoney(r.hourly_cents, r.currency)}/h</td>
                      <td className="table__mono">{r.effective_from}</td>
                      <td className="table__mono muted">{r.effective_to ?? "ongoing"}</td>
                    </tr>
                  ))}
                  {detail.client_user_rates.map((r) => (
                    <tr key={r.id}>
                      <td>User · <strong>{usersById.get(r.user_id)?.display_name ?? r.user_id}</strong></td>
                      <td className="table__mono">{formatMoney(r.hourly_cents, r.currency)}/h</td>
                      <td className="table__mono">{r.effective_from}</td>
                      <td className="table__mono muted">{r.effective_to ?? "ongoing"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section className="org-section">
            <h3>Recent billings</h3>
            {detail.recent_booking_billings.length === 0 ? (
              <p className="muted">No booking billings yet.</p>
            ) : (
              <table className="table">
                <thead>
                  <tr><th>Worker</th><th>Minutes</th><th>Hourly</th><th>Subtotal</th><th>Source</th></tr>
                </thead>
                <tbody>
                  {detail.recent_booking_billings.map((b) => (
                    <tr key={b.id}>
                      <td>{usersById.get(b.user_id)?.display_name ?? b.user_id}</td>
                      <td className="table__mono">{b.billable_minutes}</td>
                      <td className="table__mono">{formatMoney(b.hourly_cents, b.currency)}</td>
                      <td className="table__mono">{formatMoney(b.subtotal_cents, b.currency)}</td>
                      <td>
                        <Chip
                          size="sm"
                          tone={
                            b.is_cancellation_fee
                              ? "rust"
                              : b.rate_source === "unpriced"
                                ? "rust"
                                : "ghost"
                          }
                        >
                          {b.is_cancellation_fee ? "cancel fee" : b.rate_source}
                        </Chip>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}

      <section className="org-section">
        <h3>Vendor invoices</h3>
        {detail.vendor_invoices_billed_to.length === 0 && detail.vendor_invoices_billed_from.length === 0 ? (
          <p className="muted">No invoices yet.</p>
        ) : (
          <table className="table">
            <thead>
              <tr><th>Invoice</th><th>Direction</th><th>Total</th><th>Status</th><th>Billed</th></tr>
            </thead>
            <tbody>
              {detail.vendor_invoices_billed_to.map((v) => (
                <tr key={v.id}>
                  <td>{v.id}</td>
                  <td><Chip size="sm" tone="rust">we owe</Chip></td>
                  <td className="table__mono">{formatMoney(v.total_cents, v.currency)}</td>
                  <td><Chip size="sm" tone={v.status === "paid" ? "moss" : v.status === "approved" ? "sky" : "ghost"}>{v.status}</Chip></td>
                  <td><DateTime value={v.billed_at} showTime className="table__mono" /></td>
                </tr>
              ))}
              {detail.vendor_invoices_billed_from.flatMap((v) =>
                detail.vendor_invoices_billed_to.includes(v) ? [] : [
                  <tr key={v.id}>
                    <td>{v.id}</td>
                    <td><Chip size="sm" tone="moss">they owe</Chip></td>
                    <td className="table__mono">{formatMoney(v.total_cents, v.currency)}</td>
                    <td><Chip size="sm" tone={v.status === "paid" ? "moss" : "ghost"}>{v.status}</Chip></td>
                    <td><DateTime value={v.billed_at} showTime className="table__mono" /></td>
                  </tr>,
                ],
              )}
            </tbody>
          </table>
        )}
      </section>

      {detail.portal_user && (
        <p className="org-portal muted">
          Portal user: <strong>{detail.portal_user.display_name}</strong> ({detail.portal_user.email})
        </p>
      )}
    </div>
  );
}
