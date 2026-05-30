import { useCallback, useEffect, useMemo, useState } from "react";
import {
  type InfiniteData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import DeskPage from "@/components/DeskPage";
import DateTime from "@/components/DateTime";
import {
  InlineNumberField,
  InlineTableForm,
  InlineTableLoadMore,
  type InlineTableColumn,
  type InlineTableRow,
} from "@/components/InlineTableForm";
import { inlineTableNextCursor, useInlineTableInfiniteRows } from "@/components/InlineTableForm.rows";
import { Chip, Loading } from "@/components/common";
import type { AdminWorkspaceRow, AdminWorkspacesResponse } from "@/types/api";

const WORKSPACE_PAGE_SIZE = 25;
const ARCHIVE_SNAPSHOT_LIMIT = 500;
const WORKSPACE_SEARCH_DEBOUNCE_MS = 250;
const WORKSPACE_CAP_MAX_CENTS = 1_000_000;

function workspaceLoadMoreControl({
  hasMore,
  isInitialLoading,
  isFetchingMore,
  error,
  loadedCount,
  onLoadMore,
  onRetry,
}: {
  hasMore: boolean;
  isInitialLoading: boolean;
  isFetchingMore: boolean;
  error: string | null;
  loadedCount: number;
  onLoadMore: () => void;
  onRetry: () => void;
}) {
  return (
    <InlineTableLoadMore
      hasMore={hasMore}
      isInitialLoading={isInitialLoading}
      isFetchingMore={isFetchingMore}
      error={error}
      loadedCount={loadedCount}
      onLoadMore={onLoadMore}
      onRetry={onRetry}
      allLoadedLabel="All active workspace rows loaded"
    />
  );
}

const VERIFICATION_TONE: Record<
  AdminWorkspaceRow["verification_state"],
  "moss" | "sky" | "sand" | "ghost"
> = {
  trusted: "moss",
  human_verified: "sky",
  email_verified: "sand",
  unverified: "ghost",
  archived: "ghost",
};

interface TrustResponse {
  id: string;
  verification_state: AdminWorkspaceRow["verification_state"];
}

interface ArchiveResponse {
  id: string;
  archived_at: string;
}

interface UsageCapResponse {
  workspace_id: string;
  cap_cents_30d: number;
}

interface WorkspaceDraft {
  name: string;
  slug: string;
  plan: string;
  verificationState: AdminWorkspaceRow["verification_state"];
  propertiesCount: number;
  membersCount: number;
  spentCents30d: number;
  capDollars: string;
  createdAt: string;
}

type WorkspacesCache =
  | AdminWorkspacesResponse
  | InfiniteData<AdminWorkspacesResponse>;

interface WorkspaceMutationContext {
  previous: Array<[readonly unknown[], WorkspacesCache | undefined]>;
}

function dollarsToCents(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const match = trimmed.match(/^(\d+)(?:\.(\d{0,2}))?$|^\.(\d{1,2})$/);
  if (!match) return null;
  const dollars = Number(match[1] ?? "0");
  const centsPart = (match[2] ?? match[3] ?? "").padEnd(2, "0");
  const cents = dollars * 100 + Number(centsPart);
  if (cents > WORKSPACE_CAP_MAX_CENTS) return null;
  return cents;
}

function centsToDollars(value: number): string {
  // Input normalization for <input type="number">; grouping separators would be invalid.
  return (value / 100).toFixed(2);
}

function workspacesUrl(search: string, cursor: string | null): string {
  const params = new URLSearchParams({ limit: String(WORKSPACE_PAGE_SIZE) });
  const trimmed = search.trim();
  if (trimmed) params.set("q", trimmed);
  if (cursor) params.set("cursor", cursor);
  return "/admin/api/v1/workspaces?" + params.toString();
}

function useDebouncedValue(value: string, delayMs: number): string {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timeout = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timeout);
  }, [delayMs, value]);

  return debounced;
}

function normalizeWorkspacesResponse(
  response: AdminWorkspacesResponse,
): AdminWorkspacesResponse {
  return {
    ...response,
    data: response.data ?? response.workspaces,
    next_cursor: response.next_cursor ?? null,
    has_more: response.has_more ?? false,
  };
}

