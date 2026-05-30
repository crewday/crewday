import { useEffect, useMemo, useRef, useState } from "react";
import { RotateCcw } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  InlineNoteDisplay,
  InlineNoteField,
  InlineTagPickerField,
  InlineTableForm,
  type InlineTableColumn,
  type InlineTableRow,
} from "@/components/InlineTableForm";
import DeskPage from "@/components/DeskPage";
import DateTime from "@/components/DateTime";
import { Chip, Loading } from "@/components/common";
import { ApiError, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { AgentDoc, AgentDocRevision, AgentDocSummary } from "@/types/api";

type AgentDocRole = "manager" | "employee" | "admin";

interface AgentDocDraft extends AgentDocSummary {
  body_md: string;
  capabilities: string[];
  notes: string;
}

interface AgentDocSave {
  slug: string;
  draft: AgentDocDraft;
}

interface AgentDocReset {
  slug: string;
  notes: string;
}

const ROLE_OPTIONS = [
  { value: "manager", label: "Manager" },
  { value: "employee", label: "Employee" },
  { value: "admin", label: "Admin" },
] as const;

const ROLE_ORDER: AgentDocRole[] = ["manager", "employee", "admin"];
const SAFETY_WARNING =
  "Body is sent to every chat agent that loads this doc. Do not paste workspace secrets, customer data, or live API keys.";

export default function AdminAgentDocsPage() {
  const queryClient = useQueryClient();
  const [activeSlug, setActiveSlug] = useState<string | null>(null);
  const [rows, setRows] = useState<InlineTableRow<AgentDocDraft>[]>([]);
  const rowsRef = useRef<InlineTableRow<AgentDocDraft>[]>([]);

  const listQ = useQuery({
    queryKey: qk.adminAgentDocs(),
    queryFn: () => fetchJson<AgentDocSummary[]>("/admin/api/v1/agent_docs"),
  });

  const docQ = useQuery({
    queryKey: qk.adminAgentDoc(activeSlug ?? ""),
    queryFn: () => fetchJson<AgentDoc>(`/admin/api/v1/agent_docs/${activeSlug}`),
    enabled: activeSlug != null,
  });

  const revisionsQ = useQuery({
    queryKey: qk.adminAgentDocRevisions(activeSlug ?? ""),
    queryFn: () => fetchJson<AgentDocRevision[]>(`/admin/api/v1/agent_docs/${activeSlug}/revisions`),
    enabled: activeSlug != null,
  });

  const columns = useMemo<InlineTableColumn<AgentDocDraft>[]>(() => [
    {
      key: "slug",
      header: "Slug",
      width: { flex: 0.95, min: 170 },
      renderRead: ({ row }) => <code className="agent-docs__slug">{row.draft.slug}</code>,
      renderEdit: ({ row }) => <code className="agent-docs__slug">{row.draft.slug}</code>,
    },
    {
      key: "title",
      header: "Document",
      width: { flex: 1.8, min: 260 },
      renderRead: ({ row }) => <AgentDocTitle draft={row.draft} />,
      renderEdit: ({ row }) => <AgentDocTitle draft={row.draft} />,
    },
    {
      key: "roles",
      header: "Roles",
      width: { flex: 1, min: 210 },
      renderRead: ({ row }) => <RoleChips roles={row.draft.roles} />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineTagPickerField
          value={row.draft.roles}
          options={ROLE_OPTIONS}
          disabled={disabled}
          ariaLabel={`Roles for ${row.draft.slug}`}
          onChange={(roles) => update({ roles })}
        />
      ),
    },
    {
      key: "revision",
      header: "Revision",
      width: { flex: 0.9, min: 170 },
      renderRead: ({ row }) => <RevisionState draft={row.draft} />,
      renderEdit: ({ row }) => <RevisionState draft={row.draft} />,
    },
  ], []);

  const saveMutation = useMutation({
    mutationFn: ({ slug, draft }: AgentDocSave) => fetchJson<AgentDoc>(
      `/admin/api/v1/agent_docs/${slug}`,
      {
        method: "PUT",
        body: {
          body_md: draft.body_md,
          roles: normalizeRoles(draft.roles),
          notes: cleanNote(draft.notes),
        },
      },
    ),
    onSuccess: (updated) => {
      applyUpdatedDoc(updated);
      invalidateAgentDocQueries(queryClient, updated.slug);
    },
    onError: (error, vars) => {
      patchRow(vars.slug, (row) => ({
        ...row,
        saving: false,
        error: apiErrorMessage(error, "Could not save agent doc. Try again."),
      }));
    },
  });

  const resetMutation = useMutation({
    mutationFn: ({ slug, notes }: AgentDocReset) => fetchJson<AgentDoc>(
      `/admin/api/v1/agent_docs/${slug}/reset-to-default`,
      { method: "POST", body: { notes: cleanNote(notes) } },
    ),
    onSuccess: (updated) => {
      applyUpdatedDoc(updated);
      invalidateAgentDocQueries(queryClient, updated.slug);
    },
    onError: (error, vars) => {
      patchRow(vars.slug, (row) => ({
        ...row,
        saving: false,
        error: apiErrorMessage(error, "Could not reset agent doc. Your draft is still here."),
      }));
    },
  });

  useEffect(() => {
    rowsRef.current = rows;
  }, [rows]);

  useEffect(() => {
    if (!listQ.data) return;
    setRows((current) => listQ.data.map((summary) => {
      const existing = current.find((row) => row.id === summary.slug);
      if (!existing) return rowFromDraft(draftFromSummary(summary));
      const draft = mergeSummaryIntoDraft(existing.draft, summary, { preserveEditable: Boolean(existing.dirty) });
      const committedDraft = existing.committedDraft && !existing.dirty
        ? mergeSummaryIntoDraft(existing.committedDraft, summary)
        : existing.committedDraft;
      return { ...existing, draft, committedDraft, label: summary.title };
    }));
  }, [listQ.data]);

  useEffect(() => {
    if (!docQ.data || activeSlug == null) return;
    const draft = draftFromDoc(docQ.data);
    setRows((current) => current.map((row) => {
      if (row.id !== activeSlug || row.dirty) return row;
      return {
        ...row,
        draft,
        committedDraft: draft,
        editing: true,
        error: undefined,
        validation: undefined,
      };
    }));
  }, [activeSlug, docQ.data]);

  useEffect(() => {
    if (!docQ.isError || activeSlug == null) return;
    patchRow(activeSlug, (row) => ({
      ...row,
      error: apiErrorMessage(docQ.error, "Could not load agent doc. Try again."),
    }));
  }, [activeSlug, docQ.error, docQ.isError]);

  function patchRow(
    rowId: string,
    update: (row: InlineTableRow<AgentDocDraft>) => InlineTableRow<AgentDocDraft>,
  ): void {
    setRows((current) => current.map((row) => (row.id === rowId ? update(row) : row)));
  }

  function editRow(rowId: string): void {
    const cached = queryClient.getQueryData<AgentDoc>(qk.adminAgentDoc(rowId));
    const cachedDraft = cached ? draftFromDoc(cached) : null;
    setActiveSlug(rowId);
    patchRow(rowId, (row) => ({
      ...row,
      draft: cachedDraft ?? row.draft,
      committedDraft: cachedDraft ?? row.committedDraft ?? row.draft,
      editing: true,
      error: undefined,
      validation: undefined,
    }));
  }

  function cancelRow(rowId: string): void {
    patchRow(rowId, (row) => ({
      ...row,
      draft: row.committedDraft ?? row.draft,
      committedDraft: undefined,
      editing: false,
      dirty: false,
      saving: false,
      error: undefined,
      validation: undefined,
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
    patchRow(rowId, (current) => ({
      ...current,
      saving: true,
      validation: undefined,
      error: undefined,
    }));
    saveMutation.mutate({ slug: rowId, draft: row.draft });
  }

  function resetRow(rowId: string): void {
    const row = rowsRef.current.find((candidate) => candidate.id === rowId);
    if (!row) return;
    const confirmed = window.confirm(
      `Reset ${row.draft.slug} to the current code default? This replaces the edited body and roles.`,
    );
    if (!confirmed) return;
    patchRow(rowId, (current) => ({
      ...current,
      saving: true,
      validation: undefined,
      error: undefined,
    }));
    resetMutation.mutate({ slug: rowId, notes: row.draft.notes });
  }

  function applyUpdatedDoc(updated: AgentDoc): void {
    const draft = draftFromDoc(updated);
    setActiveSlug(updated.slug);
    queryClient.setQueryData(qk.adminAgentDoc(updated.slug), updated);
    queryClient.setQueryData<AgentDocSummary[]>(qk.adminAgentDocs(), (current) => (
      current ? current.map((item) => (item.slug === updated.slug ? summaryFromDoc(updated) : item)) : current
    ));
    setRows((current) => current.map((row) => (
      row.id === updated.slug
        ? {
          ...row,
          draft,
          committedDraft: draft,
          editing: true,
          dirty: false,
          saving: false,
          error: undefined,
          validation: undefined,
          label: updated.title,
        }
        : row
    )));
  }

  const sub =
    "System-side virtual files the chat agents read on demand (section 11 Agent knowledge tools).";

  if (listQ.isPending) {
    return <DeskPage title="Agent docs" sub={sub}><Loading /></DeskPage>;
  }
  if (listQ.isError || !listQ.data) {
    return <DeskPage title="Agent docs" sub={sub}>Failed to load.</DeskPage>;
  }

  return (
    <DeskPage title="Agent docs" sub={sub}>
      <section className="panel agent-docs">
        <InlineTableForm
          compact
          ariaLabel="Agent docs editor"
          className="agent-docs__table"
          columns={columns}
          rows={rows}
          saveMode="explicit"
          actionDisplay="icons"
          onDraftChange={(id, patch) => {
            patchRow(id, (row) => ({
              ...row,
              draft: { ...row.draft, ...patch },
              dirty: true,
              error: undefined,
              validation: undefined,
            }));
          }}
          onEdit={editRow}
          onCancel={cancelRow}
          onSave={saveRow}
          getRowLabel={(row) => row.draft.title || row.draft.slug}
          renderDetail={({ row, update, disabled }) => (
            row.editing ? (
              <AgentDocDetail
                row={row}
                disabled={disabled}
                detailLoading={activeSlug === row.id && docQ.isPending && !row.draft.body_md}
                revisionsLoading={activeSlug === row.id && revisionsQ.isPending}
                revisionsError={
                  activeSlug === row.id && revisionsQ.isError
                    ? apiErrorMessage(revisionsQ.error, "Could not load revision history.")
                    : null
                }
                revisions={activeSlug === row.id ? revisionsQ.data : undefined}
                onBodyChange={(body_md) => update({ body_md })}
                onNotesChange={(notes) => update({ notes })}
                onReset={() => resetRow(row.id)}
              />
            ) : null
          )}
        />
      </section>
    </DeskPage>
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

function RoleChips({ roles }: { roles: readonly string[] }) {
  const normalized = normalizeRoles(roles);
  return (
    <span className="agent-docs__chips">
      {(normalized.length > 0 ? normalized : roles).map((role) => (
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

function AgentDocDetail({
  row,
  disabled,
  detailLoading,
  revisionsLoading,
  revisionsError,
  revisions,
  onBodyChange,
  onNotesChange,
  onReset,
}: {
  row: InlineTableRow<AgentDocDraft>;
  disabled: boolean;
  detailLoading: boolean;
  revisionsLoading: boolean;
  revisionsError: string | null;
  revisions?: AgentDocRevision[];
  onBodyChange: (value: string) => void;
  onNotesChange: (value: string) => void;
  onReset: () => void;
}) {
  const draft = row.draft;
  return (
    <div className="agent-docs__detail">
      <div className="agent-docs__detail-head">
        <div>
          <span className="agent-docs__detail-label">Body</span>
          <p className="agent-docs__warning">{SAFETY_WARNING}</p>
        </div>
        <span className="agent-docs__tokens agent-docs__tokens--live">
          {tokenLabel(tokenCountForDraft(draft))}
        </span>
      </div>
      {detailLoading ? <Loading /> : null}
      <InlineNoteField
        value={draft.body_md}
        disabled={disabled}
        ariaLabel={`Body for ${draft.slug}`}
        placeholder="Markdown body sent to matching chat agents."
        onChange={onBodyChange}
      />
      <div className="agent-docs__detail-grid">
        <div className="agent-docs__field">
          <span className="agent-docs__detail-label">Change note</span>
          <InlineNoteField
            value={draft.notes}
            disabled={disabled}
            ariaLabel={`Change note for ${draft.slug}`}
            placeholder="Optional operator note for this save."
            onChange={onNotesChange}
          />
        </div>
        <div className="agent-docs__side">
          <div>
            <span className="agent-docs__detail-label">Capabilities</span>
            <RoleChips roles={draft.capabilities} />
          </div>
          <button
            type="button"
            className="btn btn--ghost btn--sm agent-docs__reset"
            disabled={disabled}
            aria-label={`Reset ${draft.slug} to code default`}
            onClick={onReset}
          >
            <RotateCcw size={14} aria-hidden="true" />
            Reset to code default
          </button>
        </div>
      </div>
      <RevisionHistory slug={draft.slug} loading={revisionsLoading} error={revisionsError} revisions={revisions} />
    </div>
  );
}

function RevisionHistory({
  slug,
  loading,
  error,
  revisions,
}: {
  slug: string;
  loading: boolean;
  error: string | null;
  revisions?: AgentDocRevision[];
}) {
  return (
    <section className="agent-docs__history" aria-label={`Revision history for ${slug}`}>
      <span className="agent-docs__detail-label">Revision history</span>
      {loading ? (
        <p className="muted">Loading revisions...</p>
      ) : error ? (
        <p className="inline-table-form__message inline-table-form__message--error">{error}</p>
      ) : revisions && revisions.length > 0 ? (
        <ol className="agent-docs__revision-list">
          {revisions.map((revision) => (
            <li key={`${revision.version}-${revision.created_at}`} className="agent-docs__revision-item">
              <span>
                <strong>v{revision.version}</strong>{" "}
                <DateTime value={revision.created_at} className="muted" />
              </span>
              <span>{tokenLabel(revision.approx_token_count)}</span>
              {revision.notes ? (
                <InlineNoteDisplay as="span" className="muted">{revision.notes}</InlineNoteDisplay>
              ) : null}
            </li>
          ))}
        </ol>
      ) : (
        <p className="muted">No revisions yet.</p>
      )}
    </section>
  );
}

function rowFromDraft(draft: AgentDocDraft): InlineTableRow<AgentDocDraft> {
  return {
    id: draft.slug,
    label: draft.title,
    draft,
    committedDraft: draft,
  };
}

function draftFromSummary(summary: AgentDocSummary): AgentDocDraft {
  return {
    ...summary,
    roles: [...summary.roles],
    body_md: "",
    capabilities: [],
    notes: "",
  };
}

function draftFromDoc(doc: AgentDoc): AgentDocDraft {
  return {
    ...doc,
    roles: [...doc.roles],
    body_md: doc.body_md,
    capabilities: doc.capabilities,
    notes: doc.notes ?? "",
  };
}

function mergeSummaryIntoDraft(
  draft: AgentDocDraft,
  summary: AgentDocSummary,
  options: { preserveEditable?: boolean } = {},
): AgentDocDraft {
  const editable = options.preserveEditable
    ? {
      body_md: draft.body_md,
      roles: draft.roles,
      notes: draft.notes,
    }
    : {
      roles: [...summary.roles],
    };
  return {
    ...draft,
    ...summary,
    ...editable,
  };
}

function summaryFromDoc(doc: AgentDoc): AgentDocSummary {
  const {
    slug,
    title,
    summary,
    roles,
    updated_at,
    version,
    is_customised,
    default_hash,
    metadata_default_hash,
    approx_token_count,
  } = doc;
  return {
    slug,
    title,
    summary,
    roles,
    updated_at,
    version,
    is_customised,
    default_hash,
    metadata_default_hash,
    approx_token_count,
  };
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

function cleanNote(note: string): string | null {
  const cleaned = note.trim();
  return cleaned ? cleaned : null;
}

function approxTokenCount(body: string): number {
  const trimmed = body.trim();
  return trimmed ? Math.ceil(trimmed.length / 4) : 0;
}

function tokenCountForDraft(draft: AgentDocDraft): number | null {
  return draft.body_md ? approxTokenCount(draft.body_md) : draft.approx_token_count ?? null;
}

function tokenLabel(count: number | null | undefined): string {
  if (typeof count !== "number") return "Approx. tokens unavailable";
  return `Approx. ${count.toLocaleString()} tokens`;
}

function apiErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    return error.userMessage ?? error.detail ?? error.title ?? error.message;
  }
  if (error instanceof Error) return error.message;
  return fallback;
}

function invalidateAgentDocQueries(queryClient: ReturnType<typeof useQueryClient>, slug: string): void {
  void queryClient.invalidateQueries({ queryKey: qk.adminAgentDocs() });
  void queryClient.invalidateQueries({ queryKey: qk.adminAgentDoc(slug) });
  void queryClient.invalidateQueries({ queryKey: qk.adminAgentDocRevisions(slug) });
}
