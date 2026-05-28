import { useEffect, useRef, useState } from "react";
import { Check, Pencil, X } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import DateTime from "@/components/DateTime";
import { Chip, Loading } from "@/components/common";
import type { AgentDoc, AgentDocSummary } from "@/types/api";

type AgentDocRole = "manager" | "employee" | "admin";

interface AgentDocListItem extends AgentDocSummary {
  capabilities?: string[];
  version?: number;
  is_customised?: boolean;
  default_hash?: string;
  approx_token_count?: number;
}

interface AgentDocDetailItem extends AgentDoc {
  notes?: string | null;
  approx_token_count?: number;
}

interface AgentDocDraft {
  slug: string;
  title: string;
  summary: string;
  roles: string[];
  capabilities: string[];
  body_md: string;
  notes: string;
  updated_at: string;
  version: number;
  is_customised: boolean;
  default_hash?: string;
  approx_token_count?: number;
}

interface AgentDocRow {
  id: string;
  draft: AgentDocDraft;
  committedDraft: AgentDocDraft;
  editing: boolean;
  dirty: boolean;
  saving: boolean;
  validation?: string;
  error?: string;
}

const ROLE_OPTIONS: readonly { value: AgentDocRole; label: string }[] = [
  { value: "manager", label: "Manager" },
  { value: "employee", label: "Employee" },
  { value: "admin", label: "Admin" },
];
const ROLE_ORDER: AgentDocRole[] = ["manager", "employee", "admin"];
const SAFETY_WARNING =
  "Body is sent to every chat agent that loads this doc. Do not paste workspace secrets, customer data, or live API keys.";