function activeWorkspacesData(
  data: InfiniteData<AdminWorkspacesResponse> | undefined,
): InfiniteData<AdminWorkspacesResponse> | undefined {
  if (!data) return data;
  return {
    ...data,
    pages: data.pages.map((page) => {
      const workspaces = page.workspaces.filter((workspace) => !workspace.archived_at);
      const pageData = (page.data ?? page.workspaces).filter((workspace) => !workspace.archived_at);
      return { ...page, workspaces, data: pageData };
    }),
  };
}

function workspaceToInlineRow(
  workspace: AdminWorkspaceRow,
): InlineTableRow<WorkspaceDraft> {
  const draft = {
    name: workspace.name,
    slug: workspace.slug,
    plan: workspace.plan,
    verificationState: workspace.verification_state,
    propertiesCount: workspace.properties_count,
    membersCount: workspace.members_count,
    spentCents30d: workspace.spent_cents_30d,
    capDollars: centsToDollars(workspace.cap_cents_30d),
    createdAt: workspace.created_at,
  };
  return {
    id: workspace.id,
    draft,
    committedDraft: draft,
    label: workspace.name,
  };
}

function updateWorkspacesPage(
  current: AdminWorkspacesResponse,
  id: string,
  update: (workspace: AdminWorkspaceRow) => AdminWorkspaceRow,
): AdminWorkspacesResponse {
  const updateRow = (workspace: AdminWorkspaceRow) =>
    workspace.id === id ? update(workspace) : workspace;
  return {
    ...current,
    workspaces: current.workspaces.map(updateRow),
    data: (current.data ?? current.workspaces).map(updateRow),
  };
}

function isInfiniteWorkspacesCache(
  cache: WorkspacesCache,
): cache is InfiniteData<AdminWorkspacesResponse> {
  return "pages" in cache;
}

function updateWorkspacesCache(
  cache: WorkspacesCache | undefined,
  id: string,
  update: (workspace: AdminWorkspaceRow) => AdminWorkspaceRow,
): WorkspacesCache | undefined {
  if (!cache) return cache;
  if (isInfiniteWorkspacesCache(cache)) {
    return {
      ...cache,
      pages: cache.pages.map((page) => updateWorkspacesPage(page, id, update)),
    };
  }
  return updateWorkspacesPage(cache, id, update);
}

