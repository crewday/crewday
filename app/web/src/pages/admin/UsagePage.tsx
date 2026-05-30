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
import { formatInteger } from "@/lib/numberFormat";
import DeskPage from "@/components/DeskPage";
import {
  InlineNumberField,
  InlineTableForm,
  InlineTableLoadMore,
  type InlineTableColumn,
  type InlineTableRow,
} from "@/components/InlineTableForm";
import { inlineTableNextCursor, useInlineTableInfiniteRows } from "@/components/InlineTableForm.rows";
import { Chip, Loading, ProgressBar, StatCard } from "@/components/common";
import type {
  AdminUsageSummary,
  AdminUsageWorkspaceRow,
  AdminUsageWorkspacesResponse,
} from "@/types/api";

const WORKSPACE_PAGE_SIZE = 25;
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
      allLoadedLabel="All workspace rows loaded"
    />
  );
}

interface UsageCapResponse {
  workspace_id: string;
  cap_cents_30d: number;
}

interface UsageWorkspaceDraft {
  name: string;
  slug: string;
  spentCents30d: number;
  capDollars: string;
  percent: number;
  paused: boolean;
}

type UsageWorkspacesCache =
  | AdminUsageWorkspacesResponse
  | InfiniteData<AdminUsageWorkspacesResponse>;

interface UsageCapMutationContext {
  previous: Array<[readonly unknown[], UsageWorkspacesCache | undefined]>;
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

function usagePercent(spentCents: number, capCents: number): number {
  if (capCents <= 0) return 100;
  return Math.min(100, Math.floor((spentCents * 100) / capCents));
}

function usageWorkspacesUrl(search: string, cursor: string | null): string {
  const params = new URLSearchParams({ limit: String(WORKSPACE_PAGE_SIZE) });
  const trimmed = search.trim();
  if (trimmed) params.set("q", trimmed);
  if (cursor) params.set("cursor", cursor);
  return "/admin/api/v1/usage/workspaces?" + params.toString();
}

function useDebouncedValue(value: string, delayMs: number): string {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timeout = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timeout);
  }, [delayMs, value]);

  return debounced;
}

function normalizeUsageWorkspacesResponse(
  response: AdminUsageWorkspacesResponse,
): AdminUsageWorkspacesResponse {
  return {
    ...response,
    data: response.data ?? response.workspaces,
    next_cursor: response.next_cursor ?? null,
    has_more: response.has_more ?? false,
  };
}

function usageWorkspaceToInlineRow(
  workspace: AdminUsageWorkspaceRow,
): InlineTableRow<UsageWorkspaceDraft> {
  const draft = {
    name: workspace.name,
    slug: workspace.slug,
    spentCents30d: workspace.spent_cents_30d,
    capDollars: centsToDollars(workspace.cap_cents_30d),
    percent: workspace.percent,
    paused: workspace.paused,
  };
  return {
    id: workspace.workspace_id,
    draft,
    committedDraft: draft,
    label: workspace.name,
  };
}

function updateUsageWorkspaceCap(
  workspace: AdminUsageWorkspaceRow,
  workspaceId: string,
  capCents: number,
): AdminUsageWorkspaceRow {
  if (workspace.workspace_id !== workspaceId) return workspace;
  return {
    ...workspace,
    cap_cents_30d: capCents,
    percent: usagePercent(workspace.spent_cents_30d, capCents),
    paused: capCents === 0 || workspace.spent_cents_30d >= capCents,
  };
}

function updateUsageWorkspacesPage(
  page: AdminUsageWorkspacesResponse,
  workspaceId: string,
  capCents: number,
): AdminUsageWorkspacesResponse {
  const workspaces = page.workspaces.map((workspace) =>
    updateUsageWorkspaceCap(workspace, workspaceId, capCents),
  );
  return {
    ...page,
    workspaces,
    data: (page.data ?? page.workspaces).map((workspace) =>
      updateUsageWorkspaceCap(workspace, workspaceId, capCents),
    ),
  };
}

function isInfiniteUsageCache(
  cache: UsageWorkspacesCache,
): cache is InfiniteData<AdminUsageWorkspacesResponse> {
  return "pages" in cache;
}

