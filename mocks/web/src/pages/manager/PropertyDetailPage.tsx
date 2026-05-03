import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import AgentPreferencesPanel from "@/components/AgentPreferencesPanel";
import { Avatar, Chip, Loading } from "@/components/common";
import { useWorkspace } from "@/context/WorkspaceContext";
import type {
  Asset,
  AssetDocument,
  AvailableWorkspace,
  Employee,
  EntitySettingsPayload,
  Instruction,
  InventoryItem,
  Me,
  Organization,
  Property,
  PropertyClosure,
  PropertyWorkspace,
  SettingDefinition,
  Stay,
  Task,
  TaskStatus,
  Unit,
  User,
  Workspace,
} from "@/types/api";

interface PropertyDetail {
  property: Property;
  property_tasks: Task[];
  stays: Stay[];
  inventory: InventoryItem[];
  instructions: Instruction[];
  closures: PropertyClosure[];
  assets: Asset[];
  asset_documents: AssetDocument[];
  // §02 + §22 — multi-belonging context.
  memberships: PropertyWorkspace[];
  membership_workspaces: Workspace[];
  client_org: Organization | null;
  owner_user: User | null;
  active_workspace_id: string;
}

const STATUS_TONE: Record<TaskStatus, "moss" | "sky" | "ghost" | "rust"> = {
  completed: "moss",
  in_progress: "sky",
  pending: "ghost",
  scheduled: "ghost",
  skipped: "rust",
  cancelled: "rust",
  overdue: "rust",
};

const MEMBERSHIP_LABEL: Record<string, string> = {
  owner_workspace: "Owner workspace",
  managed_workspace: "Managed workspace",
  observer_workspace: "Observer workspace",
};

const MEMBERSHIP_TONE: Record<string, "moss" | "sky" | "ghost"> = {
  owner_workspace: "moss",
  managed_workspace: "sky",
  observer_workspace: "ghost",
};

