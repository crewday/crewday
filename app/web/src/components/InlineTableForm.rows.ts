import { useCallback, useMemo, useRef, useState } from "react";
import type { ListEnvelope } from "@/lib/listResponse";
import type {
  InlineTableRow,
  UseInlineTableInfiniteRowsOptions,
  UseInlineTableInfiniteRowsResult,
} from "@/components/InlineTableForm";

export function inlineTableNextCursor<TItem>(page: ListEnvelope<TItem>): string | undefined {
  return page.has_more && page.next_cursor ? page.next_cursor : undefined;
}

export function useInlineTableInfiniteRows<TItem, TDraft>({
  data,
  mapRow,
  mergeRow = mergeInlineTableRowState,
}: UseInlineTableInfiniteRowsOptions<TItem, TDraft>): UseInlineTableInfiniteRowsResult<TDraft> {
  const [localRows, setLocalRows] = useState<ReadonlyMap<string, InlineTableRow<TDraft>>>(() => new Map());
  const rowsByIdRef = useRef<ReadonlyMap<string, InlineTableRow<TDraft>>>(new Map());
  const baseEntries = useMemo(() => {
    let rowIndex = 0;
    const entries: { item: TItem; row: InlineTableRow<TDraft>; index: number }[] = [];

    for (const page of data?.pages ?? []) {
      for (const item of page.data) {
        const baseRow = mapRow(item, rowIndex);
        entries.push({ item, row: baseRow, index: rowIndex });
        rowIndex += 1;
      }
    }

    return entries;
  }, [data, mapRow]);
  const baseRowsById = useMemo(() => new Map(baseEntries.map(({ row }) => [row.id, row])), [baseEntries]);
  let activeLocalRows = localRows;
  const normalizedLocalRows = normalizeInlineTableLocalRows(localRows, baseRowsById);
  if (normalizedLocalRows !== localRows) {
    activeLocalRows = normalizedLocalRows;
    setLocalRows(normalizedLocalRows);
  }

  const rows = useMemo(() => {
    const nextRows = baseEntries.map(({ item, row: baseRow, index }) => {
      const localRow = activeLocalRows.get(baseRow.id);
      return localRow ? mergeRow(baseRow, localRow, item, index) : baseRow;
    });
    rowsByIdRef.current = new Map(nextRows.map((row) => [row.id, row]));
    return nextRows;
  }, [activeLocalRows, baseEntries, mergeRow]);

  const updateRow = useCallback((rowId: string, update: (row: InlineTableRow<TDraft>) => InlineTableRow<TDraft>) => {
    setLocalRows((current) => {
      const row = current.get(rowId) ?? rowsByIdRef.current.get(rowId);
      if (!row) return current;
      const next = new Map(current);
      next.set(rowId, update(row));
      return next;
    });
  }, []);

  const patchRowDraft = useCallback((rowId: string, patch: Partial<TDraft>) => {
    updateRow(rowId, (row) => ({
      ...row,
      committedDraft: row.committedDraft ?? row.draft,
      draft: { ...row.draft, ...patch },
      dirty: true,
      error: undefined,
      validation: undefined,
    }));
  }, [updateRow]);

  const resetRow = useCallback((rowId: string) => {
    setLocalRows((current) => {
      if (!current.has(rowId)) return current;
      const next = new Map(current);
      next.delete(rowId);
      return next;
    });
  }, []);

  const resetRows = useCallback(() => setLocalRows(new Map()), []);
  const lastPage = data?.pages.at(-1);
  const nextPageCursor = lastPage ? inlineTableNextCursor(lastPage) : undefined;

  return {
    rows,
    loadedRowCount: rows.length,
    pageCount: data?.pages.length ?? 0,
    nextCursor: nextPageCursor ?? null,
    hasMore: nextPageCursor !== undefined,
    isEmpty: rows.length === 0,
    updateRow,
    patchRowDraft,
    resetRow,
    resetRows,
  };
}

function mergeInlineTableRowState<TDraft>(
  baseRow: InlineTableRow<TDraft>,
  localRow: InlineTableRow<TDraft>,
): InlineTableRow<TDraft> {
  return {
    ...baseRow,
    draft: localRow.draft,
    committedDraft: localRow.committedDraft ?? baseRow.committedDraft,
    editing: localRow.editing ?? baseRow.editing,
    dirty: localRow.dirty ?? baseRow.dirty,
    saving: localRow.saving ?? baseRow.saving,
    error: localRow.error ?? baseRow.error,
    validation: localRow.validation ?? baseRow.validation,
  };
}

function isLocalRowPending<TDraft>(row: InlineTableRow<TDraft>): boolean {
  return Boolean(row.editing || row.dirty || row.saving || row.error || row.validation);
}

function normalizeInlineTableLocalRows<TDraft>(
  current: ReadonlyMap<string, InlineTableRow<TDraft>>,
  baseRowsById: ReadonlyMap<string, InlineTableRow<TDraft>>,
): ReadonlyMap<string, InlineTableRow<TDraft>> {
  let changed = false;
  const next = new Map<string, InlineTableRow<TDraft>>();

  for (const [rowId, localRow] of current) {
    const baseRow = baseRowsById.get(rowId);
    if (!baseRow || (!isLocalRowPending(localRow) && shallowEqualDraft(baseRow.draft, localRow.draft))) {
      changed = true;
      continue;
    }
    next.set(rowId, localRow);
  }

  return changed ? next : current;
}

function shallowEqualDraft<TDraft>(left: TDraft, right: TDraft): boolean {
  if (Object.is(left, right)) return true;
  if (!isRecord(left) || !isRecord(right)) return false;
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  if (leftKeys.length !== rightKeys.length) return false;
  return leftKeys.every((key) => Object.prototype.hasOwnProperty.call(right, key)
    && Object.is(left[key], right[key]));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