export default function AdminAgentDocsPage() {
  const queryClient = useQueryClient();
  const [activeSlug, setActiveSlug] = useState<string | null>(null);
  const [rows, setRows] = useState<AgentDocRow[]>([]);
  const rowsRef = useRef<AgentDocRow[]>([]);

  const listQ = useQuery({
    queryKey: qk.adminAgentDocs(),
    queryFn: () => fetchJson<AgentDocListItem[]>("/admin/api/v1/agent_docs"),
  });

  const docQ = useQuery({
    queryKey: qk.adminAgentDoc(activeSlug ?? ""),
    queryFn: () => fetchJson<AgentDocDetailItem>(`/admin/api/v1/agent_docs/${activeSlug}`),
    enabled: activeSlug != null,
  });

  useEffect(() => {
    rowsRef.current = rows;
  }, [rows]);

  useEffect(() => {
    if (!listQ.data) return;
    setRows((current) => listQ.data.map((summary) => {
      const existing = current.find((row) => row.id === summary.slug);
      if (!existing) return rowFromDraft(draftFromSummary(summary));
      const merged = mergeSummary(existing.draft, summary, { preserveEditable: existing.dirty });
      const committedDraft = existing.dirty
        ? mergeSummary(existing.committedDraft, summary, { preserveEditable: true })
        : mergeSummary(existing.committedDraft, summary);
      return {
        ...existing,
        draft: merged,
        committedDraft,
      };
    }));
  }, [listQ.data]);

  useEffect(() => {
    if (!listQ.data) return;
    let cancelled = false;
    void Promise.allSettled(
      listQ.data.map((summary) => queryClient.fetchQuery({
        queryKey: qk.adminAgentDoc(summary.slug),
        queryFn: () => fetchJson<AgentDocDetailItem>(`/admin/api/v1/agent_docs/${summary.slug}`),
      })),
    ).then((results) => {
      if (cancelled) return;
      mergeFetchedDocs(results.flatMap((result) => (result.status === "fulfilled" ? [result.value] : [])));
    }).catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [listQ.data, queryClient]);

  useEffect(() => {
    if (!docQ.data || activeSlug == null) return;
    const draft = draftFromDoc(docQ.data);
    setRows((current) => current.map((row) => {
      if (row.id !== activeSlug) return row;
      if (!row.dirty && shouldKeepLocalDraft(row.committedDraft, draft)) return row;
      const nextDraft = row.dirty ? mergeDetailIntoDirtyDraft(row.draft, draft) : draft;
      return {
        ...row,
        draft: nextDraft,
        committedDraft: row.dirty && row.committedDraft.body_md ? row.committedDraft : draft,
        error: undefined,
      };
    }));
  }, [activeSlug, docQ.data]);

  useEffect(() => {
    if (!docQ.isError || activeSlug == null) return;
    patchRow(activeSlug, (row) => ({ ...row, error: "Could not load agent doc body." }));
  }, [activeSlug, docQ.isError]);

  const sub =
    "System-side virtual files the chat agents read on demand (section 11 Agent knowledge tools).";

  function patchRow(rowId: string, update: (row: AgentDocRow) => AgentDocRow): void {
    setRows((current) => {
      const next = current.map((row) => (row.id === rowId ? update(row) : row));
      rowsRef.current = next;
      return next;
    });
  }

  function editRow(rowId: string): void {
    const cached = queryClient.getQueryData<AgentDocDetailItem>(qk.adminAgentDoc(rowId));
    const cachedDraft = cached ? draftFromDoc(cached) : null;
    setActiveSlug(rowId);
    patchRow(rowId, (row) => {
      const draft = cachedDraft && !shouldKeepLocalDraft(row.committedDraft, cachedDraft)
        ? cachedDraft
        : row.committedDraft;
      return {
        ...row,
        draft,
        committedDraft: draft,
        editing: true,
        dirty: false,
        validation: undefined,
        error: undefined,
      };
    });
  }

  function updateDraft(rowId: string, patch: Partial<AgentDocDraft>): void {
    patchRow(rowId, (row) => ({
      ...row,
      draft: { ...row.draft, ...patch },
      dirty: true,
      validation: undefined,
      error: undefined,
    }));
  }

  function cancelRow(rowId: string): void {
    patchRow(rowId, (row) => ({
      ...row,
      draft: row.committedDraft,
      editing: false,
      dirty: false,
      saving: false,
      validation: undefined,
      error: undefined,
    }));
  }

  function saveRow(rowId: string): void {
    const row = rowsRef.current.find((candidate) => candidate.id === rowId);
    if (!row) return;
    const validation = validateDraft(row.draft);
    if (validation) {
      patchRow(rowId, (current) => ({ ...current, validation, error: undefined }));
      return;
    }

    const savedDraft: AgentDocDraft = {
      ...row.draft,
      roles: normalizeRoles(row.draft.roles),
      notes: row.draft.notes.trim(),
      version: row.draft.version + (row.dirty ? 1 : 0),
      is_customised: true,
      updated_at: new Date().toISOString(),
      approx_token_count: approxTokenCount(row.draft.body_md),
    };

    queryClient.setQueryData<AgentDocDetailItem>(qk.adminAgentDoc(rowId), {
      ...savedDraft,
      body_md: savedDraft.body_md,
      capabilities: savedDraft.capabilities,
      default_hash: savedDraft.default_hash ?? "",
    });
    queryClient.setQueryData<AgentDocListItem[]>(qk.adminAgentDocs(), (current) => (
      current ? current.map((item) => (item.slug === rowId ? summaryFromDraft(savedDraft) : item)) : current
    ));
    patchRow(rowId, (current) => ({
      ...current,
      draft: savedDraft,
      committedDraft: savedDraft,
      dirty: false,
      saving: false,
      validation: undefined,
      error: undefined,
    }));
  }

  function mergeFetchedDocs(docs: AgentDocDetailItem[]): void {
    const docsBySlug = new Map(docs.map((doc) => [doc.slug, draftFromDoc(doc)]));
    setRows((current) => current.map((row) => {
      const fetched = docsBySlug.get(row.id);
      if (!fetched) return row;
      if (!row.dirty && shouldKeepLocalDraft(row.committedDraft, fetched)) return row;
      if (row.dirty) {
        return {
          ...row,
          draft: mergeDetailIntoDirtyDraft(row.draft, fetched),
          committedDraft: row.committedDraft.body_md ? row.committedDraft : fetched,
        };
      }
      return {
        ...row,
        draft: row.editing ? fetched : { ...row.draft, ...fetched },
        committedDraft: fetched,
      };
    }));
  }

  if (listQ.isPending) {
    return <DeskPage title="Agent docs" sub={sub}><Loading /></DeskPage>;
  }
  if (listQ.isError || !listQ.data) {
    return <DeskPage title="Agent docs" sub={sub}>Failed to load.</DeskPage>;
  }

  return (
    <DeskPage title="Agent docs" sub={sub}>
      <section className="panel agent-docs">
        <div className="inline-table-form inline-table-form--explicit inline-table-form--compact agent-docs__table">
          <div className="inline-table-form__table" role="table" aria-label="Agent docs editor">
            <div className="inline-table-form__head" role="rowgroup">
              <div className="inline-table-form__row inline-table-form__row--head" role="row">
                <div className="inline-table-form__th" role="columnheader">Slug</div>
                <div className="inline-table-form__th" role="columnheader">Document</div>
                <div className="inline-table-form__th" role="columnheader">Roles</div>
                <div className="inline-table-form__th" role="columnheader">Revision</div>
                <div className="inline-table-form__th inline-table-form__th--actions" role="columnheader">State</div>
              </div>
            </div>
            <div className="inline-table-form__body" role="rowgroup">
              {rows.map((row) => (
                <AgentDocEditorRow
                  key={row.id}
                  row={row}
                  detailLoading={activeSlug === row.id && docQ.isPending && !row.draft.body_md}
                  onEdit={() => editRow(row.id)}
                  onCancel={() => cancelRow(row.id)}
                  onSave={() => saveRow(row.id)}
                  onChange={(patch) => updateDraft(row.id, patch)}
                />
              ))}
            </div>
          </div>
        </div>
      </section>
    </DeskPage>
  );
}

function AgentDocEditorRow({
  row,
  detailLoading,
  onEdit,
  onCancel,
  onSave,
  onChange,
}: {
  row: AgentDocRow;
  detailLoading: boolean;
  onEdit: () => void;
  onCancel: () => void;
  onSave: () => void;
  onChange: (patch: Partial<AgentDocDraft>) => void;
}) {
  const editorDisabled = row.saving || detailLoading;
  return (
    <div
      className={[
        "inline-table-form__group",
        row.editing ? "is-editing" : "is-reading",
        row.dirty ? "is-dirty" : null,
        row.error ? "has-error" : null,
        row.validation ? "has-validation" : null,
      ].filter(Boolean).join(" ")}
      aria-label={row.draft.title}
    >
      <div className="inline-table-form__row" role="row">
        <div className="inline-table-form__td" role="cell" data-label="Slug">
          <span className="inline-table-form__mobile-label">Slug</span>
          <code className="agent-docs__slug">{row.draft.slug}</code>
        </div>
        <div className="inline-table-form__td" role="cell" data-label="Document">
          <span className="inline-table-form__mobile-label">Document</span>
          <AgentDocTitle draft={row.draft} />
        </div>
        <div className="inline-table-form__td" role="cell" data-label="Roles">
          <span className="inline-table-form__mobile-label">Roles</span>
          {row.editing ? (
            <RolePicker roles={row.draft.roles} disabled={editorDisabled} onChange={(roles) => onChange({ roles })} />
          ) : (
            <RoleChips roles={row.draft.roles} />
          )}
        </div>
        <div className="inline-table-form__td" role="cell" data-label="Revision">
          <span className="inline-table-form__mobile-label">Revision</span>
          <RevisionState draft={row.draft} />
        </div>
        <div className="inline-table-form__td inline-table-form__td--actions" role="cell" data-label="State">
          <span className="inline-table-form__mobile-label">State</span>
          <RowActions
            row={row}
            cancelDisabled={row.saving}
            saveDisabled={row.saving || detailLoading}
            onEdit={onEdit}
            onCancel={onCancel}
            onSave={onSave}
          />
        </div>
      </div>
      {row.editing ? (
        <div className="inline-table-form__detail" role="row">
          <div className="inline-table-form__detail-body" role="cell">
            {row.validation ? (
              <p className="inline-table-form__message inline-table-form__message--validation">{row.validation}</p>
            ) : null}
            {row.error ? (
              <p className="inline-table-form__message inline-table-form__message--error">{row.error}</p>
            ) : null}
            <AgentDocDetail
              row={row}
              detailLoading={detailLoading}
              disabled={editorDisabled}
              onBodyChange={(body_md) => onChange({ body_md, approx_token_count: approxTokenCount(body_md) })}
              onNotesChange={(notes) => onChange({ notes })}
            />
          </div>
        </div>
      ) : null}
    </div>
  );
}

function RowActions({
  row,
  cancelDisabled,
  saveDisabled,
  onEdit,
  onCancel,
  onSave,
}: {
  row: AgentDocRow;
  cancelDisabled: boolean;
  saveDisabled: boolean;
  onEdit: () => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  if (!row.editing) {
    return (
      <div className="inline-table-form__actions">
        <button type="button" className="inline-table-form__icon-btn" onClick={onEdit}>
          <Pencil size={14} aria-hidden="true" />
          Edit
        </button>
      </div>
    );
  }
  return (
    <div className="inline-table-form__actions">
      <span className={`inline-table-form__status inline-table-form__status--${row.dirty ? "dirty" : "idle"}`}>
        {row.dirty ? "Unsaved" : "Open"}
      </span>
      <button
        type="button"
        className="inline-table-form__icon-btn inline-table-form__icon-btn--primary"
        disabled={saveDisabled}
        onClick={onSave}
      >
        <Check size={14} aria-hidden="true" />
        Save
      </button>
      <button type="button" className="inline-table-form__icon-btn" disabled={cancelDisabled} onClick={onCancel}>
        <X size={14} aria-hidden="true" />
        Cancel
      </button>
    </div>
  );
}

function AgentDocTitle({ draft }: { draft: AgentDocDraft }) {
  return (
    <span className="agent-docs__title">
      <strong>{draft.title}</strong>
      <span className="table__sub">{draft.summary}</span>
    </span>
  );
}

function RolePicker({
  roles,
  disabled,
  onChange,
}: {
  roles: readonly string[];
  disabled: boolean;
  onChange: (roles: string[]) => void;
}) {
  const selected = new Set(roles);
  return (
    <div className="inline-table-form__tag-picker" role="group" aria-label="Roles">
      <div className="inline-table-form__tag-options">
        {ROLE_OPTIONS.map((option) => (
          <button
            key={option.value}
            type="button"
            className={[
              "inline-table-form__tag-option",
              selected.has(option.value) ? "is-selected" : null,
            ].filter(Boolean).join(" ")}
            aria-pressed={selected.has(option.value)}
            disabled={disabled}
            onClick={() => onChange(toggleRole(roles, option.value))}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function AgentDocDetail({
  row,
  detailLoading,
  disabled,
  onBodyChange,
  onNotesChange,
}: {
  row: AgentDocRow;
  detailLoading: boolean;
  disabled: boolean;
  onBodyChange: (value: string) => void;
  onNotesChange: (value: string) => void;
}) {
  const draft = row.draft;
  return (
    <div className="agent-docs__detail">
      <div className="agent-docs__detail-head">
        <div>
          <span className="agent-docs__detail-label">Body</span>
          <p className="agent-docs__warning">{SAFETY_WARNING}</p>
        </div>
        <span className="agent-docs__tokens agent-docs__tokens--live">{tokenLabel(tokenCountForDraft(draft))}</span>
      </div>
      {detailLoading ? <Loading /> : null}
      <textarea
        className="inline-table-form__note agent-docs__body-editor"
        value={draft.body_md}
        disabled={disabled || detailLoading}
        aria-label={`Body for ${draft.slug}`}
        placeholder="Markdown body sent to matching chat agents."
        onChange={(event) => onBodyChange(event.target.value)}
      />
      <div className="agent-docs__detail-grid">
        <label className="agent-docs__field">
          <span className="agent-docs__detail-label">Change note</span>
          <textarea
            className="inline-table-form__note"
            value={draft.notes}
            disabled={disabled}
            aria-label={`Change note for ${draft.slug}`}
            placeholder="Optional operator note for this save."
            onChange={(event) => onNotesChange(event.target.value)}
          />
        </label>
        <div className="agent-docs__side">
          <div>
            <span className="agent-docs__detail-label">Capabilities</span>
            <RoleChips roles={draft.capabilities} />
          </div>
        </div>
      </div>
    </div>
  );
}

function RoleChips({ roles }: { roles: readonly string[] }) {
  const normalized = normalizeRoles(roles);
  const visibleRoles = normalized.length > 0 ? normalized : roles;
  return (
    <span className="agent-docs__chips">
      {visibleRoles.map((role) => (
        <Chip key={role} tone="ghost" size="sm">{role}</Chip>
      ))}
    </span>
  );
}

function RevisionState({ draft }: { draft: AgentDocDraft }) {
  return (
    <span className="agent-docs__revision">
      <span className="agent-docs__revision-line">
        <span className="muted">v{draft.version}</span>
        <Chip tone={draft.is_customised ? "sand" : "ghost"} size="sm">
          {draft.is_customised ? "customised" : "default"}
        </Chip>
      </span>
      <span className="agent-docs__tokens">{tokenLabel(tokenCountForDraft(draft))}</span>
      <DateTime value={draft.updated_at} className="muted agent-docs__updated" />
    </span>
  );
}

function rowFromDraft(draft: AgentDocDraft): AgentDocRow {
  return {
    id: draft.slug,
    draft,
    committedDraft: draft,
    editing: false,
    dirty: false,
    saving: false,
  };
}

function draftFromSummary(summary: AgentDocListItem): AgentDocDraft {
  return {
    slug: summary.slug,
    title: summary.title,
    summary: summary.summary,
    roles: [...summary.roles],
    capabilities: [...(summary.capabilities ?? [])],
    body_md: "",
    notes: "",
    updated_at: summary.updated_at,
    version: summary.version ?? 1,
    is_customised: summary.is_customised ?? false,
    default_hash: summary.default_hash,
    approx_token_count: summary.approx_token_count,
  };
}

function draftFromDoc(doc: AgentDocDetailItem): AgentDocDraft {
  return {
    slug: doc.slug,
    title: doc.title,
    summary: doc.summary,
    roles: [...doc.roles],
    capabilities: [...doc.capabilities],
    body_md: doc.body_md,
    notes: doc.notes ?? "",
    updated_at: doc.updated_at,
    version: doc.version,
    is_customised: doc.is_customised,
    default_hash: doc.default_hash,
    approx_token_count: doc.approx_token_count ?? approxTokenCount(doc.body_md),
  };
}

function summaryFromDraft(draft: AgentDocDraft): AgentDocListItem {
  return {
    slug: draft.slug,
    title: draft.title,
    summary: draft.summary,
    roles: [...draft.roles],
    capabilities: [...draft.capabilities],
    updated_at: draft.updated_at,
    version: draft.version,
    is_customised: draft.is_customised,
    default_hash: draft.default_hash,
    approx_token_count: draft.approx_token_count,
  };
}

function mergeSummary(
  draft: AgentDocDraft,
  summary: AgentDocListItem,
  options: { preserveEditable?: boolean } = {},
): AgentDocDraft {
  const incomingVersion = summary.version ?? draft.version;
  const keepLocal = draft.is_customised && draft.version > incomingVersion;
  return {
    ...draft,
    slug: summary.slug,
    title: summary.title,
    summary: summary.summary,
    roles: options.preserveEditable || keepLocal ? draft.roles : [...summary.roles],
    capabilities: summary.capabilities ? [...summary.capabilities] : draft.capabilities,
    updated_at: keepLocal ? draft.updated_at : summary.updated_at,
    version: keepLocal ? draft.version : incomingVersion,
    is_customised: keepLocal ? draft.is_customised : summary.is_customised ?? draft.is_customised,
    default_hash: summary.default_hash ?? draft.default_hash,
    approx_token_count: keepLocal ? draft.approx_token_count : summary.approx_token_count ?? draft.approx_token_count,
  };
}

function mergeDetailIntoDirtyDraft(draft: AgentDocDraft, detail: AgentDocDraft): AgentDocDraft {
  const bodyWasLoaded = draft.body_md.trim().length > 0;
  return {
    ...detail,
    roles: draft.roles,
    body_md: bodyWasLoaded ? draft.body_md : detail.body_md,
    notes: draft.notes,
    approx_token_count: bodyWasLoaded ? draft.approx_token_count : detail.approx_token_count,
  };
}

function shouldKeepLocalDraft(local: AgentDocDraft, incoming: AgentDocDraft): boolean {
  return local.is_customised && local.version > incoming.version;
}

function toggleRole(roles: readonly string[], role: AgentDocRole): string[] {
  const selected = new Set(roles);
  if (selected.has(role)) selected.delete(role);
  else selected.add(role);
  return ROLE_ORDER.filter((candidate) => selected.has(candidate));
}

function normalizeRoles(roles: readonly string[]): string[] {
  const selected = new Set(roles);
  return ROLE_ORDER.filter((role) => selected.has(role));
}

function validateDraft(draft: AgentDocDraft): string | null {
  if (!draft.body_md.trim()) return "Body is required before saving.";
  const roleValidation = validateRoles(draft.roles);
  if (roleValidation) return roleValidation;
  return null;
}

function validateRoles(roles: readonly string[]): string | null {
  if (roles.length === 0) return "Pick at least one role before saving.";
  const seen = new Set<string>();
  for (const role of roles) {
    if (!isAgentDocRole(role)) return "Roles must be manager, employee, or admin.";
    if (seen.has(role)) return "Roles must not contain duplicates.";
    seen.add(role);
  }
  return null;
}

function isAgentDocRole(role: string): role is AgentDocRole {
  return ROLE_ORDER.includes(role as AgentDocRole);
}

function approxTokenCount(body: string): number {
  const trimmed = body.trim();
  return trimmed ? Math.ceil(trimmed.length / 4) : 0;
}

function tokenCountForDraft(draft: AgentDocDraft): number | null {
  if (typeof draft.approx_token_count === "number") return draft.approx_token_count;
  return draft.body_md ? approxTokenCount(draft.body_md) : null;
}

function tokenLabel(count: number | null | undefined): string {
  if (typeof count !== "number") return "Approx. tokens unavailable";
  return `Approx. ${count.toLocaleString()} tokens`;
}