function fmtDayMon(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

function fmtDayMonTime(iso: string): string {
  const d = new Date(iso);
  const date = d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return date + " · " + time;
}

function formatValue(value: unknown): string {
  if (value === true) return "yes";
  if (value === false) return "no";
  if (value === null || value === undefined) return "--";
  return String(value);
}

function SettingsOverridePanel({
  overrides,
  resolved,
  catalog,
}: {
  overrides: Record<string, unknown>;
  resolved: Record<string, { value: unknown; source: string }>;
  catalog: SettingDefinition[];
}) {
  const propertyScoped = catalog.filter((d) => d.override_scope.includes("P"));

  return (
    <div className="panel">
      <header className="panel__head"><h2>Settings overrides</h2></header>
      <p className="muted">
        Property-scoped settings. Overridden values take precedence over workspace defaults.
      </p>
      <table className="table">
        <thead>
          <tr>
            <th>Setting</th>
            <th>Effective value</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {propertyScoped.map((def) => {
            const hasOverride = def.key in overrides;
            const res = resolved[def.key];
            return (
              <tr key={def.key}>
                <td title={def.description}>
                  <code className="inline-code">{def.key}</code>
                  <span className="muted setting-label">{def.label}</span>
                </td>
                <td>
                  {hasOverride ? (
                    <strong>{formatValue(res?.value)}</strong>
                  ) : (
                    <span className="muted">{formatValue(res?.value)}</span>
                  )}
                </td>
                <td>
                  {hasOverride ? (
                    <Chip tone="moss" size="sm">overridden</Chip>
                  ) : (
                    <span className="muted">inherited ({res?.source ?? "catalog"})</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// §02 + §22 — Sharing & client tab. Lists every workspace this
// property belongs to and the client organization it bills to.
// "Invite agency" / "Revoke" / "Switch agency" buttons are mock
// stubs (read-only visualisation per the /specs decision); they
// post to the mock backend so the UI reacts but no spec rule is
// crossed yet.
function SharingPanel({
  detail,
  meAvailable,
}: {
  detail: PropertyDetail;
  meAvailable: AvailableWorkspace[];
}) {
  const queryClient = useQueryClient();
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const [confirm, setConfirm] = useState<
    | { kind: "share"; workspaceId: string }
    | { kind: "revoke"; workspaceId: string }
    | null
  >(null);
  const [shareTarget, setShareTarget] = useState<string>("");

  useEffect(() => {
    const el = dialogRef.current;
    if (!el) return;
    if (confirm && !el.open) el.showModal();
    if (!confirm && el.open) el.close();
  }, [confirm]);

  const shareMu = useMutation({
    mutationFn: (vars: { workspace_id: string }) =>
      fetchJson<PropertyWorkspace>("/api/v1/property_workspaces/share", {
        method: "POST",
        body: {
          property_id: detail.property.id,
          workspace_id: vars.workspace_id,
          membership_role: "managed_workspace",
        },
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.property(detail.property.id) });
      void queryClient.invalidateQueries({ queryKey: qk.propertyWorkspaces() });
      setConfirm(null);
    },
  });
  const revokeMu = useMutation({
    mutationFn: (vars: { workspace_id: string }) =>
      fetchJson<{ ok: boolean }>("/api/v1/property_workspaces/revoke", {
        method: "POST",
        body: {
          property_id: detail.property.id,
          workspace_id: vars.workspace_id,
        },
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.property(detail.property.id) });
      void queryClient.invalidateQueries({ queryKey: qk.propertyWorkspaces() });
      setConfirm(null);
    },
  });

  const wsById = new Map(detail.membership_workspaces.map((w) => [w.id, w]));
  const linkedIds = new Set(detail.memberships.map((m) => m.workspace_id));
  const owner = detail.memberships.find((m) => m.membership_role === "owner_workspace");
  const isOwnerSurface = owner?.workspace_id === detail.active_workspace_id;
  const shareCandidates = meAvailable.filter((a) => !linkedIds.has(a.workspace.id));

  return (
    <div className="panel">
      <header className="panel__head">
        <h2>Sharing &amp; client</h2>
        {isOwnerSurface && shareCandidates.length > 0 && (
          <div className="sharing-add">
            <label className="field field--inline">
              <span className="muted">Workspace</span>
              <select
                value={shareTarget}
                onChange={(e) => setShareTarget(e.target.value)}
              >
                <option value="">Invite a workspace…</option>
                {shareCandidates.map((c) => (
                  <option key={c.workspace.id} value={c.workspace.id}>
                    {c.workspace.name}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="btn btn--moss btn--sm"
              disabled={!shareTarget}
              onClick={() => setConfirm({ kind: "share", workspaceId: shareTarget })}
            >
              Invite as agency
            </button>
          </div>
        )}
      </header>
      <p className="muted">
        Multi-belonging property. The owner workspace controls who else may see or manage it.
        Worker shifts and work orders carry their own workspace tag forward, so payroll, billing and
        history stay separated even when several teams share the same villa.
      </p>

      <table className="table">
        <thead>
          <tr><th>Workspace</th><th>Membership</th><th>Since</th><th></th></tr>
        </thead>
        <tbody>
          {detail.memberships.map((m) => {
            const ws = wsById.get(m.workspace_id);
            const isCurrent = m.workspace_id === detail.active_workspace_id;
            const canRevoke = isOwnerSurface && m.membership_role !== "owner_workspace";
            return (
              <tr key={m.workspace_id}>
                <td>
                  <strong>{ws?.name ?? m.workspace_id}</strong>
                  {isCurrent && <span className="muted"> (current view)</span>}
                </td>
                <td>
                  <Chip tone={MEMBERSHIP_TONE[m.membership_role] ?? "ghost"} size="sm">
                    {MEMBERSHIP_LABEL[m.membership_role] ?? m.membership_role}
                  </Chip>
                </td>
                <td className="table__mono">{fmtDayMon(m.added_at)}</td>
                <td>
                  {canRevoke && (
                    <button
                      type="button"
                      className="btn btn--ghost btn--sm"
                      onClick={() => setConfirm({ kind: "revoke", workspaceId: m.workspace_id })}
                    >
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <div className="sharing-client">
        <h3>Billing client</h3>
        {detail.client_org ? (
          <div className="sharing-client__row">
            <div>
              <strong>{detail.client_org.name}</strong>
              {detail.client_org.legal_name && (
                <div className="muted">{detail.client_org.legal_name}</div>
              )}
              {detail.client_org.tax_id && (
                <div className="muted mono">{detail.client_org.tax_id}</div>
              )}
            </div>
            <div className="sharing-client__chips">
              <Chip tone="sand" size="sm">{detail.client_org.default_currency}</Chip>
              {detail.client_org.is_client && <Chip tone="moss" size="sm">Client</Chip>}
              {detail.client_org.is_supplier && <Chip tone="sky" size="sm">Supplier</Chip>}
            </div>
          </div>
        ) : (
          <p className="muted">
            No client organization linked. Shifts and vendor invoices for this property are
            paid by the workspace itself; no client-billing rollup applies.
          </p>
        )}
        {detail.owner_user && (
          <div className="sharing-client__owner">
            <span className="muted">Owner of record:</span>{" "}
            <strong>{detail.owner_user.display_name}</strong>
          </div>
        )}
      </div>

      <dialog className="modal" ref={dialogRef} onClose={() => setConfirm(null)}>
        {confirm && (
          <div className="modal__body">
            <h3 className="modal__title">
              {confirm.kind === "share" ? "Invite this workspace as agency?" : "Revoke this workspace?"}
            </h3>
            <p className="modal__sub">
              {confirm.kind === "share"
                ? "Adds a managed_workspace link. The workspace gains operational access — its members can dispatch workers and create work orders here. Acceptance and invoicing remain bound to the owner workspace's policy."
                : "Removes the property_workspace link. In production this is approval-gated. The mock skips the approval and applies it immediately so you can see the result."}
            </p>
            <div className="modal__actions">
              <button
                type="button"
                className="btn btn--ghost"
                onClick={() => setConfirm(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className={"btn " + (confirm.kind === "share" ? "btn--moss" : "btn--rust")}
                disabled={shareMu.isPending || revokeMu.isPending}
                onClick={() => {
                  if (confirm.kind === "share") shareMu.mutate({ workspace_id: confirm.workspaceId });
                  else revokeMu.mutate({ workspace_id: confirm.workspaceId });
                }}
              >
                {confirm.kind === "share" ? "Invite" : "Revoke"}
              </button>
            </div>
          </div>
        )}
      </dialog>
    </div>
  );
}

type UnitFormState = {
  name: string;
  ordinal: string;
  default_checkin_time: string;
  default_checkout_time: string;
  max_guests: string;
};

type UnitDialog =
  | { kind: "add" }
  | { kind: "edit"; unit: Unit }
  | { kind: "delete"; unit: Unit }
  | null;

function unitFormFrom(unit: Unit | null, nextOrdinal: number): UnitFormState {
  return {
    name: unit?.name ?? "",
    ordinal: String(unit?.ordinal ?? nextOrdinal),
    default_checkin_time: unit?.default_checkin_time ?? "16:00",
    default_checkout_time: unit?.default_checkout_time ?? "10:00",
    max_guests: unit?.max_guests ? String(unit.max_guests) : "",
  };
}

function unitPayload(form: UnitFormState) {
  return {
    name: form.name.trim(),
    ordinal: Number(form.ordinal),
    default_checkin_time: form.default_checkin_time || null,
    default_checkout_time: form.default_checkout_time || null,
    max_guests: form.max_guests ? Number(form.max_guests) : null,
  };
}

function displayTime(value: string | null): string {
  return value ?? "Inherited";
}

function displayGuests(value: number | null): string {
  return value === null ? "No limit" : String(value);
}

function UnitsPanel({ propertyId, units }: { propertyId: string; units: Unit[] }) {
  const queryClient = useQueryClient();
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const orderedUnits = [...units].sort((a, b) => {
    if (a.ordinal !== b.ordinal) return a.ordinal - b.ordinal;
    return a.name.localeCompare(b.name);
  });
  const [dialog, setDialog] = useState<UnitDialog>(null);
  const [form, setForm] = useState<UnitFormState>(() =>
    unitFormFrom(null, orderedUnits.length),
  );

  useEffect(() => {
    const el = dialogRef.current;
    if (!el) return;
    if (dialog && !el.open) el.showModal();
    if (!dialog && el.open) el.close();
  }, [dialog]);

  const closeDialog = () => setDialog(null);
  const openAdd = () => {
    setForm(unitFormFrom(null, orderedUnits.length));
    setDialog({ kind: "add" });
  };
  const openEdit = (unit: Unit) => {
    setForm(unitFormFrom(unit, orderedUnits.length));
    setDialog({ kind: "edit", unit });
  };

  const invalidateUnits = () => {
    void queryClient.invalidateQueries({ queryKey: qk.propertyUnits(propertyId) });
    void queryClient.invalidateQueries({ queryKey: qk.property(propertyId) });
    void queryClient.invalidateQueries({ queryKey: qk.audit() });
  };

  const saveMu = useMutation({
    mutationFn: (vars: { kind: "add" | "edit"; unitId?: string; form: UnitFormState }) => {
      const body = unitPayload(vars.form);
      if (vars.kind === "add") {
        return fetchJson<Unit>("/api/v1/properties/" + propertyId + "/units", {
          method: "POST",
          body,
        });
      }
      return fetchJson<Unit>("/api/v1/units/" + vars.unitId, {
        method: "PATCH",
        body,
      });
    },
    onSuccess: () => {
      invalidateUnits();
      closeDialog();
    },
  });

  const deleteMu = useMutation({
    mutationFn: (unitId: string) =>
      fetchJson<Unit>("/api/v1/units/" + unitId, { method: "DELETE" }),
    onSuccess: () => {
      invalidateUnits();
      closeDialog();
    },
  });

  const canDelete = orderedUnits.length > 1;
  const busy = saveMu.isPending || deleteMu.isPending;
  const mutationError = saveMu.error ?? deleteMu.error;

  return (
    <div className="panel property-units">
      <header className="panel__head">
        <div className="panel__head-stack">
          <h2>Units</h2>
          <p className="panel__sub">
            Bookable spaces within this property. Stays, iCal feeds and guest defaults attach here.
          </p>
        </div>
        <button type="button" className="btn btn--moss btn--sm" onClick={openAdd}>
          Add unit
        </button>
      </header>

      <table className="table property-units__table">
        <thead>
          <tr>
            <th>Unit</th>
            <th>Order</th>
            <th>Default stay window</th>
            <th>Max guests</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {orderedUnits.map((unit) => (
            <tr key={unit.id}>
              <td>
                <Link className="link property-units__name" to={"/property/" + propertyId + "/units/" + unit.id}>
                  {unit.name}
                </Link>
                {Object.keys(unit.welcome_overrides_json).length > 0 && (
                  <span className="table__sub"> Welcome overrides</span>
                )}
              </td>
              <td className="table__mono">{unit.ordinal}</td>
              <td className="table__mono">
                {displayTime(unit.default_checkin_time)} / {displayTime(unit.default_checkout_time)}
              </td>
              <td>{displayGuests(unit.max_guests)}</td>
              <td>
                <div className="property-units__actions">
                  <button
                    type="button"
                    className="btn btn--ghost btn--sm"
                    onClick={() => openEdit(unit)}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="btn btn--ghost btn--sm"
                    onClick={() => setDialog({ kind: "delete", unit })}
                  >
                    Delete
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <dialog className="modal" ref={dialogRef} onClose={closeDialog}>
        {dialog?.kind === "delete" ? (
          <div className="modal__body">
            <h3 className="modal__title">Delete {dialog.unit.name}?</h3>
            <p className="modal__sub">
              This soft-deletes the unit and removes it from active unit pickers. Existing history keeps
              its unit reference.
            </p>
            {!canDelete && (
              <p className="unit-delete-warning">
                Cannot delete the last live unit. Every property must keep at least one live unit.
              </p>
            )}
            {mutationError && <p className="unit-modal__error">{mutationError.message}</p>}
            <div className="modal__actions">
              <button type="button" className="btn btn--ghost" onClick={closeDialog}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn--rust"
                disabled={!canDelete || busy}
                onClick={() => deleteMu.mutate(dialog.unit.id)}
              >
                Delete unit
              </button>
            </div>
          </div>
        ) : dialog ? (
          <form
            className="modal__body unit-modal"
            onSubmit={(e) => {
              e.preventDefault();
              saveMu.mutate({
                kind: dialog.kind,
                unitId: dialog.kind === "edit" ? dialog.unit.id : undefined,
                form,
              });
            }}
          >
            <h3 className="modal__title">
              {dialog.kind === "add" ? "Add unit" : "Edit unit"}
            </h3>
            <div className="unit-modal__grid">
              <label className="field">
                <span>Name</span>
                <input
                  required
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                />
              </label>
              <label className="field">
                <span>Order</span>
                <input
                  required
                  type="number"
                  min="0"
                  step="1"
                  value={form.ordinal}
                  onChange={(e) => setForm({ ...form, ordinal: e.target.value })}
                />
              </label>
              <label className="field">
                <span>Default check-in</span>
                <input
                  required
                  type="time"
                  value={form.default_checkin_time}
                  onChange={(e) => setForm({ ...form, default_checkin_time: e.target.value })}
                />
              </label>
              <label className="field">
                <span>Default check-out</span>
                <input
                  required
                  type="time"
                  value={form.default_checkout_time}
                  onChange={(e) => setForm({ ...form, default_checkout_time: e.target.value })}
                />
              </label>
              <label className="field">
                <span>Max guests</span>
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={form.max_guests}
                  onChange={(e) => setForm({ ...form, max_guests: e.target.value })}
                />
              </label>
            </div>
            {mutationError && <p className="unit-modal__error">{mutationError.message}</p>}
            <div className="modal__actions">
              <button type="button" className="btn btn--ghost" onClick={closeDialog}>
                Cancel
              </button>
              <button type="submit" className="btn btn--moss" disabled={busy}>
                {dialog.kind === "add" ? "Add unit" : "Save changes"}
              </button>
            </div>
          </form>
        ) : null}
      </dialog>
    </div>
  );
}

type Tab = "overview" | "units" | "assets" | "sharing" | "settings";

export default function PropertyDetailPage() {
  const { pid = "" } = useParams<{ pid: string }>();
  const [activeTab, setActiveTab] = useState<Tab>("overview");
  const [unitsRevealed, setUnitsRevealed] = useState(false);
  const { workspaceId } = useWorkspace();

  useEffect(() => {
    setActiveTab("overview");
    setUnitsRevealed(false);
  }, [pid]);

  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const detailQ = useQuery({
    queryKey: qk.property(pid),
    queryFn: () => fetchJson<PropertyDetail>("/api/v1/properties/" + pid),
    enabled: pid !== "",
  });
  const unitsQ = useQuery({
    queryKey: qk.propertyUnits(pid),
    queryFn: () => fetchJson<Unit[]>("/api/v1/properties/" + pid + "/units"),
    enabled: pid !== "",
  });
  const empsQ = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });
  const settingsQ = useQuery({
    queryKey: qk.propertySettings(pid),
    queryFn: () => fetchJson<EntitySettingsPayload>("/api/v1/properties/" + pid + "/settings"),
    enabled: pid !== "" && activeTab === "settings",
  });
  const catalogQ = useQuery({
    queryKey: qk.settingsCatalog(),
    queryFn: () => fetchJson<SettingDefinition[]>("/api/v1/settings/catalog"),
    enabled: activeTab === "settings",
  });
  void workspaceId;  // forces re-render on switch via React state subscription

  if (detailQ.isPending || unitsQ.isPending || empsQ.isPending) {
    return <DeskPage title="Property"><Loading /></DeskPage>;
  }
  if (!detailQ.data || !unitsQ.data || !empsQ.data) {
    return <DeskPage title="Property">Failed to load.</DeskPage>;
  }

  const { property, property_tasks, stays, assets, asset_documents: _asset_documents } = detailQ.data;
  void _asset_documents;
  const empsById = new Map(empsQ.data.map((e) => [e.id, e]));
  const units = unitsQ.data;
  const showUnitsTab = units.length > 1 || unitsRevealed;
  const openUnits = () => {
    setUnitsRevealed(true);
    setActiveTab("units");
  };

  return (
    <DeskPage
      title={property.name}
      sub={property.city + " · " + property.timezone}
      actions={
        <div className="property-actions">
          {!showUnitsTab && (
            <button type="button" className="btn btn--ghost btn--sm" onClick={openUnits}>
              Manage units
            </button>
          )}
          <button type="button" className="btn btn--moss">Edit property</button>
        </div>
      }
      overflow={[{ label: "New task", onSelect: () => undefined }]}
    >
      <nav className="tabs tabs--h">
        <a
          className={"tab-link" + (activeTab === "overview" ? " tab-link--active" : "")}
          onClick={() => setActiveTab("overview")}
        >
          Overview
        </a>
        <a className="tab-link">Areas</a>
        <a className="tab-link">Stays</a>
        {showUnitsTab && (
          <a
            className={"tab-link" + (activeTab === "units" ? " tab-link--active" : "")}
            onClick={openUnits}
          >
            Units
          </a>
        )}
        <a
          className={"tab-link" + (activeTab === "assets" ? " tab-link--active" : "")}
          onClick={() => setActiveTab("assets")}
        >
          Assets
        </a>
        <a className="tab-link">Instructions</a>
        <a className="tab-link">Closures</a>
        <a
          className={"tab-link" + (activeTab === "sharing" ? " tab-link--active" : "")}
          onClick={() => setActiveTab("sharing")}
        >
          Sharing &amp; client
        </a>
        <a
          className={"tab-link" + (activeTab === "settings" ? " tab-link--active" : "")}
          onClick={() => setActiveTab("settings")}
        >
          Settings
        </a>
      </nav>

      {activeTab === "overview" && (
        <section className="grid grid--split">
          <div className="panel">
            <header className="panel__head"><h2>Upcoming stays</h2></header>
            <table className="table">
              <thead>
                <tr>
                  <th>Guest</th><th>Source</th><th>In</th><th>Out</th><th>Guests</th>
                </tr>
              </thead>
              <tbody>
                {stays.map((s) => (
                  <tr key={s.id}>
                    <td><strong>{s.guest_name}</strong></td>
                    <td>{s.source}</td>
                    <td className="table__mono">{fmtDayMon(s.check_in)}</td>
                    <td className="table__mono">{fmtDayMon(s.check_out)}</td>
                    <td>{s.guest_name}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="panel">
            <header className="panel__head"><h2>Tasks for this property</h2></header>
            <ul className="task-list task-list--desk">
              {property_tasks.map((t) => {
                const emp = empsById.get(t.assignee_id);
                return (
                  <li key={t.id} className="task-row">
                    <span className="task-row__time table__mono">
                      {fmtDayMonTime(t.scheduled_start)}
                    </span>
                    <span className="task-row__title">
                      <strong>{t.title}</strong>
                      <span className="task-row__area">{t.area}</span>
                    </span>
                    <span className="task-row__assignee">
                      {emp && (
                        <>
                          <Avatar url={emp.avatar_url} initials={emp.avatar_initials} size="xs" alt={emp.name} />{" "}
                          {emp.name.split(" ")[0]}
                        </>
                      )}
                    </span>
                    <Chip tone={STATUS_TONE[t.status]} size="sm">{t.status}</Chip>
                  </li>
                );
              })}
            </ul>
          </div>
        </section>
      )}

      {activeTab === "settings" && (
        <>
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
        </>
      )}

      {activeTab === "units" && showUnitsTab && (
        <UnitsPanel propertyId={property.id} units={units} />
      )}

      {activeTab === "assets" && (
        <div className="panel">
          <header className="panel__head">
            <h2>Assets</h2>
            <span className="muted mono">{assets.length} tracked</span>
          </header>
          {assets.length === 0 ? (
            <p className="muted">No assets tracked for this property.</p>
          ) : (
            <table className="table">
              <thead>
                <tr><th>Asset</th><th>Area</th><th>Condition</th><th>Status</th></tr>
              </thead>
              <tbody>
                {assets.map((a) => (
                  <tr key={a.id}>
                    <td><strong>{a.name}</strong>{a.make && <span className="table__sub"> {a.make} {a.model}</span>}</td>
                    <td>{a.area ?? "\u2014"}</td>
                    <td><Chip tone={a.condition === "fair" ? "sand" : (a.condition === "poor" || a.condition === "needs_replacement") ? "rust" : "moss"} size="sm">{a.condition}</Chip></td>
                    <td><Chip tone={a.status === "active" ? "moss" : a.status === "in_repair" ? "sand" : "rust"} size="sm">{a.status}</Chip></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {activeTab === "sharing" && (
        <SharingPanel
          detail={detailQ.data}
          meAvailable={meQ.data?.available_workspaces ?? []}
        />
      )}

      <AgentPreferencesPanel
        scope="property"
        scopeId={property.id}
        title={"Agent preferences — " + property.name}
        subtitle="Sits between workspace and user preferences when the agent discusses this property. Soft guidance only — hard rules belong in the settings cascade above."
      />
    </DeskPage>
  );
}