function updateUsageWorkspacesCache(
  cache: UsageWorkspacesCache | undefined,
  workspaceId: string,
  capCents: number,
): UsageWorkspacesCache | undefined {
  if (!cache) return cache;
  if (isInfiniteUsageCache(cache)) {
    return {
      ...cache,
      pages: cache.pages.map((page) =>
        updateUsageWorkspacesPage(page, workspaceId, capCents),
      ),
    };
  }
  return updateUsageWorkspacesPage(cache, workspaceId, capCents);
}

// react-doctor-disable-next-line react-doctor/no-giant-component -- Existing promoted surface is intentionally deferred until a focused component split preserves behavior.
export default function AdminUsagePage() {
  // code-health: ignore[nloc] Usage route keeps query orchestration and the single cap-editing table in one place.
  const qc = useQueryClient();
  const [workspaceSearch, setWorkspaceSearch] = useState("");
  const debouncedWorkspaceSearch = useDebouncedValue(
    workspaceSearch.trim(),
    WORKSPACE_SEARCH_DEBOUNCE_MS,
  );

  const summaryQ = useQuery({
    queryKey: qk.adminUsageSummary(),
    queryFn: () => fetchJson<AdminUsageSummary>("/admin/api/v1/usage/summary"),
  });

  const rowsQ = useInfiniteQuery<
    AdminUsageWorkspacesResponse,
    Error,
    InfiniteData<AdminUsageWorkspacesResponse>,
    readonly ["admin", "usage", "workspaces", { readonly q: string }],
    string | null
  >({
    queryKey: [...qk.adminUsageWorkspaces(), { q: debouncedWorkspaceSearch }],
    initialPageParam: null,
    queryFn: ({ pageParam, queryKey }) =>
      fetchJson<AdminUsageWorkspacesResponse>(
        usageWorkspacesUrl(queryKey[3].q, pageParam),
      ).then(normalizeUsageWorkspacesResponse),
    getNextPageParam: inlineTableNextCursor,
  });

  const workspaceColumns = useMemo<InlineTableColumn<UsageWorkspaceDraft>[]>(
    () => [
      {
        key: "workspace",
        header: "Workspace",
        width: { flex: 1.4, min: 220 },
        renderRead: ({ row }) => (
          <>
            {row.draft.name}
            <div className="table__sub">{row.draft.slug}</div>
          </>
        ),
        renderEdit: ({ row }) => (
          <>
            {row.draft.name}
            <div className="table__sub">{row.draft.slug}</div>
          </>
        ),
      },
      {
        key: "spend",
        header: "30d spend",
        align: "end",
        width: { px: 130 },
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
        width: { px: 150 },
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
        key: "usage",
        header: "Usage",
        width: { px: 150 },
        renderRead: ({ row }) => (
          <>
            <ProgressBar value={row.draft.percent} slim />
            <span className="muted"> {row.draft.percent}%</span>
          </>
        ),
        renderEdit: ({ row }) => (
          <>
            <ProgressBar value={row.draft.percent} slim />
            <span className="muted"> {row.draft.percent}%</span>
          </>
        ),
      },
      {
        key: "state",
        header: "State",
        width: { px: 116 },
        renderRead: ({ row }) =>
          row.draft.paused ? (
            <Chip tone="rust" size="sm">paused</Chip>
          ) : (
            <Chip tone="moss" size="sm">active</Chip>
          ),
        renderEdit: ({ row }) =>
          row.draft.paused ? (
            <Chip tone="rust" size="sm">paused</Chip>
          ) : (
            <Chip tone="moss" size="sm">active</Chip>
          ),
      },
    ],
    [],
  );

  const mapUsageRow = useCallback(
    (workspace: AdminUsageWorkspaceRow) => usageWorkspaceToInlineRow(workspace),
    [],
  );
  const workspaceRows = useInlineTableInfiniteRows<
    AdminUsageWorkspaceRow,
    UsageWorkspaceDraft
  >({
    data: rowsQ.data,
    getRowId: (workspace) => workspace.workspace_id,
    mapRow: mapUsageRow,
  });

  const setCap = useMutation<
    UsageCapResponse,
    Error,
    { id: string; capCents: number },
    UsageCapMutationContext
  >({
    mutationFn: ({ id, capCents }: { id: string; capCents: number }) =>
      fetchJson<UsageCapResponse>(`/admin/api/v1/usage/workspaces/${id}/cap`, {
        method: "PUT",
        body: { cap_cents_30d: capCents },
      }),
    onMutate: async ({ id, capCents }) => {
      await qc.cancelQueries({ queryKey: qk.adminUsageWorkspaces() });
      const previous = qc.getQueriesData<UsageWorkspacesCache>({
        queryKey: qk.adminUsageWorkspaces(),
      });
      qc.setQueriesData<UsageWorkspacesCache>(
        { queryKey: qk.adminUsageWorkspaces() },
        (current) => updateUsageWorkspacesCache(current, id, capCents),
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
      qc.invalidateQueries({ queryKey: qk.adminUsageWorkspaces() });
      qc.invalidateQueries({ queryKey: qk.adminUsageSummary() });
      qc.invalidateQueries({ queryKey: qk.adminWorkspaces() });
    },
  });

  const sub =
    "Rolling-30-day LLM spend per workspace. Adjust a workspace's cap to raise or tighten its envelope.";

  if (summaryQ.isPending) {
    return <DeskPage title="Usage" sub={sub}><Loading /></DeskPage>;
  }
  if (!summaryQ.data) {
    return <DeskPage title="Usage" sub={sub}>Failed to load.</DeskPage>;
  }

  const sum = summaryQ.data;
  const topCapability = sum.per_capability[0];
  const workspaceLoadError = rowsQ.error
    ? rowsQ.data
      ? "Could not load more workspace rows."
      : "Could not load workspace rows."
    : null;
  const workspaceResultSummary = rowsQ.isFetching && !rowsQ.isFetchingNextPage
    ? "Loading workspace rows"
    : workspaceSearch.trim()
      ? workspaceRows.loadedRowCount + " matching workspaces loaded"
      : workspaceRows.loadedRowCount + " workspaces loaded";
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
      if (rowsQ.data) {
        void rowsQ.fetchNextPage();
        return;
      }
      void rowsQ.refetch();
    },
  });

  return (
    <DeskPage title="Usage" sub={sub}>
      <section className="grid grid--stats">
        <StatCard
          label="30d spend"
          value={formatMoney(sum.deployment_spend_cents_30d, "USD")}
          sub={sum.window_label}
        />
        <StatCard
          label="Workspaces"
          value={sum.workspace_count}
          sub={sum.paused_workspace_count + " paused"}
          warn={sum.paused_workspace_count > 0}
        />
        <StatCard
          label="Calls (30d)"
          value={formatInteger(sum.deployment_calls_30d)}
        />
        <StatCard
          label="Top capability"
          value={topCapability?.capability ?? ","}
          sub={
            topCapability
              ? formatMoney(topCapability.spend_cents_30d, "USD")
              : undefined
          }
        />
      </section>

      <div className="panel">
        <header className="panel__head"><h2>Per workspace</h2></header>
        <InlineTableForm
          compact
          ariaLabel="Workspace usage caps"
          columns={workspaceColumns}
          rows={workspaceRows.rows}
          saveMode="explicit"
          search={{
            value: workspaceSearch,
            onChange: setWorkspaceSearch,
            label: "Search workspaces",
            placeholder: "Name or slug",
            clearLabel: "Clear workspace search",
            resultSummary: workspaceResultSummary,
            noResultsState: "No workspaces match this search.",
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

      <div className="panel">
        <header className="panel__head"><h2>Per capability (30d)</h2></header>
        <table className="table">
          <thead>
            <tr>
              <th>Capability</th><th>Calls</th><th>Spend</th>
            </tr>
          </thead>
          <tbody>
            {sum.per_capability
              .slice()
              .sort((a, b) => b.spend_cents_30d - a.spend_cents_30d)
              .map((c) => (
                <tr key={c.capability}>
                  <td><code className="inline-code">{c.capability}</code></td>
                  <td className="mono">{formatInteger(c.calls_30d)}</td>
                  <td className="mono">
                    {formatMoney(c.spend_cents_30d, "USD")}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