// react-doctor-disable-next-line react-doctor/no-giant-component -- Existing promoted surface is intentionally deferred until a focused component split preserves behavior.
export default function AdminWorkspacesPage() {
  // code-health: ignore[nloc] Workspace admin route composes one optimistic mutation set and its active/archive tables.
  const qc = useQueryClient();
  const [workspaceSearch, setWorkspaceSearch] = useState("");
  const debouncedWorkspaceSearch = useDebouncedValue(
    workspaceSearch.trim(),
    WORKSPACE_SEARCH_DEBOUNCE_MS,
  );

  const rowsQ = useInfiniteQuery<
    AdminWorkspacesResponse,
    Error,
    InfiniteData<AdminWorkspacesResponse>,
    readonly ["admin", "workspaces", { readonly q: string }],
    string | null
  >({
    queryKey: [...qk.adminWorkspaces(), { q: debouncedWorkspaceSearch }],
    initialPageParam: null,
    queryFn: ({ pageParam, queryKey }) =>
      fetchJson<AdminWorkspacesResponse>(
        workspacesUrl(queryKey[2].q, pageParam),
      ).then(normalizeWorkspacesResponse),
    getNextPageParam: inlineTableNextCursor,
  });

  const archivedQ = useQuery({
    queryKey: [...qk.adminWorkspaces(), "archived-snapshot"],
    queryFn: () =>
      fetchJson<AdminWorkspacesResponse>(
        `/admin/api/v1/workspaces?limit=${ARCHIVE_SNAPSHOT_LIMIT}`,
      ).then(normalizeWorkspacesResponse),
  });

  const activeRowsData = useMemo(() => activeWorkspacesData(rowsQ.data), [rowsQ.data]);
  const mapWorkspaceRow = useCallback(
    (workspace: AdminWorkspaceRow) => workspaceToInlineRow(workspace),
    [],
  );
  const workspaceRows = useInlineTableInfiniteRows<
    AdminWorkspaceRow,
    WorkspaceDraft
  >({
    data: activeRowsData,
    getRowId: (workspace) => workspace.id,
    mapRow: mapWorkspaceRow,
  });

  const trust = useMutation<TrustResponse, Error, string, WorkspaceMutationContext>({
    mutationFn: (id: string) =>
      fetchJson<TrustResponse>(`/admin/api/v1/workspaces/${id}/trust`, {
        method: "POST",
      }),
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: qk.adminWorkspaces() });
      const previous = qc.getQueriesData<WorkspacesCache>({
        queryKey: qk.adminWorkspaces(),
      });
      qc.setQueriesData<WorkspacesCache>(
        { queryKey: qk.adminWorkspaces() },
        (current) => updateWorkspacesCache(current, id, (workspace) => ({
          ...workspace,
          verification_state: "trusted",
        })),
      );
      return { previous };
    },
    onError: (_err, _id, context) => {
      for (const [key, value] of context?.previous ?? []) {
        qc.setQueryData(key, value);
      }
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.adminWorkspaces() });
    },
  });

  const archive = useMutation<ArchiveResponse, Error, string, WorkspaceMutationContext>({
    mutationFn: (id: string) =>
      fetchJson<ArchiveResponse>(`/admin/api/v1/workspaces/${id}/archive`, {
        method: "POST",
      }),
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: qk.adminWorkspaces() });
      const previous = qc.getQueriesData<WorkspacesCache>({
        queryKey: qk.adminWorkspaces(),
      });
      const archivedAt = new Date().toISOString();
      qc.setQueriesData<WorkspacesCache>(
        { queryKey: qk.adminWorkspaces() },
        (current) => updateWorkspacesCache(current, id, (workspace) => ({
          ...workspace,
          archived_at: archivedAt,
        })),
      );
      return { previous };
    },
    onError: (_err, _id, context) => {
      for (const [key, value] of context?.previous ?? []) {
        qc.setQueryData(key, value);
      }
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.adminWorkspaces() });
    },
  });

  const setCap = useMutation<
    UsageCapResponse,
    Error,
    { id: string; capCents: number },
    WorkspaceMutationContext
  >({
    mutationFn: ({ id, capCents }: { id: string; capCents: number }) =>
      fetchJson<UsageCapResponse>(`/admin/api/v1/usage/workspaces/${id}/cap`, {
        method: "PUT",
        body: { cap_cents_30d: capCents },
      }),
    onMutate: async ({ id, capCents }) => {
      await qc.cancelQueries({ queryKey: qk.adminWorkspaces() });
      const previous = qc.getQueriesData<WorkspacesCache>({
        queryKey: qk.adminWorkspaces(),
      });
      qc.setQueriesData<WorkspacesCache>(
        { queryKey: qk.adminWorkspaces() },
        (current) => updateWorkspacesCache(current, id, (workspace) => ({
          ...workspace,
          cap_cents_30d: capCents,
        })),
      );
      return { previous };
    },
    onError: (_err, vars, context) => {
      for (const [key, value] of context?.previous ?? []) {
        qc.setQueryData(key, value);
      }
      workspaceRows.updateRow(vars.id, (row) => ({
        ...row,
        saving: false,
        error: "Could not save cap. Try again.",
      }));
    },
    onSuccess: (data, vars) => {
      workspaceRows.updateRow(vars.id, (row) => {
        const draft = { ...row.draft, capDollars: centsToDollars(data.cap_cents_30d) };
        return {
          ...row,
          draft,
          committedDraft: draft,
          editing: false,
          dirty: false,
          saving: false,
          error: undefined,
          validation: undefined,
        };
      });
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.adminWorkspaces() });
      qc.invalidateQueries({ queryKey: qk.adminUsageWorkspaces() });
      qc.invalidateQueries({ queryKey: qk.adminUsageSummary() });
    },
  });

  const lifecycleActions = useCallback(
    (row: InlineTableRow<WorkspaceDraft>) => (
      <div className="inline-actions">
        {row.draft.verificationState !== "trusted" && (
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            disabled={trust.isPending}
            onClick={() => trust.mutate(row.id)}
          >
            Trust
          </button>
        )}
        <button
          type="button"
          className="btn btn--rust btn--sm"
          disabled={archive.isPending}
          onClick={() => {
            if (confirm(`Archive ${row.draft.name}? Owner can restore from backup.`)) {
              archive.mutate(row.id);
            }
          }}
        >
          Archive
        </button>
      </div>
    ),
    [archive, trust],
  );

  const workspaceColumns = useMemo<InlineTableColumn<WorkspaceDraft>[]>(
    () => [
      {
        key: "workspace",
        header: "Workspace",
        width: { flex: 1.5, min: 220 },
        renderRead: ({ row }) => (
          <>
            {row.draft.name}
            <div className="table__sub">/w/{row.draft.slug}</div>
          </>
        ),
        renderEdit: ({ row }) => (
          <>
            {row.draft.name}
            <div className="table__sub">/w/{row.draft.slug}</div>
          </>
        ),
      },
      {
        key: "plan",
        header: "Plan",
        width: { px: 92 },
        renderRead: ({ row }) => (
          <Chip tone={row.draft.plan === "free" ? "ghost" : "sky"} size="sm">
            {row.draft.plan}
          </Chip>
        ),
        renderEdit: ({ row }) => (
          <Chip tone={row.draft.plan === "free" ? "ghost" : "sky"} size="sm">
            {row.draft.plan}
          </Chip>
        ),
      },
      {
        key: "verification",
        header: "Verification",
        width: { px: 142 },
        renderRead: ({ row }) => (
          <Chip tone={VERIFICATION_TONE[row.draft.verificationState]} size="sm">
            {row.draft.verificationState}
          </Chip>
        ),
        renderEdit: ({ row }) => (
          <Chip tone={VERIFICATION_TONE[row.draft.verificationState]} size="sm">
            {row.draft.verificationState}
          </Chip>
        ),
      },
      {
        key: "properties",
        header: "Properties",
        align: "end",
        width: { px: 104 },
        renderRead: ({ row }) => (
          <span className="mono">{row.draft.propertiesCount}</span>
        ),
        renderEdit: ({ row }) => (
          <span className="mono">{row.draft.propertiesCount}</span>
        ),
      },
      {
        key: "members",
        header: "Members",
        align: "end",
        width: { px: 92 },
        renderRead: ({ row }) => (
          <span className="mono">{row.draft.membersCount}</span>
        ),
        renderEdit: ({ row }) => (
          <span className="mono">{row.draft.membersCount}</span>
        ),
      },
      {
        key: "spend",
        header: "30d spend",
        align: "end",
        width: { px: 116 },
        renderRead: ({ row }) => (
          <span className="mono">
            {formatMoney(row.draft.spentCents30d, "USD")}
          </span>
        ),
        renderEdit: ({ row }) => (
          <span className="mono">
            {formatMoney(row.draft.spentCents30d, "USD")}
          </span>
        ),
      },
      {
        key: "cap",
        header: "Cap",
        align: "end",
        width: { px: 138 },
        renderRead: ({ row }) => (
          <span className="mono">
            {formatMoney(dollarsToCents(row.draft.capDollars) ?? 0, "USD")}
          </span>
        ),
        renderEdit: ({ row, update, disabled }) => (
          <InlineNumberField
            value={row.draft.capDollars}
            min={0}
            max={WORKSPACE_CAP_MAX_CENTS / 100}
            step="0.01"
            placeholder="0.00"
            disabled={disabled}
            ariaLabel="30 day cap dollars"
            onChange={(capDollars) => update({ capDollars })}
          />
        ),
      },
      {
        key: "created",
        header: "Created",
        width: { px: 132 },
        renderRead: ({ row }) => (
          <span className="mono muted">
            <DateTime value={row.draft.createdAt} />
          </span>
        ),
        renderEdit: ({ row }) => (
          <span className="mono muted">
            <DateTime value={row.draft.createdAt} />
          </span>
        ),
      },
      {
        key: "lifecycle",
        header: "Lifecycle",
        width: { px: 184 },
        renderRead: ({ row }) => lifecycleActions(row),
        renderEdit: ({ row }) => lifecycleActions(row),
      },
    ],
    [lifecycleActions],
  );

  const sub =
    "Every workspace on this deployment. Promote verification, archive on owner request, or drill into usage.";

  if (rowsQ.isPending) return <DeskPage title="Workspaces" sub={sub}><Loading /></DeskPage>;
  if (!rowsQ.data) return <DeskPage title="Workspaces" sub={sub}>Failed to load.</DeskPage>;

  const archived = (archivedQ.data?.workspaces ?? []).filter((w) => w.archived_at);
  const workspaceLoadError = rowsQ.error
    ? rowsQ.data
      ? "Could not load more active workspace rows."
      : "Could not load active workspace rows."
    : null;
  const workspaceResultSummary = rowsQ.isFetching && !rowsQ.isFetchingNextPage
    ? "Loading workspace rows"
    : workspaceSearch.trim()
      ? workspaceRows.loadedRowCount + " matching active workspaces loaded"
      : workspaceRows.loadedRowCount + " active workspaces loaded";
  const saveWorkspaceCap = (rowId: string) => {
    const row = workspaceRows.rows.find((candidate) => candidate.id === rowId);
    if (!row) return;
    const capCents = dollarsToCents(row.draft.capDollars);
    if (capCents === null) {
      workspaceRows.updateRow(rowId, (current) => ({
        ...current,
        dirty: true,
        validation: "Enter a dollar amount from 0.00 to 10000.00.",
      }));
      return;
    }
    workspaceRows.updateRow(rowId, (current) => ({
      ...current,
      saving: true,
      error: undefined,
      validation: undefined,
    }));
    setCap.mutate({ id: rowId, capCents });
  };
  const workspaceLoadMore = workspaceLoadMoreControl({
    hasMore: Boolean(rowsQ.hasNextPage),
    isInitialLoading: rowsQ.isPending,
    isFetchingMore: rowsQ.isFetchingNextPage,
    error: workspaceLoadError,
    loadedCount: workspaceRows.loadedRowCount,
    onLoadMore: () => {
      void rowsQ.fetchNextPage();
    },
    onRetry: () => {
      void rowsQ.fetchNextPage();
    },
  });

  return (
    <DeskPage title="Workspaces" sub={sub}>
      <div className="panel">
        <header className="panel__head"><h2>Active ({workspaceRows.loadedRowCount})</h2></header>
        <InlineTableForm
          compact
          ariaLabel="Active workspaces"
          columns={workspaceColumns}
          rows={workspaceRows.rows}
          saveMode="explicit"
          actionDisplay="text"
          search={{
            value: workspaceSearch,
            onChange: setWorkspaceSearch,
            label: "Search workspaces",
            placeholder: "Name or slug",
            clearLabel: "Clear workspace search",
            resultSummary: workspaceResultSummary,
            noResultsState: "No active workspaces match this search.",
          }}
          loadMore={workspaceLoadMore}
          onDraftChange={workspaceRows.patchRowDraft}
          onEdit={(rowId) => {
            workspaceRows.updateRow(rowId, (row) => ({
              ...row,
              editing: true,
              error: undefined,
              validation: undefined,
            }));
          }}
          onCancel={workspaceRows.resetRow}
          onSave={saveWorkspaceCap}
          getRowLabel={(row) => row.draft.name}
        />
      </div>

      {archived.length > 0 && (
        <div className="panel">
          <header className="panel__head"><h2>Archived ({archived.length})</h2></header>
          <table className="table">
            <thead>
              <tr>
                <th>Workspace</th>
                <th>Plan</th>
                <th>Archived on</th>
              </tr>
            </thead>
            <tbody>
              {archived.map((w) => (
                <tr key={w.id}>
                  <td>
                    {w.name}
                    <div className="table__sub">/w/{w.slug}</div>
                  </td>
                  <td className="muted">{w.plan}</td>
                  <td className="mono muted">
                    <DateTime value={w.archived_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </DeskPage>
  );
}
