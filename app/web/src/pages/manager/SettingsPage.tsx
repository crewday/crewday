import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { fetchApiDownload, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { landingForWorkspace } from "@/auth/roleLanding";
import { workspaceRouteForPathname, workspaceSlugFromRoutePath } from "@/lib/workspaceRoutes";
import DeskPage from "@/components/DeskPage";
import AgentPreferencesPanel from "@/components/AgentPreferencesPanel";
import { Chip, Loading, ProgressBar } from "@/components/common";
import type { AuthMe } from "@/auth/types";
import type {
  AvailableWorkspace,
  Employee,
  Me,
  Property,
  SettingDefinition,
  WorkspaceSettings,
  WorkspaceUsage,
} from "@/types/api";

const NAMESPACE_LABELS: Record<string, string> = {
  evidence: "Evidence",
  bookings: "Bookings",
  time: "Time tracking",
  pay: "Pay",
  retention: "Retention",
  scheduling: "Scheduling",
  tasks: "Tasks",
  inventory: "Inventory",
  expenses: "Expenses",
  chat: "Chat",
  voice: "Voice",
  notifications: "Notifications",
  assets: "Assets",
  auth: "Authentication",
  ical: "iCal",
  webhook: "Webhooks",
};

function groupByNamespace(
  defaults: Record<string, unknown>,
  catalog: SettingDefinition[],
): Record<string, { def: SettingDefinition; value: unknown }[]> {
  const groups: Record<string, { def: SettingDefinition; value: unknown }[]> = {};
  for (const def of catalog) {
    const ns = def.key.split(".")[0] ?? "other";
    const bucket = groups[ns] ?? (groups[ns] = []);
    bucket.push({ def, value: defaults[def.key] ?? def.catalog_default });
  }
  return groups;
}

function draftFromValue(value: unknown): string {
  // code-health: ignore[ccn nloc] Small draft mapper is mis-scored by the TS parser around setting editor JSX.
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value;
  return "";
}

const SCOPE_LABELS: Record<string, string> = {
  W: "workspace",
  P: "property",
  U: "unit",
  WE: "work engagement",
  T: "task",
  E: "employee",
  workspace: "workspace",
};

const ENUM_LABELS: Record<string, Record<string, string>> = {
  "evidence.policy": {
    require: "Required",
    optional: "Optional",
    forbid: "Forbidden",
  },
  "bookings.pay_basis": {
    scheduled: "Scheduled time",
    actual: "Actual worked time",
  },
  "pay.frequency": {
    weekly: "Weekly",
    fortnightly: "Fortnightly",
    monthly: "Monthly",
  },
  "pay.week_start": {
    monday: "Monday",
    sunday: "Sunday",
  },
};

const ARCHIVE_CONFIRMATION = "ARCHIVE";
const DELETE_CONFIRMATION = "DELETE";
const WORKSPACE_EXPORT_PATH = "/api/v1/admin/workspace/export";
const WORKSPACE_ARCHIVE_PATH = "/api/v1/admin/workspace/archive";
const WORKSPACE_DELETE_PATH = "/api/v1/admin/workspace/delete";

interface WorkspaceArchiveResponse {
  id: string;
  archived_at: string;
}

interface WorkspaceDeleteResponse {
  id: string;
  archived_at: string;
  delete_requested_at: string;
  purge_after: string;
}

interface WorkspaceSwitcherEntry {
  workspace_id: string;
  slug: string;
  name: string;
  current_role: string | null;
  last_seen_at: string | null;
  settings_override: Record<string, unknown>;
}

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

function formatDeadline(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function triggerBrowserDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function nextWorkspaceRoute(currentPathname: string, workspaces: AvailableWorkspace[]): string {
  const currentSlug = workspaceSlugFromRoutePath(currentPathname);
  const next = workspaces.find((workspace) => workspace.workspace.id !== currentSlug);
  return next ? landingForWorkspace(next) : "/select-workspace";
}

function workspaceAdminPath(pathname: string, path: string): string {
  const slug = workspaceSlugFromRoutePath(pathname);
  return slug ? `/w/${encodeURIComponent(slug)}${path}` : path;
}

function renameAvailableWorkspaces<T extends { available_workspaces: AvailableWorkspace[] }>(
  cached: T | undefined,
  slug: string,
  name: string,
): T | undefined {
  if (!cached) return cached;
  return {
    ...cached,
    available_workspaces: cached.available_workspaces.map((entry) =>
      entry.workspace.id === slug
        ? { ...entry, workspace: { ...entry.workspace, name } }
        : entry,
    ),
  };
}

function renameSwitcherWorkspaces(
  cached: WorkspaceSwitcherEntry[] | undefined,
  slug: string,
  name: string,
): WorkspaceSwitcherEntry[] | undefined {
  if (!cached) return cached;
  return cached.map((entry) => (entry.slug === slug ? { ...entry, name } : entry));
}

function renameWorkspaceInCaches(qc: QueryClient, settings: WorkspaceSettings): void {
  const { slug, display_name: displayName } = settings.meta;
  qc.setQueryData(
    qk.authMe(),
    renameAvailableWorkspaces(qc.getQueryData<AuthMe>(qk.authMe()), slug, displayName),
  );
  qc.setQueryData(qk.me(), renameAvailableWorkspaces(qc.getQueryData<Me>(qk.me()), slug, displayName));
  qc.setQueryData(
    qk.meWorkspaces(),
    renameSwitcherWorkspaces(qc.getQueryData<WorkspaceSwitcherEntry[]>(qk.meWorkspaces()), slug, displayName),
  );
}

async function routeAfterWorkspaceArchived(pathname: string): Promise<string> {
  const workspaces = await fetchJson<AvailableWorkspace[]>("/api/v1/me/workspaces");
  return nextWorkspaceRoute(pathname, workspaces);
}

function enumOptionLabel(def: SettingDefinition, option: string): string {
  const label = ENUM_LABELS[def.key]?.[option];
  if (label) return label;
  return option
    .replaceAll("_", " ")
    .split(" ")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function scopeLabel(scope: string): string {
  return scope
    .split("/")
    .map((part) => SCOPE_LABELS[part] ?? part)
    .join(", ");
}

function parseDraft(def: SettingDefinition, draft: string): unknown {
  if (def.type === "bool") return draft === "true";
  if (def.type === "int") return Number(draft);
  return draft;
}

interface SettingPaneItem {
  def: SettingDefinition;
  value: unknown;
}

function settingHelpId(key: string): string {
  return `setting-help-${key.replaceAll(".", "-")}`;
}

function draftsFromItems(items: SettingPaneItem[]): Record<string, string> {
  return Object.fromEntries(items.map(({ def, value }) => [def.key, draftFromValue(value)]));
}

function catalogDraftsFromItems(items: SettingPaneItem[]): Record<string, string> {
  return Object.fromEntries(items.map(({ def }) => [def.key, draftFromValue(def.catalog_default)]));
}

function dirtyPayload(
  items: SettingPaneItem[],
  drafts: Record<string, string>,
  resetKeys: ReadonlySet<string>,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const { def, value } of items) {
    const draft = drafts[def.key] ?? "";
    const current = draftFromValue(value);
    if (resetKeys.has(def.key) && draft === draftFromValue(def.catalog_default)) {
      if (draft !== current) payload[def.key] = null;
      continue;
    }
    if (draft !== current) payload[def.key] = parseDraft(def, draft);
  }
  return payload;
}

function invalidDirtySetting(items: SettingPaneItem[], drafts: Record<string, string>): boolean {
  return items.some(({ def, value }) => {
    const draft = drafts[def.key] ?? "";
    if (draft === draftFromValue(value)) return false;
    return def.type === "int" && (!Number.isInteger(Number(draft)) || draft.trim() === "");
  });
}

function SettingEditorRow({
  def,
  draft,
  disabled,
  onDraftChange,
}: {
  def: SettingDefinition;
  draft: string;
  disabled: boolean;
  onDraftChange: (draft: string) => void;
}) {
  const helpId = settingHelpId(def.key);

  return (
    <div className="settings-editor form-layout__row">
      <dt className="form-layout__label">
        <span className="settings-editor__label">{def.label}</span>
      </dt>
      <dd className="form-layout__control">
        <div className="settings-editor__control">
          {def.type === "bool" ? (
            <select
              className="settings-editor__input"
              aria-label={def.label}
              aria-describedby={helpId}
              value={draft}
              onChange={(event) => onDraftChange(event.target.value)}
              disabled={disabled}
            >
              <option value="true">Yes</option>
              <option value="false">No</option>
            </select>
          ) : def.type === "enum" ? (
            <select
              className="settings-editor__input"
              aria-label={def.label}
              aria-describedby={helpId}
              value={draft}
              onChange={(event) => onDraftChange(event.target.value)}
              disabled={disabled}
            >
              {(def.enum_values ?? []).map((option) => (
                <option key={option} value={option}>{enumOptionLabel(def, option)}</option>
              ))}
            </select>
          ) : (
            <input
              className="settings-editor__input"
              aria-label={def.label}
              aria-describedby={helpId}
              type="number"
              value={draft}
              onChange={(event) => onDraftChange(event.target.value)}
              disabled={disabled}
            />
          )}
        </div>
      </dd>
      <dd id={helpId} className="settings-editor__help form-layout__help">
        <span>{def.description}</span>
        {" "}
        <span className="settings-editor__scope">
          Can be overridden at: {scopeLabel(def.override_scope)}
        </span>
      </dd>
    </div>
  );
}

function SettingsPane({
  namespace,
  items,
}: {
  namespace: string;
  items: SettingPaneItem[];
}) {
  const qc = useQueryClient();
  const [drafts, setDrafts] = useState<Record<string, string>>(() => draftsFromItems(items));
  const [resetKeys, setResetKeys] = useState<Set<string>>(() => new Set());
  const [error, setError] = useState<string | null>(null);
  const valuesKey = JSON.stringify(items.map(({ def, value }) => [def.key, value]));
  const syncedValuesKey = useRef(valuesKey);

  useEffect(() => {
    if (syncedValuesKey.current === valuesKey) return;
    syncedValuesKey.current = valuesKey;
    setDrafts(draftsFromItems(items));
    setResetKeys(new Set());
  }, [items, valuesKey]);

  const save = useMutation({
    mutationFn: (payload: Record<string, unknown>) =>
      fetchJson<WorkspaceSettings>("/api/v1/settings", {
        method: "PATCH",
        body: payload,
      }),
    onSuccess: (next) => {
      qc.setQueryData(qk.settings(), next);
      setError(null);
    },
    onError: () => setError("Save failed."),
  });

  const payload = dirtyPayload(items, drafts, resetKeys);
  const dirty = Object.keys(payload).length > 0;
  const invalid = invalidDirtySetting(items, drafts);
  const canUseDefaults = items.some(({ def, value }) => {
    const catalogDefault = draftFromValue(def.catalog_default);
    return (drafts[def.key] ?? "") !== catalogDefault || draftFromValue(value) !== catalogDefault;
  });

  return (
    <div className="panel">
      <header className="panel__head">
        <h2>{NAMESPACE_LABELS[namespace] ?? namespace}</h2>
      </header>
      <form
        className="settings-pane"
        onSubmit={(event) => {
          event.preventDefault();
          if (dirty && !invalid) save.mutate(payload);
        }}
      >
        <dl className="settings-kv settings-kv--editable form-layout form-layout--two-column">
          {items.map(({ def }) => (
            <SettingEditorRow
              key={def.key}
              def={def}
              draft={drafts[def.key] ?? ""}
              disabled={save.isPending}
              onDraftChange={(draft) => {
                setDrafts((current) => ({ ...current, [def.key]: draft }));
                setResetKeys((current) => {
                  if (!current.has(def.key)) return current;
                  const next = new Set(current);
                  next.delete(def.key);
                  return next;
                });
              }}
            />
          ))}
        </dl>
        {dirty || canUseDefaults ? (
          <div className="settings-pane__actions form-layout__actions">
            {dirty ? (
              <button
                className="btn btn--moss btn--sm"
                type="submit"
                disabled={invalid || save.isPending}
              >
                {save.isPending ? "Saving…" : "Save"}
              </button>
            ) : null}
            {canUseDefaults ? (
              <button
                className="btn btn--ghost btn--sm"
                type="button"
                disabled={save.isPending}
                onClick={() => {
                  setDrafts(catalogDraftsFromItems(items));
                  setResetKeys(new Set(items.map(({ def }) => def.key)));
                }}
              >
                Use defaults
              </button>
            ) : null}
          </div>
        ) : null}
        {error ? <p className="settings-editor__error" role="alert">{error}</p> : null}
      </form>
    </div>
  );
}

function WorkspaceDetailsForm({ settings }: { settings: WorkspaceSettings }) {
  const qc = useQueryClient();
  const displayName = settings.meta.display_name;
  const [draft, setDraft] = useState(displayName);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDraft(displayName);
  }, [displayName]);

  const save = useMutation({
    mutationFn: () =>
      fetchJson<WorkspaceSettings>("/api/v1/settings/basics", {
        method: "PATCH",
        body: { display_name: draft },
      }),
    onSuccess: (next) => {
      qc.setQueryData(qk.settings(), next);
      renameWorkspaceInCaches(qc, next);
      setError(null);
    },
    onError: (err) => {
      setError(errorMessage(err, "Workspace details could not be saved."));
    },
  });

  const dirty = draft !== displayName;
  const invalid = draft.trim() === "";
  const displayNameId = "workspace-display-name";

  return (
    <form
      className="workspace-details-form form-layout form-layout--two-column"
      onSubmit={(event) => {
        event.preventDefault();
        if (!dirty || invalid || save.isPending) return;
        save.mutate();
      }}
    >
      <div className="settings-editor form-layout__row">
        <label className="form-layout__label settings-editor__label" htmlFor={displayNameId}>
          Display name
        </label>
        <div className="form-layout__control">
          <input
            id={displayNameId}
            className="settings-editor__input"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            disabled={save.isPending}
          />
        </div>
        <p className="settings-editor__help form-layout__help">
          Shown in workspace lists, page titles, and notifications.
        </p>
      </div>
      <div className="settings-editor form-layout__row">
        <span className="form-layout__label settings-editor__label">Slug</span>
        <span className="form-layout__control">
          <span className="workspace-details-form__slug mono">{settings.meta.slug}</span>
        </span>
        <p className="settings-editor__help form-layout__help">
          Read-only URL slug for routes under <span className="mono">/w/{settings.meta.slug}</span>.
        </p>
      </div>
      <div className="settings-editor form-layout__row">
        <span className="form-layout__label settings-editor__label">Workspace defaults</span>
        <span className="form-layout__control workspace-details-form__defaults">
          <span className="mono">{settings.meta.timezone}</span>
          <span className="mono">{settings.meta.currency}</span>
          <span className="mono">{settings.meta.country}</span>
          <span className="mono">{settings.meta.default_locale}</span>
        </span>
        <p className="settings-editor__help form-layout__help">
          Timezone, currency, country, and locale remain managed by the workspace defaults flow.
        </p>
      </div>
      {dirty ? (
        <div className="workspace-details-form__actions form-layout__actions">
          <button className="btn btn--moss" type="submit" disabled={invalid || save.isPending}>
            {save.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      ) : null}
      {error ? <p className="settings-editor__error" role="alert">{error}</p> : null}
    </form>
  );
}

function OverrideSummary({ properties, employees }: { properties: Property[]; employees: Employee[] }) {
  const { pathname } = useLocation();
  const propsWithOverrides = properties.filter((p) => Object.keys(p.settings_override).length > 0);
  const empsWithOverrides = employees.filter((e) => Object.keys(e.settings_override).length > 0);

  if (propsWithOverrides.length === 0 && empsWithOverrides.length === 0) {
    return (
      <div className="panel">
        <header className="panel__head"><h2>Override summary</h2></header>
        <p className="muted">No properties or employees have settings overrides.</p>
      </div>
    );
  }

  return (
    <div className="panel">
      <header className="panel__head"><h2>Override summary</h2></header>
      <p className="muted">Properties and employees that override workspace defaults.</p>
      {propsWithOverrides.length > 0 && (
        <>
          <h3 className="section-title section-title--sm">Properties</h3>
          <ul className="settings-list">
            {propsWithOverrides.map((p) => (
              <li key={p.id}>
                <Link to={workspaceRouteForPathname(pathname, "/property/" + p.id)} className="link">
                  <strong>{p.name}</strong>
                </Link>{" "}
                <Chip tone={p.color} size="sm">
                  {Object.keys(p.settings_override).length} override{Object.keys(p.settings_override).length !== 1 ? "s" : ""}
                </Chip>
                <span className="muted"> — {Object.keys(p.settings_override).join(", ")}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {empsWithOverrides.length > 0 && (
        <>
          <h3 className="section-title section-title--sm">Employees</h3>
          <ul className="settings-list">
            {empsWithOverrides.map((e) => (
              <li key={e.id}>
                <Link to={workspaceRouteForPathname(pathname, "/employee/" + e.id)} className="link">
                  <strong>{e.name}</strong>
                </Link>{" "}
                <Chip tone="sky" size="sm">
                  {Object.keys(e.settings_override).length} override{Object.keys(e.settings_override).length !== 1 ? "s" : ""}
                </Chip>
                <span className="muted"> — {Object.keys(e.settings_override).join(", ")}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function WorkspaceLifecycleDialog({
  action,
  expected,
  title,
  description,
  confirmLabel,
  pending,
  error,
  onCancel,
  onConfirm,
}: {
  action: "archive" | "delete";
  expected: string;
  title: string;
  description: string;
  confirmLabel: string;
  pending: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const ref = useRef<HTMLDialogElement | null>(null);
  const [draft, setDraft] = useState("");

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) dialog.showModal();
    } else if (!dialog.open) {
      dialog.setAttribute("open", "");
    }
    return () => {
      if (dialog.open && typeof dialog.close === "function") dialog.close();
      dialog.removeAttribute("open");
    };
  }, []);

  const valid = draft.trim() === expected;
  const inputId = `workspace-${action}-confirmation`;

  return (
    <dialog
      ref={ref}
      className="modal workspace-danger-dialog"
      aria-labelledby={`workspace-${action}-title`}
      onCancel={(event) => {
        event.preventDefault();
        if (!pending) onCancel();
      }}
      onClose={() => {
        if (!pending) onCancel();
      }}
    >
      <form
        className="modal__body workspace-danger-dialog__body"
        onSubmit={(event) => {
          event.preventDefault();
          if (valid && !pending) onConfirm();
        }}
      >
        <h3 id={`workspace-${action}-title`} className="modal__title">{title}</h3>
        <p className="modal__sub">{description}</p>
        <label className="field">
          <span>Type {expected} to confirm</span>
          <input
            id={inputId}
            className="workspace-danger-dialog__input"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            disabled={pending}
            autoFocus
          />
        </label>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <div className="modal__actions">
          <button className="btn btn--ghost" type="button" disabled={pending} onClick={onCancel}>
            Cancel
          </button>
          <button className="btn btn--rust" type="submit" disabled={!valid || pending}>
            {pending ? "Working…" : confirmLabel}
          </button>
        </div>
      </form>
    </dialog>
  );
}

export default function SettingsPage() {
  // code-health: ignore[nloc] Manager settings route keeps catalog grouping, drafts, and shared setting editor together.
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [confirmation, setConfirmation] = useState<"archive" | "delete" | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleteResult, setDeleteResult] = useState<WorkspaceDeleteResponse | null>(null);
  const settingsQ = useQuery({
    queryKey: qk.settings(),
    queryFn: () => fetchJson<WorkspaceSettings>("/api/v1/settings"),
  });
  const catalogQ = useQuery({
    queryKey: qk.settingsCatalog(),
    queryFn: () => fetchJson<SettingDefinition[]>("/api/v1/settings/catalog"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const empsQ = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });
  const usageQ = useQuery({
    queryKey: qk.workspaceUsage(),
    queryFn: () => fetchJson<WorkspaceUsage>("/api/v1/workspace/usage"),
  });
  const invalidateWorkspaceLifecycle = (): void => {
    void qc.invalidateQueries({ queryKey: qk.authMe() });
    void qc.invalidateQueries({ queryKey: qk.meWorkspaces() });
    void qc.invalidateQueries({ queryKey: qk.me(), refetchType: "none" });
    void qc.invalidateQueries({ queryKey: qk.settings(), refetchType: "none" });
  };
  const exportWorkspace = useMutation({
    mutationFn: () =>
      fetchApiDownload(workspaceAdminPath(pathname, WORKSPACE_EXPORT_PATH), { method: "POST" }),
    onMutate: () => {
      setExportError(null);
    },
    onSuccess: (download) => {
      triggerBrowserDownload(download.blob, download.filename ?? "crewday-workspace-export.zip");
    },
    onError: (err) => {
      setExportError(errorMessage(err, "Workspace export failed."));
    },
  });
  const archiveWorkspace = useMutation({
    mutationFn: () =>
      fetchJson<WorkspaceArchiveResponse>(workspaceAdminPath(pathname, WORKSPACE_ARCHIVE_PATH), {
        method: "POST",
      }),
    onMutate: () => {
      setArchiveError(null);
    },
    onSuccess: async () => {
      setConfirmation(null);
      invalidateWorkspaceLifecycle();
      const route = await routeAfterWorkspaceArchived(pathname).catch(() => "/select-workspace");
      navigate(route, { replace: true });
    },
    onError: (err) => {
      setArchiveError(errorMessage(err, "Workspace could not be archived."));
    },
  });
  const deleteWorkspace = useMutation({
    mutationFn: () =>
      fetchJson<WorkspaceDeleteResponse>(workspaceAdminPath(pathname, WORKSPACE_DELETE_PATH), {
        method: "POST",
      }),
    onMutate: () => {
      setDeleteError(null);
      setDeleteResult(null);
    },
    onSuccess: async (result) => {
      setDeleteResult(result);
      setConfirmation(null);
      invalidateWorkspaceLifecycle();
    },
    onError: (err) => {
      setDeleteError(errorMessage(err, "Workspace deletion could not be scheduled."));
    },
  });
  const sub = (
    <>
      Workspace-wide configuration only. Personal profile, approval mode, and private agent
      preferences live under <Link to={workspaceRouteForPathname(pathname, "/me")} className="link">My profile</Link>.
    </>
  );

  if (
    settingsQ.isPending ||
    catalogQ.isPending ||
    propsQ.isPending ||
    empsQ.isPending ||
    usageQ.isPending
  ) {
    return <DeskPage title="Workspace settings" sub={sub}><Loading /></DeskPage>;
  }
  if (!settingsQ.data || !catalogQ.data || !propsQ.data || !empsQ.data || !usageQ.data) {
    return <DeskPage title="Workspace settings" sub={sub}>Failed to load.</DeskPage>;
  }

  const ws = settingsQ.data;
  const catalog = catalogQ.data;
  const grouped = groupByNamespace(ws.defaults, catalog);

  return (
    <DeskPage title="Workspace settings" sub={sub}>
      {/* §11 — Agent preferences (workspace layer). Soft guidance stacked
          into every composition-capability system prompt. */}
      <AgentPreferencesPanel
        scope="workspace"
        title="Agent preferences — Workspace"
        subtitle="Stacked broadest-first with property and user preferences into every agent turn. CLAUDE.md-style free-form guidance; not a substitute for the structured settings cascade below."
      />

      {/* Workspace identity */}
      <section className="panel">
        <header className="panel__head"><h2>Workspace</h2></header>
        <WorkspaceDetailsForm settings={ws} />
      </section>

      {/* §11 — Workspace usage budget. Manager-visible shape is
          percent-only by design: no dollars, no tokens, no reset date.
          Dollars live on /settings/llm for the operator audience. The
          cap itself is adjusted via `crewday admin budget set-cap`; there
          is no HTTP surface to raise it. */}
      <section className="panel agent-usage">
        <header className="panel__head">
          <div className="agent-usage__heading">
            <h2>Agent usage</h2>
            <span className="muted agent-usage__window">{usageQ.data.window_label}</span>
          </div>
        </header>
        <div className="agent-usage__row">
          <div className="agent-usage__value">
            {usageQ.data.paused ? (
              <Chip tone="rust" size="sm">Paused</Chip>
            ) : (
              <span className="agent-usage__pct">{usageQ.data.percent}%</span>
            )}
          </div>
          <div className="agent-usage__bar">
            <ProgressBar value={usageQ.data.percent} />
          </div>
        </div>
        {usageQ.data.paused ? (
          <p className="muted">
            Agents are paused until older activity ages out of the window.
          </p>
        ) : null}
      </section>

      {/* Workspace defaults grouped by namespace */}
      <section className="grid grid--split">
        {Object.entries(grouped).map(([ns, items]) => (
          <SettingsPane key={ns} namespace={ns} items={items} />
        ))}
      </section>

      <section className="panel">
        <header className="panel__head">
          <h2>Chat gateway</h2>
          <Chip tone="ghost" size="sm">using deployment default</Chip>
        </header>
        <p className="muted">
          WhatsApp runs on the deployment-default Meta account — every workspace on this
          deployment shares one phone number. That's what your workers link when they pair their
          phone on <Link to={workspaceRouteForPathname(pathname, "/me")} className="link">/me → Chat channels</Link>.
        </p>
        <p className="muted">
          No per-user preferences live here: a linked WhatsApp means agent reach-out is on for
          that worker, unlinked means off. Whatever a worker could do via the CLI, the chat agent
          can do on their behalf — never more.
        </p>
        <dl className="settings-kv">
          <dt>Provider</dt>
          <dd>Deployment-default WhatsApp (<span className="mono">offapp_whatsapp</span>)</dd>
          <dt>Workspace override</dt>
          <dd className="muted">None. Opt in below to bring your own Meta account.</dd>
        </dl>
        <div className="chat-gateway-panel__footer">
          <button type="button" className="btn btn--ghost btn--sm" disabled>
            Use a dedicated Meta account for this workspace
          </button>
          <p className="muted chat-gateway-panel__hint">
            Overriding the default makes this workspace Meta-verify and own its own WhatsApp
            Business number — useful for branded communication or stricter isolation.
          </p>
        </div>
      </section>

      {/* Override summary */}
      <OverrideSummary properties={propsQ.data} employees={empsQ.data} />

      {/* Policy + Danger zone */}
      <div className="panel">
        <header className="panel__head"><h2>Agent approvals</h2></header>
        <p className="muted">Actions that require your manual approval before an agent can execute them.</p>

        <h3 className="section-title section-title--sm">Always gated (cannot be disabled)</h3>
        <ul className="settings-list">
          {ws.policy.approvals.always_gated.map((a) => (
            <li key={a}><code className="inline-code">{a}</code></li>
          ))}
        </ul>

        <h3 className="section-title section-title--sm">Configurable</h3>
        <ul className="settings-list">
          {ws.policy.approvals.configurable.map((a) => (
            <li key={a}>
              <code className="inline-code">{a}</code>{" "}
              <Chip tone="moss" size="sm">gated</Chip>
            </li>
          ))}
        </ul>
      </div>

      <div className="panel panel--danger">
        <header className="panel__head"><h2>Danger zone</h2></header>
        <p className="muted">
          Owner-only workspace lifecycle actions. Archive and Delete remove this workspace from
          normal selection immediately.
        </p>
        {deleteResult ? (
          <p className="workspace-danger-actions__notice" role="status">
            Deletion scheduled. This workspace is archived now and will be purged after{" "}
            <time dateTime={deleteResult.purge_after}>{formatDeadline(deleteResult.purge_after)}</time>.
          </p>
        ) : null}
        <ul className="workspace-danger-actions">
          <li className="workspace-danger-action">
            <div className="workspace-danger-action__copy">
              <strong>Export</strong>
              <span>Download a portable ZIP of this workspace's rows and uploads.</span>
            </div>
            <button
              className="btn btn--ghost"
              type="button"
              disabled={exportWorkspace.isPending}
              onClick={() => exportWorkspace.mutate()}
            >
              {exportWorkspace.isPending ? "Exporting…" : "Export"}
            </button>
          </li>
          <li className="workspace-danger-action">
            <div className="workspace-danger-action__copy">
              <strong>Archive</strong>
              <span>Make the workspace inactive while keeping its data for a future reactivation flow.</span>
            </div>
            <button
              className="btn btn--rust"
              type="button"
              disabled={archiveWorkspace.isPending || deleteWorkspace.isPending}
              onClick={() => {
                setArchiveError(null);
                setConfirmation("archive");
              }}
            >
              Archive
            </button>
          </li>
          <li className="workspace-danger-action">
            <div className="workspace-danger-action__copy">
              <strong>Delete</strong>
              <span>Archive now and schedule irreversible purge after the 14-day grace period.</span>
            </div>
            <button
              className="btn btn--rust"
              type="button"
              disabled={archiveWorkspace.isPending || deleteWorkspace.isPending}
              onClick={() => {
                setDeleteError(null);
                setConfirmation("delete");
              }}
            >
              Delete
            </button>
          </li>
        </ul>
        {exportError ? <p className="form-error" role="alert">{exportError}</p> : null}
        {deleteResult ? (
          <button
            className="btn btn--moss workspace-danger-actions__continue"
            type="button"
            onClick={async () => {
              const route = await routeAfterWorkspaceArchived(pathname).catch(() => "/select-workspace");
              navigate(route, { replace: true });
            }}
          >
            Continue
          </button>
        ) : null}
      </div>

      {confirmation === "archive" ? (
        <WorkspaceLifecycleDialog
          action="archive"
          expected={ARCHIVE_CONFIRMATION}
          title="Archive workspace"
          description="The workspace becomes inactive immediately and disappears from normal workspace selection."
          confirmLabel="Archive workspace"
          pending={archiveWorkspace.isPending}
          error={archiveError}
          onCancel={() => setConfirmation(null)}
          onConfirm={() => archiveWorkspace.mutate()}
        />
      ) : null}
      {confirmation === "delete" ? (
        <WorkspaceLifecycleDialog
          action="delete"
          expected={DELETE_CONFIRMATION}
          title="Delete workspace"
          description="The workspace is archived immediately. Deletion is scheduled for purge after the 14-day grace period."
          confirmLabel="Schedule deletion"
          pending={deleteWorkspace.isPending}
          error={deleteError}
          onCancel={() => setConfirmation(null)}
          onConfirm={() => deleteWorkspace.mutate()}
        />
      ) : null}
    </DeskPage>
  );
}
