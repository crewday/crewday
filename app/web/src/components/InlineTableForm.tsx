import {
  type ChangeEvent,
  type ComponentPropsWithoutRef,
  type CSSProperties,
  type FocusEvent,
  type KeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type MutableRefObject,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type Ref,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import type { InfiniteData } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Check, GripVertical, Loader2, Lock, Pencil, RotateCcw, Search, Trash2, X } from "lucide-react";
import ConfirmationModal from "@/components/ConfirmationModal";
import IconSelector from "@/components/IconSelector";
import { EmptyState } from "@/components/common";
import SearchableSelect, { type SearchableSelectOption } from "@/components/SearchableSelect";
import { useReorderableList } from "@/components/useReorderableList";
import type { ListEnvelope } from "@/lib/listResponse";

export type InlineTableSaveMode = "explicit" | "autosave" | "batch";
export type InlineTableRowStatus = "idle" | "dirty" | "saving" | "error" | "disabled";
export type InlineTableActionDisplay = "text" | "icons";
export type InlineTableActivationMode = "doubleClick" | "singleClick";
export type InlineTableColumnWidth =
  | number
  | { px: number }
  | { flex?: number; min?: number | string; max?: number | string };

const DELETE_KEY_WINDOW_MS = 650;
const DETAIL_COLUMN_KEY = "__detail";
const TABLE_ROLE = "table";
const ROWGROUP_ROLE = "rowgroup";
const ROW_ROLE = "row";
const COLUMNHEADER_ROLE = "columnheader";
const CELL_ROLE = "cell";
const SEARCH_ROLE = "search";
const STATUS_ROLE = "status";
const GROUP_ROLE = "group";
let createRowCounter = 0;

function InlineTableRowGroup({
  rowRef,
  ...props
}: ComponentPropsWithoutRef<"div"> & { rowRef?: Ref<HTMLDivElement> }) {
  return <div ref={rowRef} {...props} />;
}

export interface InlineTableColumn<TDraft> {
  key: string;
  header: ReactNode;
  mobileLabel?: ReactNode;
  /** Defaults to flex weight 1. Use `{ px }` for fixed columns or `{ flex, min, max }` for fluid ones. */
  width?: InlineTableColumnWidth;
  className?: string;
  align?: "start" | "center" | "end";
  renderRead: (context: InlineTableCellContext<TDraft>) => ReactNode;
  renderEdit: (context: InlineTableCellContext<TDraft>) => ReactNode;
}

export interface InlineTableRow<TDraft> {
  id: string;
  draft: TDraft;
  label?: string;
  /** Optional caller-owned baseline used by save/cancel flows; the component only passes it through. */
  committedDraft?: TDraft;
  editing?: boolean;
  dirty?: boolean;
  isNew?: boolean;
  disabled?: boolean;
  saving?: boolean;
  error?: ReactNode;
  validation?: ReactNode;
  meta?: ReactNode;
}

export interface InlineTableCellContext<TDraft> {
  row: InlineTableRow<TDraft>;
  saveMode: InlineTableSaveMode;
  disabled: boolean;
  messageId: string;
  validationMessageId: string;
  errorMessageId: string;
  update: (patch: Partial<TDraft>) => void;
  save: () => void;
  cancel: () => void;
}

export interface InlineTableReorder<TDraft> {
  rowId: string;
  fromIndex: number;
  toIndex: number;
  orderedRowIds: string[];
  orderedRows: readonly InlineTableRow<TDraft>[];
}

export interface InlineTableBatchContext<TDraft> {
  rows: readonly InlineTableRow<TDraft>[];
  dirtyRows: readonly InlineTableRow<TDraft>[];
  validationRows: readonly InlineTableRow<TDraft>[];
  errorRows: readonly InlineTableRow<TDraft>[];
  savingRows: readonly InlineTableRow<TDraft>[];
  disabledRows: readonly InlineTableRow<TDraft>[];
  hasDirtyRows: boolean;
  hasValidation: boolean;
  hasErrors: boolean;
  isSaving: boolean;
  canSubmit: boolean;
  canDiscard: boolean;
  discard: () => void;
}

export interface InlineTableDeleteConfirmationContext<TDraft> {
  row: InlineTableRow<TDraft>;
  rowIndex: number;
  label: string;
}

export interface InlineTableDeleteConfirmationCopy {
  title?: string;
  eyebrow?: string;
  confirmLabel?: string;
  tone?: "moss" | "rust";
  children: ReactNode;
}

export interface InlineTableSearchProps {
  value: string;
  onChange: (value: string) => void;
  label: string;
  placeholder?: string;
  clearLabel?: string;
  resultSummary?: ReactNode;
  filters?: ReactNode;
  /** Set when non-search filters are active so empty rows read as filtered results, not missing data. */
  isFiltered?: boolean;
  noResultsState?: ReactNode;
}

export interface UseInlineTableInfiniteRowsOptions<TItem, TDraft> {
  data?: Pick<InfiniteData<ListEnvelope<TItem>>, "pages"> | null;
  getRowId: (item: TItem) => string;
  mapRow: (item: TItem, index: number) => InlineTableRow<TDraft>;
  mergeRow?: (
    baseRow: InlineTableRow<TDraft>,
    localRow: InlineTableRow<TDraft>,
    item: TItem,
    index: number,
  ) => InlineTableRow<TDraft>;
}

export interface UseInlineTableInfiniteRowsResult<TDraft> {
  rows: readonly InlineTableRow<TDraft>[];
  loadedRowCount: number;
  pageCount: number;
  nextCursor: string | null;
  hasMore: boolean;
  isEmpty: boolean;
  updateRow: (rowId: string, update: (row: InlineTableRow<TDraft>) => InlineTableRow<TDraft>) => void;
  patchRowDraft: (rowId: string, patch: Partial<TDraft>) => void;
  resetRow: (rowId: string) => void;
  resetRows: () => void;
}

export interface InlineTableLoadMoreProps {
  hasMore: boolean;
  isInitialLoading?: boolean;
  isFetchingMore?: boolean;
  error?: ReactNode;
  loadedCount?: number;
  onLoadMore?: () => void;
  onRetry?: () => void;
  loadMoreLabel?: string;
  loadingLabel?: string;
  retryLabel?: string;
  allLoadedLabel?: string;
}

/**
 * Reusable inline table editor for dense operational forms.
 *
 * Keep the default optional props in normal product pages so row editing,
 * keyboard behavior, actions, and responsive downgrade stay coherent across
 * crew.day. Override them only for a specific workflow requirement, such as a
 * text-action table, a compact high-volume sheet, or a select-first table that
 * needs `e`/`dd` shortcuts.
 */
interface InlineTableFormBaseProps<TDraft> {
  ariaLabel: string;
  columns: readonly InlineTableColumn<TDraft>[];
  rows: readonly InlineTableRow<TDraft>[];
  onDraftChange: (rowId: string, patch: Partial<TDraft>) => void;
  onEdit?: (rowId: string) => void;
  onDelete?: (rowId: string) => void;
  /** Opt-in row reordering for read-mode rows. Caller owns persisting the returned order. */
  onReorder?: (reorder: InlineTableReorder<TDraft>) => void;
  /** Keeps the leading reorder affordance visible while a caller temporarily disables onReorder. */
  showReorderHandles?: boolean;
  /** Defaults to icons for dense sheets; use text when a page needs extra action clarity. */
  actionDisplay?: InlineTableActionDisplay;
  /** Defaults to single-click entry; use doubleClick for select-first workflows with `e`/`dd`. */
  activationMode?: InlineTableActivationMode;
  /** Prefer createEmptyDraft/onCreate for standard creation; use addRow for bespoke external controls. */
  addRow?: ReactNode;
  /** Enables the standard always-editing trailing create row. */
  createEmptyDraft?: () => TDraft;
  /** Called when the factory create row saves. Return false to keep the row open. */
  onCreate?: (draft: TDraft) => false | void;
  /** Optional validation for the factory create row; return a message to block save. */
  validateCreate?: (draft: TDraft) => ReactNode;
  createRowLabel?: string;
  /** Full-control escape hatch for uncommon create-row behavior. Prefer createEmptyDraft/onCreate. */
  trailingCreateRow?: InlineTableRow<TDraft>;
  /** Shown only when there are no rows and no trailing create row. */
  emptyState?: ReactNode;
  /** Optional caller-controlled search/filter chrome. Filtering remains caller-owned. */
  search?: InlineTableSearchProps;
  /** Optional standard footer slot for cursor loading controls and status. */
  loadMore?: ReactNode;
  /** Optional full-width detail line for notes, validation help, subtasks, or row metadata. */
  renderDetail?: (context: InlineTableCellContext<TDraft>) => ReactNode;
  /** Improves accessible labels and delete confirmation copy when row content has a stable name. */
  getRowLabel?: (row: InlineTableRow<TDraft>, index: number) => string;
  /** Optional caller-provided delete modal copy for domain-specific cascades or warnings. */
  renderDeleteConfirmation?: (context: InlineTableDeleteConfirmationContext<TDraft>) => InlineTableDeleteConfirmationCopy;
  /** Defaults to Delete; use Remove for soft-retire or unlink semantics. */
  deleteActionLabel?: string;
  /** Use only for semantic page-level variants; prefer the default class set. */
  className?: string;
  /** Defaults to the standard density; reserve compact for secondary or very high-volume sheets. */
  compact?: boolean;
}

interface InlineTableRowSaveProps {
  /** Defaults to autosave; use explicit only when the workflow needs a deliberate commit button. */
  saveMode?: "explicit" | "autosave";
  onSave: (rowId: string) => void;
  onCancel: (rowId: string) => void;
  renderBatchActions?: never;
  onBatchCancel?: never;
}

interface InlineTableBatchProps<TDraft> {
  saveMode: "batch";
  /** Batch mode uses caller-owned global actions; row save/cancel callbacks are optional no-ops. */
  onSave?: (rowId: string) => void;
  onCancel?: (rowId: string) => void;
  /** Batch-mode global action affordance for caller-owned submit/discard controls. */
  renderBatchActions?: (context: InlineTableBatchContext<TDraft>) => ReactNode;
  /** Optional batch-mode discard/cancel callback exposed through renderBatchActions context. */
  onBatchCancel?: () => void;
}

export type InlineTableFormProps<TDraft> = InlineTableFormBaseProps<TDraft> & (
  | InlineTableRowSaveProps
  | InlineTableBatchProps<TDraft>
);

// react-doctor-disable-next-line react-doctor/no-giant-component -- Existing promoted surface is intentionally deferred until a focused component split preserves behavior.
export function InlineTableForm<TDraft>({
  ariaLabel,
  columns,
  rows,
  saveMode = "autosave",
  onDraftChange,
  onSave,
  onCancel,
  onEdit,
  onDelete,
  onReorder,
  showReorderHandles = false,
  actionDisplay = "icons",
  activationMode = "singleClick",
  addRow,
  createEmptyDraft,
  onCreate,
  validateCreate,
  createRowLabel = "New row",
  trailingCreateRow,
  emptyState,
  search,
  loadMore,
  renderDetail,
  renderBatchActions,
  onBatchCancel,
  getRowLabel,
  renderDeleteConfirmation,
  deleteActionLabel = "Delete",
  className,
  compact = false,
}: InlineTableFormProps<TDraft>) {
  const tableId = useId();
  const rootRef = useRef<HTMLElement | null>(null);
  const pendingFocusRef = useRef<{ rowId: string; columnKey: string } | null>(null);
  const pendingRowFocusRef = useRef<string | null>(null);
  const pendingCreatedSelectionRef = useRef<{ previousIds: Set<string>; sourceRowId: string } | null>(null);
  const lastDeleteKeyRef = useRef<{ rowId: string; at: number } | null>(null);
  const deleteArmTimerRef = useRef<number | null>(null);
  const suppressAutosaveBlurRowsRef = useRef<Set<string>>(new Set());
  const exitedAutosaveRowsBeforeEditRef = useRef<Set<string>>(new Set());
  const pendingFormPointerTargetRef = useRef<EventTarget | null>(null);
  const [selectedRowId, setSelectedRowId] = useState<string | null>(null);
  const [deleteArmedRowId, setDeleteArmedRowId] = useState<string | null>(null);
  const [deleteConfirmationRowId, setDeleteConfirmationRowId] = useState<string | null>(null);
  const [factoryCreateRowState, setFactoryCreateRow] = useState<InlineTableRow<TDraft> | null>(null);
  const defaultFactoryCreateRow = useMemo(
    () => (createEmptyDraft && onCreate && !trailingCreateRow
      ? makeFactoryCreateRow(createEmptyDraft, createRowLabel)
      : null),
    [createEmptyDraft, createRowLabel, onCreate, trailingCreateRow],
  );
  const factoryCreateRow = factoryCreateRowState ?? defaultFactoryCreateRow;
  const updateFactoryCreateRow = (
    update: InlineTableRow<TDraft> | null | ((row: InlineTableRow<TDraft> | null) => InlineTableRow<TDraft> | null),
  ) => {
    setFactoryCreateRow((current) => (
      typeof update === "function" ? update(current ?? defaultFactoryCreateRow) : update
    ));
  };
  const hasReorderColumn = Boolean(onReorder) || showReorderHandles;
  const templateColumns = [
    ...(hasReorderColumn ? ["42px"] : []),
    ...columns.map((column) => columnTemplate(column.width)),
    "max-content",
  ].join(" ");
  const classes = [
    "inline-table-form",
    `inline-table-form--${saveMode}`,
    compact ? "inline-table-form--compact" : null,
    className,
  ].filter(Boolean).join(" ");
  const activeTrailingCreateRow = trailingCreateRow ?? factoryCreateRow ?? undefined;
  const renderedRows = activeTrailingCreateRow ? [...rows, activeTrailingCreateRow] : rows;
  const hasActiveSearch = search ? search.value.trim().length > 0 || Boolean(search.isFiltered) : false;
  const batchActions = saveMode === "batch" && renderBatchActions
    ? renderBatchActions(makeBatchContext(rows, onBatchCancel))
    : null;
  const reorderableRows = onReorder
    ? rows.filter((row) => !isRowEditing(row) && !isRowDisabled(row))
    : [];

  const reorderRow = (rowId: string, toMovableIndex: number) => {
    if (!onReorder) return;
    const movingRow = rows.find((row) => row.id === rowId);
    if (!movingRow || isRowEditing(movingRow) || isRowDisabled(movingRow)) return;
    const fromIndex = rows.findIndex((row) => row.id === rowId);
    const targetRow = reorderableRows[toMovableIndex];
    const toIndex = targetRow ? rows.findIndex((row) => row.id === targetRow.id) : -1;
    if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex) return;
    const orderedRows = moveRow(rows, fromIndex, toIndex);
    clearDeleteArm();
    selectRow(rowId, true);
    onReorder({
      rowId,
      fromIndex,
      toIndex,
      orderedRows,
      orderedRowIds: orderedRows.map((row) => row.id),
    });
  };

  const reorderable = useReorderableList({
    items: reorderableRows,
    getId: (row) => row.id,
    onMove: reorderRow,
    disabled: !onReorder || reorderableRows.length < 2,
  });
  const reorderListProps = reorderable.getListProps();

  useEffect(() => {
    const target = pendingFocusRef.current;
    if (!target) return;
    const focused = focusCellControl(rootRef.current, target.rowId, target.columnKey);
    if (focused) pendingFocusRef.current = null;
  }, [renderedRows]);

  useEffect(() => {
    const rowId = pendingRowFocusRef.current;
    if (!rowId) return;
    const focused = focusRowGroup(rootRef.current, rowId);
    if (focused) pendingRowFocusRef.current = null;
  }, [renderedRows, selectedRowId]);

  useEffect(() => () => {
    if (deleteArmTimerRef.current) window.clearTimeout(deleteArmTimerRef.current);
  }, []);

  useEffect(() => {
    const clearPendingPointerTarget = () => {
      pendingFormPointerTargetRef.current = null;
    };
    window.addEventListener("pointerup", clearPendingPointerTarget, true);
    window.addEventListener("pointercancel", clearPendingPointerTarget, true);
    return () => {
      window.removeEventListener("pointerup", clearPendingPointerTarget, true);
      window.removeEventListener("pointercancel", clearPendingPointerTarget, true);
    };
  }, []);

  const clearDeleteArm = () => {
    if (deleteArmTimerRef.current) {
      window.clearTimeout(deleteArmTimerRef.current);
      deleteArmTimerRef.current = null;
    }
    lastDeleteKeyRef.current = null;
    setDeleteArmedRowId(null);
  };

  const armDeleteRow = (rowId: string) => {
    if (deleteArmTimerRef.current) window.clearTimeout(deleteArmTimerRef.current);
    setDeleteArmedRowId(rowId);
    deleteArmTimerRef.current = window.setTimeout(() => {
      lastDeleteKeyRef.current = null;
      setDeleteArmedRowId(null);
      deleteArmTimerRef.current = null;
    }, DELETE_KEY_WINDOW_MS);
  };

  const selectRow = (rowId: string, shouldFocus = false) => {
    setSelectedRowId(rowId);
    if (deleteArmedRowId !== rowId) clearDeleteArm();
    if (shouldFocus) pendingRowFocusRef.current = rowId;
  };

  useEffect(() => {
    if (!selectedRowId) return;
    const clearSelectionForOutsidePointer = (event: PointerEvent) => {
      if (selectedRowContainsTarget(rootRef.current, selectedRowId, event.target)) return;
      pendingRowFocusRef.current = null;
      clearDeleteArm();
      setSelectedRowId(null);
    };
    window.addEventListener("pointerdown", clearSelectionForOutsidePointer, true);
    return () => {
      window.removeEventListener("pointerdown", clearSelectionForOutsidePointer, true);
    };
  }, [selectedRowId]);

  const suppressAutosaveBlur = (rowId: string) => {
    suppressAutosaveBlurRowsRef.current.add(rowId);
  };

  const consumeSuppressedAutosaveBlur = (rowId: string) => {
    if (!suppressAutosaveBlurRowsRef.current.has(rowId)) return false;
    suppressAutosaveBlurRowsRef.current.delete(rowId);
    return true;
  };

  const clearSuppressedAutosaveBlur = (rowId: string) => {
    suppressAutosaveBlurRowsRef.current.delete(rowId);
  };

  const focusMovedInsideForm = (target: EventTarget | null) => (
    target instanceof Node && Boolean(rootRef.current?.contains(target))
  );

  const canAutoExitRow = (row: InlineTableRow<TDraft>) => (
    saveMode !== "batch"
    && row.id !== activeTrailingCreateRow?.id
  );

  const exitAutosaveRowsBeforeEdit = (targetRowId: string) => {
    if (saveMode === "batch") return;
    for (const candidate of renderedRows) {
      if (
        candidate.id === targetRowId
        || !canAutoExitRow(candidate)
        || !isRowEditing(candidate)
        || candidate.saving
        || candidate.disabled
        || exitedAutosaveRowsBeforeEditRef.current.has(candidate.id)
      ) {
        continue;
      }
      exitedAutosaveRowsBeforeEditRef.current.add(candidate.id);
      suppressAutosaveBlur(candidate.id);
      if (candidate.dirty) {
        onSave?.(candidate.id);
      } else {
        onCancel?.(candidate.id);
      }
    }
  };

  const focusEditCell = (rowId: string, columnKey: string, options?: { alreadyEditing?: boolean }) => {
    clearDeleteArm();
    pendingFocusRef.current = { rowId, columnKey };
    if (options?.alreadyEditing && focusCellControl(rootRef.current, rowId, columnKey)) {
      pendingFocusRef.current = null;
      return;
    }
    exitAutosaveRowsBeforeEdit(rowId);
    onEdit?.(rowId);
    exitedAutosaveRowsBeforeEditRef.current.clear();
  };

  const editRow = (rowId: string, columnKey: string) => {
    if (!onEdit) return;
    focusEditCell(rowId, columnKey);
  };

  const updateRowDraft = (row: InlineTableRow<TDraft>, patch: Partial<TDraft>) => {
    if (factoryCreateRow?.id === row.id) {
      updateFactoryCreateRow((current) => current
        ? {
          ...current,
          draft: { ...current.draft, ...patch },
          dirty: true,
          validation: undefined,
          error: undefined,
        }
        : current);
      return;
    }
    onDraftChange(row.id, patch);
  };

  const saveRow = (row: InlineTableRow<TDraft>, isTrailingCreate: boolean) => {
    if (factoryCreateRow?.id !== row.id) {
      exitRow(row, () => onSave?.(row.id), { selectCreated: row.isNew || isTrailingCreate });
      return;
    }
    const validation = validateCreate?.(row.draft);
    if (validation) {
      updateFactoryCreateRow((current) => current ? { ...current, dirty: true, validation } : current);
      return;
    }
    const result = onCreate?.(row.draft);
    if (result === false) return;
    pendingCreatedSelectionRef.current = {
      previousIds: new Set(renderedRows.map((candidate) => candidate.id)),
      sourceRowId: row.id,
    };
    clearDeleteArm();
    updateFactoryCreateRow(makeFactoryCreateRow(createEmptyDraft ?? (() => row.draft), createRowLabel));
  };

  const cancelRow = (row: InlineTableRow<TDraft>) => {
    if (factoryCreateRow?.id === row.id) {
      clearDeleteArm();
      updateFactoryCreateRow(makeFactoryCreateRow(createEmptyDraft ?? (() => row.draft), createRowLabel));
      return;
    }
    exitRow(row, () => onCancel?.(row.id));
  };

  const exitRow = (row: InlineTableRow<TDraft>, action: () => void, options?: { selectCreated?: boolean }) => {
    suppressAutosaveBlur(row.id);
    if (options?.selectCreated) {
      pendingCreatedSelectionRef.current = {
        previousIds: new Set(renderedRows.map((candidate) => candidate.id)),
        sourceRowId: row.id,
      };
    }
    clearDeleteArm();
    selectRow(row.id, true);
    action();
  };

  const requestDelete = (rowId: string) => {
    suppressAutosaveBlur(rowId);
    clearDeleteArm();
    selectRow(rowId);
    setDeleteConfirmationRowId(rowId);
  };

  const cancelDelete = () => {
    if (deleteConfirmationRowId) clearSuppressedAutosaveBlur(deleteConfirmationRowId);
    setDeleteConfirmationRowId(null);
  };

  const confirmDelete = () => {
    const rowId = deleteConfirmationRowId;
    if (!rowId || !onDelete) return;
    clearSuppressedAutosaveBlur(rowId);
    selectDeleteTarget(rowId);
    onDelete(rowId);
  };

  const deleteFromKeyboard = (rowId: string) => {
    if (!onDelete) return;
    if (renderDeleteConfirmation) {
      requestDelete(rowId);
      return;
    }
    clearDeleteArm();
    selectDeleteTarget(rowId);
    onDelete(rowId);
  };

  const selectDeleteTarget = (rowId: string) => {
    const selectableRowIds = selectableRows().map((row) => row.id);
    const currentIndex = selectableRowIds.indexOf(rowId);
    if (currentIndex === -1) {
      setSelectedRowId(null);
      return;
    }
    const nextRowId = selectableRowIds[currentIndex + 1] ?? selectableRowIds[currentIndex - 1] ?? null;
    if (nextRowId) {
      selectRow(nextRowId, true);
      return;
    }
    setSelectedRowId(null);
  };

  const selectableRows = () => renderedRows.filter((candidate) => (
    (!isRowEditing(candidate) || candidate.id === activeTrailingCreateRow?.id) && !isRowDisabled(candidate)
  ));

  const selectPendingCreatedRow = (row: InlineTableRow<TDraft>) => (node: HTMLDivElement | null) => {
    if (!node) return;
    const pending = pendingCreatedSelectionRef.current;
    if (
      !pending
      || pending.previousIds.has(row.id)
      || isRowEditing(row)
      || isRowDisabled(row)
    ) {
      return;
    }
    pendingCreatedSelectionRef.current = null;
    setSelectedRowId(row.id);
    pendingRowFocusRef.current = row.id;
    node.focus();
  };

  const pendingCreatedSelection = pendingCreatedSelectionRef.current;
  const pendingCreatedRowId = pendingCreatedSelection
    ? renderedRows.find((row) => (
      !pendingCreatedSelection.previousIds.has(row.id) && !isRowEditing(row) && !isRowDisabled(row)
    ))?.id ?? null
    : null;
  const effectiveSelectedRowId = pendingCreatedRowId ?? selectedRowId;
  if (pendingCreatedSelection && !pendingCreatedRowId) {
    const sourceRow = renderedRows.find((row) => row.id === pendingCreatedSelection.sourceRowId);
    if (!sourceRow || sourceRow.validation || sourceRow.error) {
      pendingCreatedSelectionRef.current = null;
    }
  }

  const moveSelectedRow = (rowId: string, direction: "previous" | "next") => {
    const selectableRowIds = selectableRows().map((candidate) => candidate.id);
    const currentIndex = selectableRowIds.indexOf(rowId);
    if (currentIndex === -1) return;
    const nextIndex = direction === "previous" ? currentIndex - 1 : currentIndex + 1;
    const nextRowId = selectableRowIds[nextIndex];
    if (!nextRowId) return;
    clearDeleteArm();
    selectRow(nextRowId, true);
  };

  const deleteConfirmationRow = deleteConfirmationRowId
    ? renderedRows.find((row) => row.id === deleteConfirmationRowId)
    : undefined;
  const deleteConfirmationLabel = deleteConfirmationRow
    ? rowLabel(deleteConfirmationRow, renderedRows.indexOf(deleteConfirmationRow), getRowLabel)
    : "this row";
  const deleteConfirmationCopy = deleteConfirmationRow
    ? renderDeleteConfirmation?.({
      row: deleteConfirmationRow,
      rowIndex: renderedRows.indexOf(deleteConfirmationRow),
      label: deleteConfirmationLabel,
    })
    : undefined;

  return (
    <section
      ref={rootRef}
      className={classes}
      style={{ "--inline-table-columns": templateColumns } as CSSProperties}
      aria-label={ariaLabel}
      onPointerDownCapture={(event) => {
        pendingFormPointerTargetRef.current = event.target;
      }}
      onPointerUpCapture={() => {
        pendingFormPointerTargetRef.current = null;
      }}
      onPointerCancelCapture={() => {
        pendingFormPointerTargetRef.current = null;
      }}
    >
      {search ? (
        <InlineTableSearchToolbar
          tableLabel={ariaLabel}
          search={search}
        />
      ) : null}
      <div className="inline-table-form__table" role={TABLE_ROLE} aria-label={ariaLabel}>
        <div className="inline-table-form__head" role={ROWGROUP_ROLE}>
          <div className="inline-table-form__row inline-table-form__row--head" role={ROW_ROLE}>
            {hasReorderColumn ? (
              <div
                className="inline-table-form__th inline-table-form__th--reorder"
                role={COLUMNHEADER_ROLE}
              />
            ) : null}
            {columns.map((column) => (
              <div
                key={column.key}
                className={cellClasses(column, "inline-table-form__th")}
                role={COLUMNHEADER_ROLE}
              >
                {column.header}
              </div>
            ))}
            <div className="inline-table-form__th inline-table-form__th--actions" role={COLUMNHEADER_ROLE} aria-label="Actions" />
          </div>
        </div>

        <div
          className="inline-table-form__body"
          role={ROWGROUP_ROLE}
          onDragLeave={reorderListProps.onDragLeave}
          onDrop={reorderListProps.onDrop}
        >
          {renderedRows.length === 0 ? (
            <div className="inline-table-form__empty" role={ROW_ROLE}>
              <div
                className="inline-table-form__empty-cell"
                role={CELL_ROLE}
                aria-colspan={columns.length + (hasReorderColumn ? 2 : 1)}
              >
                {hasActiveSearch ? search?.noResultsState ?? (
                  <EmptyState
                    variant="compact"
                    title="No matching rows"
                    copy="Clear search or adjust filters to see rows."
                  />
                ) : emptyState ?? (
                  <EmptyState
                    variant="compact"
                    title="No rows yet"
                    copy="Rows will appear here when there is something to edit."
                  />
                )}
              </div>
            </div>
          ) : null}

          {renderedRows.map((row, index) => {
            const label = rowLabel(row, index, getRowLabel);
            const isTrailingCreate = activeTrailingCreateRow?.id === row.id;
            const isFactoryCreate = factoryCreateRow?.id === row.id;
            const status = rowStatus(row);
            const editing = row.editing ?? false;
            const renderEditing = editing && !row.saving;
            const controlsDisabled = row.disabled || row.saving || status === "disabled";
            const selectionDisabled = row.disabled || status === "disabled";
            const messageId = rowMessageId(tableId, row.id);
            const validationMessageId = rowValidationMessageId(tableId, row.id);
            const errorMessageId = rowErrorMessageId(tableId, row.id);
            const context: InlineTableCellContext<TDraft> = {
              row,
              saveMode,
              disabled: controlsDisabled,
              messageId,
              validationMessageId,
              errorMessageId,
              update: (patch) => updateRowDraft(row, patch),
              save: () => saveRow(row, isTrailingCreate),
              cancel: () => cancelRow(row),
            };
            const detail = renderDetail?.(context);
            const selected = effectiveSelectedRowId === row.id && !selectionDisabled && (!renderEditing || isTrailingCreate);
            const deleteArmed = deleteArmedRowId === row.id && selected;
            const movableIndex = reorderableRows.findIndex((candidate) => candidate.id === row.id);
            const canShowReorderHandle = hasReorderColumn && !row.disabled && !isTrailingCreate;
            const canReorderRow = canShowReorderHandle && !editing && !row.saving && movableIndex >= 0;
            const reorderItemProps = canReorderRow ? reorderable.getItemProps(movableIndex) : null;
            const dropPosition = reorderable.dropTarget?.id === row.id
              ? reorderable.dropTarget.position
              : null;
            const isDragging = reorderable.draggedId === row.id;
            const activateCell = (columnKey: string) => {
              if (renderEditing || controlsDisabled || !onEdit) return;
              editRow(row.id, columnKey);
            };
            const activateDetail = () => activateCell(DETAIL_COLUMN_KEY);
            const rowGroupActivationProps = activationMode === "doubleClick" && !renderEditing && !controlsDisabled
              ? {
                  onPointerUp: (event: ReactPointerEvent<HTMLDivElement>) => {
                    if (isInteractiveEventTarget(event.target)) return;
                    selectRow(row.id);
                    event.currentTarget.focus();
                  },
                  onClick: (event: ReactMouseEvent<HTMLDivElement>) => {
                    if (isInteractiveEventTarget(event.target)) return;
                    selectRow(row.id);
                    event.currentTarget.focus();
                  },
                }
              : {};

            return (
              <InlineTableRowGroup
                key={row.id}
                rowRef={selectPendingCreatedRow(row)}
                tabIndex={(renderEditing && !isTrailingCreate) || selectionDisabled ? undefined : 0}
                className={[
                  "inline-table-form__group",
                  row.isNew && !row.saving ? "inline-table-form__group--new" : null,
                  isTrailingCreate ? "inline-table-form__group--trailing-create" : null,
                  renderEditing ? "is-editing" : "is-reading",
                  row.dirty && !row.saving ? "is-dirty" : null,
                  row.saving ? "is-saving" : null,
                  row.error ? "has-error" : null,
                  row.validation ? "has-validation" : null,
                  row.disabled ? "is-disabled" : null,
                  selected ? "is-selected" : null,
                  deleteArmed ? "is-delete-armed" : null,
                  isDragging ? "is-dragging" : null,
                  dropPosition ? `inline-table-form__group--drop-${dropPosition}` : null,
                ].filter(Boolean).join(" ")}
                draggable={reorderItemProps?.draggable}
                data-inline-table-row-group={row.id}
                role={ROWGROUP_ROLE}
                aria-label={label}
                {...rowGroupActivationProps}
                onDragStart={reorderItemProps?.onDragStart}
                onDragOver={reorderItemProps?.onDragOver}
                onDragLeave={reorderItemProps?.onDragLeave}
                onDrop={reorderItemProps?.onDrop}
                onDragEnd={reorderItemProps?.onDragEnd}
                onBlur={(event) => {
                  if (!canAutoExitRow(row) || row.saving || row.disabled) return;
                  if (focusStayedInside(event)) return;
                  if (consumeSuppressedAutosaveBlur(row.id)) {
                    return;
                  }
                  const movedInsideForm = focusMovedInsideForm(event.relatedTarget)
                    || focusMovedInsideForm(pendingFormPointerTargetRef.current);
                  if (row.dirty) {
                    if (movedInsideForm) exitedAutosaveRowsBeforeEditRef.current.add(row.id);
                    onSave?.(row.id);
                    return;
                  }
                  if (editing && !isTrailingCreate) {
                    if (movedInsideForm) exitedAutosaveRowsBeforeEditRef.current.add(row.id);
                    onCancel?.(row.id);
                  }
                }}
                onKeyDown={(event) => {
                  if (!renderEditing || (isTrailingCreate && !isEditableShortcutTarget(event.target))) {
                    handleReadRowKeyDown(event, {
                      rowId: row.id,
                      disabled: controlsDisabled,
                      selected,
                      onEdit: onEdit || isTrailingCreate ? () => {
                        const firstColumn = columns[0];
                        if (firstColumn) focusEditCell(row.id, firstColumn.key, { alreadyEditing: renderEditing });
                      } : undefined,
                      onDelete: onDelete && !isTrailingCreate ? () => deleteFromKeyboard(row.id) : undefined,
                      onArmDelete: () => armDeleteRow(row.id),
                      onMoveSelection: (direction) => moveSelectedRow(row.id, direction),
                      lastDeleteKeyRef,
                    });
                    return;
                  }
                  if (saveMode === "batch") {
                    handleBatchGroupKeyDown(event);
                    return;
                  }
                  handleGroupKeyDown(event, {
                    canSave: !controlsDisabled,
                    onSave: () => saveRow(row, isTrailingCreate),
                    onCancel: () => cancelRow(row),
                  });
                }}
              >
                <div
                  className="inline-table-form__row"
                  role={ROW_ROLE}
                  aria-describedby={detail || row.validation || row.error || row.meta ? messageId : undefined}
                >
                  {hasReorderColumn ? (
                    <div
                      className="inline-table-form__td inline-table-form__td--reorder"
                      role={CELL_ROLE}
                      data-label=""
                    >
                      {canShowReorderHandle ? (
                        <InlineTableReorderControls
                          label={label}
                          canMoveUp={canReorderRow && movableIndex > 0}
                          canMoveDown={canReorderRow && movableIndex < reorderableRows.length - 1}
                          onMoveUp={() => reorderRow(row.id, movableIndex - 1)}
                          onMoveDown={() => reorderRow(row.id, movableIndex + 1)}
                        />
                      ) : null}
                    </div>
                  ) : null}
                  {columns.map((column) => (
                    <div
                      key={column.key}
                      className={cellClasses(column, "inline-table-form__td")}
                      role={CELL_ROLE}
                      data-label={plainLabel(column.mobileLabel ?? column.header)}
                      data-inline-table-row={row.id}
                      data-inline-table-column={column.key}
                      ref={(node) => {
                        if (!node) return;
                        node.onclick = (event) => {
                          if (event.target && isInteractiveEventTarget(event.target)) return;
                          if (activationMode === "singleClick" && !isTrailingCreate) activateCell(column.key);
                        };
                      }}
                      onPointerUp={(event) => {
                        if (isInteractiveEventTarget(event.target)) return;
                        if (activationMode === "singleClick" && !isTrailingCreate) activateCell(column.key);
                      }}
                      onDoubleClick={() => {
                        if (activationMode !== "doubleClick") return;
                        activateCell(column.key);
                      }}
                    >
                      <span className="inline-table-form__mobile-label">
                        {column.mobileLabel ?? column.header}
                      </span>
                      {renderEditing ? column.renderEdit(context) : column.renderRead(context)}
                    </div>
                  ))}
                  <div
                    className="inline-table-form__td inline-table-form__td--actions"
                    role={CELL_ROLE}
                    data-label="State"
                  >
                    <span className="inline-table-form__mobile-label" />
                    <InlineTableActions
                      editing={renderEditing}
                      dirty={Boolean(row.dirty)}
                      disabled={controlsDisabled}
                      saveMode={saveMode}
                      status={status}
                      onEdit={onEdit ? () => {
                        exitAutosaveRowsBeforeEdit(row.id);
                        onEdit(row.id);
                        exitedAutosaveRowsBeforeEditRef.current.clear();
                      } : undefined}
                      onDelete={onDelete && !isTrailingCreate ? () => requestDelete(row.id) : undefined}
                      deleteLabel={deleteActionLabel}
                      onSave={() => saveRow(row, isTrailingCreate)}
                      onCancel={() => cancelRow(row)}
                      actionDisplay={actionDisplay}
                      hideRowCommit={saveMode === "batch"}
                      hideCancel={isFactoryCreate && !row.dirty}
                      onActionPointerDown={(action) => {
                        if (editing || action !== "edit") suppressAutosaveBlur(row.id);
                      }}
                    />
                  </div>
                </div>

                {(detail || row.validation || row.error || row.meta) && (
                  <div
                    id={messageId}
                    className="inline-table-form__detail"
                    role={ROW_ROLE}
                  >
                    <div className="inline-table-form__detail-body" role={CELL_ROLE}>
                      {detail ? (
                        <div
                          className="inline-table-form__detail-content"
                          role={CELL_ROLE}
                          data-inline-table-row={row.id}
                          data-inline-table-column={DETAIL_COLUMN_KEY}
                          ref={(node) => {
                            if (!node) return;
                            node.onclick = (event) => {
                              if (event.target && isInteractiveEventTarget(event.target)) return;
                              if (activationMode === "singleClick" && !isTrailingCreate) activateDetail();
                            };
                          }}
                          onPointerUp={(event) => {
                            if (isInteractiveEventTarget(event.target)) return;
                            if (activationMode === "singleClick" && !isTrailingCreate) activateDetail();
                          }}
                          onDoubleClick={() => {
                            if (activationMode !== "doubleClick") return;
                            activateDetail();
                          }}
                        >
                          {detail}
                        </div>
                      ) : null}
                      {row.validation ? (
                        <p
                          id={validationMessageId}
                          className="inline-table-form__message inline-table-form__message--validation"
                        >
                          {row.validation}
                        </p>
                      ) : null}
                      {row.error ? (
                        <p
                          id={errorMessageId}
                          className="inline-table-form__message inline-table-form__message--error"
                        >
                          {row.error}
                        </p>
                      ) : null}
                      {row.meta ? <div className="inline-table-form__meta">{row.meta}</div> : null}
                    </div>
                  </div>
                )}
              </InlineTableRowGroup>
            );
          })}
        </div>
        {loadMore ? <div className="inline-table-form__load-more-slot">{loadMore}</div> : null}
      </div>
      {addRow ? <div className="inline-table-form__add">{addRow}</div> : null}
      {batchActions ? (
        <div className="inline-table-form__batch-actions">
          {batchActions}
        </div>
      ) : null}
      <ConfirmationModal
        open={Boolean(deleteConfirmationRow)}
        title={deleteConfirmationCopy?.title ?? "Delete this row?"}
        eyebrow={deleteConfirmationCopy?.eyebrow ?? "Confirm delete"}
        confirmLabel={deleteConfirmationCopy?.confirmLabel ?? "Delete row"}
        tone={deleteConfirmationCopy?.tone ?? "rust"}
        onCancel={cancelDelete}
        onConfirm={confirmDelete}
        pending={Boolean(deleteConfirmationRow?.saving)}
      >
        <>
          {deleteConfirmationCopy?.children ?? (
            <p>
              Delete <strong>{deleteConfirmationLabel}</strong>? This removes the row immediately.
            </p>
          )}
          {deleteConfirmationRow?.error ? (
            <div className="inline-table-form__message inline-table-form__message--error">
              {deleteConfirmationRow.error}
            </div>
          ) : null}
        </>
      </ConfirmationModal>
    </section>
  );
}

function InlineTableActions({
  editing,
  dirty,
  disabled,
  saveMode,
  status,
  onEdit,
  onDelete,
  deleteLabel,
  onSave,
  onCancel,
  actionDisplay,
  hideRowCommit,
  hideCancel,
  onActionPointerDown,
}: {
  editing: boolean;
  dirty: boolean;
  disabled: boolean;
  saveMode: InlineTableSaveMode;
  status: InlineTableRowStatus;
  onEdit?: () => void;
  onDelete?: () => void;
  deleteLabel: string;
  onSave: () => void;
  onCancel: () => void;
  actionDisplay: InlineTableActionDisplay;
  hideRowCommit: boolean;
  hideCancel: boolean;
  onActionPointerDown: (action: "edit" | "delete" | "cancel" | "save") => void;
}) {
  if (!editing) {
    return (
      <div className="inline-table-form__actions">
        <InlineTableStatus status={status} />
        {onDelete || onEdit ? (
          <div className="inline-table-form__button-group btn-group btn-group--attached" role={GROUP_ROLE} aria-label="Row actions">
            {onDelete ? (
              <InlineTableActionButton
                action="delete"
                display={actionDisplay}
                label={deleteLabel}
                disabled={disabled}
                onClick={onDelete}
                onPointerDown={() => onActionPointerDown("delete")}
              />
            ) : null}
            {onEdit ? (
              <InlineTableActionButton
                action="edit"
                display={actionDisplay}
                disabled={disabled}
                onClick={onEdit}
                onPointerDown={() => onActionPointerDown("edit")}
              />
            ) : null}
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <div className="inline-table-form__actions">
      <InlineTableStatus status={status} />
      {onDelete || (!hideRowCommit && (!hideCancel || saveMode === "explicit")) ? (
        <div className="inline-table-form__button-group btn-group btn-group--attached" role={GROUP_ROLE} aria-label="Edit row actions">
          {onDelete ? (
            <InlineTableActionButton
              action="delete"
              display={actionDisplay}
              label={deleteLabel}
              disabled={disabled}
              onClick={onDelete}
              onPointerDown={() => onActionPointerDown("delete")}
            />
          ) : null}
          {!hideRowCommit && !hideCancel ? (
            <InlineTableActionButton
              action="cancel"
              display={actionDisplay}
              disabled={disabled}
              onClick={onCancel}
              onPointerDown={() => onActionPointerDown("cancel")}
            />
          ) : null}
          {!hideRowCommit && saveMode === "explicit" ? (
            <InlineTableActionButton
              action="save"
              display={actionDisplay}
              disabled={disabled || !dirty}
              onClick={onSave}
              onPointerDown={() => onActionPointerDown("save")}
            />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function InlineTableSearchToolbar({
  tableLabel,
  search,
}: {
  tableLabel: string;
  search: InlineTableSearchProps;
}) {
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const clearLabel = search.clearLabel ?? "Clear search";
  const clearSearch = () => {
    search.onChange("");
    inputRef.current?.focus();
  };

  return (
    <div className="inline-table-form__toolbar" role={SEARCH_ROLE} aria-label={`${tableLabel} search and filters`}>
      <div className="inline-table-form__search-field">
        <label className="inline-table-form__search-label" htmlFor={inputId}>
          {search.label}
        </label>
        <Search className="inline-table-form__search-icon" size={15} aria-hidden="true" />
        <input
          id={inputId}
          ref={inputRef}
          className="inline-table-form__search-input"
          type="search"
          aria-label={search.label}
          value={search.value}
          placeholder={search.placeholder}
          onChange={(event: ChangeEvent<HTMLInputElement>) => search.onChange(event.target.value)}
        />
        {search.value ? (
          <button
            type="button"
            className="inline-table-form__search-clear"
            aria-label={clearLabel}
            title={clearLabel}
            onClick={clearSearch}
          >
            <X size={14} aria-hidden="true" />
          </button>
        ) : null}
      </div>
      {search.resultSummary !== undefined && search.resultSummary !== null ? (
        <span className="inline-table-form__search-summary" role={STATUS_ROLE} aria-live="polite">
          {search.resultSummary}
        </span>
      ) : null}
      {search.filters ? (
        <div className="inline-table-form__toolbar-filters">
          {search.filters}
        </div>
      ) : null}
    </div>
  );
}

export function InlineTableLoadMore({
  hasMore,
  isInitialLoading = false,
  isFetchingMore = false,
  error,
  loadedCount,
  onLoadMore,
  onRetry,
  loadMoreLabel = "Load more rows",
  loadingLabel = "Loading rows",
  retryLabel = "Retry loading rows",
  allLoadedLabel = "All rows loaded",
}: InlineTableLoadMoreProps) {
  const countLabel = loadedCount === undefined ? null : (
    <span className="inline-table-form__load-more-count">
      {loadedCount} loaded
    </span>
  );

  if (isInitialLoading) {
    return (
      <output className="inline-table-form__load-more" aria-live="polite" aria-busy="true">
        <span className="inline-table-form__load-more-status">
          <Loader2 className="inline-table-form__load-more-spinner" size={15} aria-hidden="true" />
          {loadingLabel}
        </span>
        {countLabel}
      </output>
    );
  }

  if (error) {
    return (
      <div className="inline-table-form__load-more inline-table-form__load-more--error" aria-label="Row loading error">
        <output className="inline-table-form__load-more-status" aria-live="assertive">
          {error}
        </output>
        {countLabel}
        {onRetry ? (
          <button
            type="button"
            className="inline-table-form__load-more-button"
            disabled={isFetchingMore}
            aria-busy={isFetchingMore || undefined}
            onClick={onRetry}
          >
            {isFetchingMore ? (
              <Loader2 className="inline-table-form__load-more-spinner" size={15} aria-hidden="true" />
            ) : (
              <RotateCcw size={15} aria-hidden="true" />
            )}
            {isFetchingMore ? loadingLabel : retryLabel}
          </button>
        ) : null}
      </div>
    );
  }

  if (!hasMore) {
    return (
      <div className="inline-table-form__load-more inline-table-form__load-more--complete">
        <output className="inline-table-form__load-more-status" aria-live="polite">
          {allLoadedLabel}
        </output>
        {countLabel}
      </div>
    );
  }

  return (
    <div className="inline-table-form__load-more" aria-label="Load more table rows" aria-live="polite">
      <button
        type="button"
        className="inline-table-form__load-more-button"
        disabled={isFetchingMore || !onLoadMore}
        aria-busy={isFetchingMore || undefined}
        onClick={onLoadMore}
      >
        {isFetchingMore ? (
          <Loader2 className="inline-table-form__load-more-spinner" size={15} aria-hidden="true" />
        ) : null}
        {isFetchingMore ? loadingLabel : loadMoreLabel}
      </button>
      {countLabel}
    </div>
  );
}

function InlineTableReorderControls({
  label,
  canMoveUp,
  canMoveDown,
  onMoveUp,
  onMoveDown,
}: {
  label: string;
  canMoveUp: boolean;
  canMoveDown: boolean;
  onMoveUp: () => void;
  onMoveDown: () => void;
}) {
  return (
    <span className="inline-table-form__reorder-tools">
      <button
        type="button"
        className="inline-table-form__reorder-btn"
        disabled={!canMoveUp}
        aria-label={`Move ${label} up`}
        title={`Move ${label} up`}
        onClick={onMoveUp}
      >
        <ArrowUp size={13} aria-hidden="true" />
      </button>
      <InlineTableDragHandle label={label} />
      <button
        type="button"
        className="inline-table-form__reorder-btn"
        disabled={!canMoveDown}
        aria-label={`Move ${label} down`}
        title={`Move ${label} down`}
        onClick={onMoveDown}
      >
        <ArrowDown size={13} aria-hidden="true" />
      </button>
    </span>
  );
}

function InlineTableDragHandle({
  label,
}: {
  label: string;
}) {
  return (
    <span
      className="inline-table-form__drag-handle"
      aria-label={`Drag ${label} to reorder`}
      title={`Drag ${label} to reorder`}
    >
      <GripVertical size={15} aria-hidden="true" />
    </span>
  );
}

function InlineTableActionButton({
  action,
  display,
  label,
  disabled,
  onClick,
  onPointerDown,
}: {
  action: "edit" | "delete" | "cancel" | "save";
  display: InlineTableActionDisplay;
  label?: string;
  disabled: boolean;
  onClick: () => void;
  onPointerDown: () => void;
}) {
  const resolvedLabel = label ?? actionLabel(action);
  const Icon = actionIcon(action);
  const classes = [
    "inline-table-form__icon-btn",
    action === "save" ? "inline-table-form__icon-btn--primary" : null,
    action === "delete" ? "inline-table-form__icon-btn--danger" : null,
    display === "icons" ? "inline-table-form__icon-btn--icon-only" : null,
  ].filter(Boolean).join(" ");

  return (
    <button
      type="button"
      className={classes}
      aria-label={display === "icons" ? resolvedLabel : undefined}
      title={display === "icons" ? resolvedLabel : undefined}
      disabled={disabled}
      onPointerDown={onPointerDown}
      onClick={onClick}
    >
      {display === "icons" ? <Icon size={15} aria-hidden="true" /> : resolvedLabel}
    </button>
  );
}

function actionLabel(action: "edit" | "delete" | "cancel" | "save") {
  if (action === "edit") return "Edit";
  if (action === "delete") return "Delete";
  if (action === "cancel") return "Cancel";
  return "Save";
}

function actionIcon(action: "edit" | "delete" | "cancel" | "save") {
  if (action === "edit") return Pencil;
  if (action === "delete") return Trash2;
  if (action === "cancel") return X;
  return Check;
}

function InlineTableStatus({ status }: { status: InlineTableRowStatus }) {
  if (status !== "saving" && status !== "error" && status !== "disabled") return null;
  if (status === "saving") {
    return (
      <output
        className={statusClass(status)}
        aria-label="Saving..."
        title="Saving..."
      >
        <Loader2 className="inline-table-form__status-spinner" size={13} aria-hidden="true" />
        <span className="sr-only">Saving</span>
      </output>
    );
  }
  if (status === "disabled") {
    return (
      <output
        className={statusClass(status)}
        aria-label="Locked"
        title="Locked"
      >
        <Lock className="inline-table-form__status-icon" size={13} aria-hidden="true" />
      </output>
    );
  }
  return <span className={statusClass(status)}>{statusLabel(status)}</span>;
}

export function InlineTextField({
  value,
  onChange,
  placeholder,
  disabled,
  ariaLabel,
  ariaInvalid,
  ariaDescribedBy,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  ariaLabel?: string;
  ariaInvalid?: boolean;
  ariaDescribedBy?: string;
}) {
  return (
    <input
      className="inline-table-form__control"
      type="text"
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      aria-label={ariaLabel}
      aria-invalid={ariaInvalid ? "true" : undefined}
      aria-describedby={ariaDescribedBy}
      onChange={(event) => onChange(event.currentTarget.value)}
    />
  );
}

export function InlineNumberField({
  value,
  onChange,
  min,
  max,
  step,
  placeholder,
  disabled,
  ariaLabel,
  ariaInvalid,
  ariaDescribedBy,
}: {
  value: string;
  onChange: (value: string) => void;
  min?: number | string;
  max?: number | string;
  step?: number | string;
  placeholder?: string;
  disabled?: boolean;
  ariaLabel?: string;
  ariaInvalid?: boolean;
  ariaDescribedBy?: string;
}) {
  return (
    <input
      className="inline-table-form__control"
      type="text"
      inputMode="decimal"
      value={value}
      min={min}
      max={max}
      step={step}
      placeholder={placeholder}
      disabled={disabled}
      aria-label={ariaLabel}
      aria-invalid={ariaInvalid ? "true" : undefined}
      aria-describedby={ariaDescribedBy}
      onChange={(event) => onChange(event.currentTarget.value)}
    />
  );
}

export function InlineDateField({
  value,
  onChange,
  disabled,
  ariaLabel,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  ariaLabel?: string;
}) {
  return (
    <input
      className="inline-table-form__control inline-table-form__control--date"
      type="date"
      value={value}
      disabled={disabled}
      aria-label={ariaLabel}
      onChange={(event) => onChange(event.currentTarget.value)}
    />
  );
}

export function InlineTimeField({
  value,
  onChange,
  disabled,
  ariaLabel,
  min,
  max,
  step,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  ariaLabel?: string;
  min?: string;
  max?: string;
  step?: number | string;
}) {
  return (
    <input
      className="inline-table-form__control inline-table-form__control--time"
      type="time"
      value={value}
      disabled={disabled}
      aria-label={ariaLabel}
      min={min}
      max={max}
      step={step}
      onChange={(event) => onChange(event.currentTarget.value)}
    />
  );
}

export interface InlineSelectOption {
  value: string;
  label: string;
}

export function InlineSelectField({
  value,
  options,
  onChange,
  disabled,
  ariaLabel,
}: {
  value: string;
  options: readonly InlineSelectOption[];
  onChange: (value: string) => void;
  disabled?: boolean;
  ariaLabel?: string;
}) {
  return (
    <select
      className="inline-table-form__control inline-table-form__control--select"
      value={value}
      disabled={disabled}
      aria-label={ariaLabel}
      onChange={(event) => onChange(event.currentTarget.value)}
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

export interface InlineTagPickerOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface InlineTagPickerFieldProps {
  value: readonly string[];
  options: readonly InlineTagPickerOption[];
  onChange: (value: string[]) => void;
  label?: string;
  ariaLabel?: string;
  disabled?: boolean;
  searchable?: boolean;
  allowCustomValues?: boolean;
  inputLabel?: string;
  placeholder?: string;
  noResultsLabel?: string;
  normalizeCustomValue?: (value: string) => string;
}

export function InlineTagPickerField({
  value,
  options,
  onChange,
  label,
  ariaLabel,
  disabled = false,
  searchable = false,
  allowCustomValues = false,
  inputLabel,
  placeholder = "Filter...",
  noResultsLabel = "No matches",
  normalizeCustomValue = defaultNormalizeCustomTagValue,
}: InlineTagPickerFieldProps) {
  const labelId = useId();
  const inputId = useId();
  const [query, setQuery] = useState("");
  const uniqueOptions = uniqueTagPickerOptions([
    ...options,
    ...(allowCustomValues ? value.map((selected) => ({ value: selected, label: selected })) : []),
  ]);
  const visibleOptions = filterTagPickerOptions(uniqueOptions, query);
  const selectedValues = normalizeTagPickerValue(value, uniqueOptions);
  const selected = new Set(selectedValues);
  const resolvedLabel = ariaLabel ?? label ?? "Tag options";

  const applySelected = (nextSelected: ReadonlySet<string>) => {
    onChange(orderedTagPickerValues(uniqueOptions, nextSelected));
  };

  const toggleOption = (option: InlineTagPickerOption) => {
    if (disabled || option.disabled) return;
    const nextSelected = new Set(selectedValues);
    if (nextSelected.has(option.value)) {
      nextSelected.delete(option.value);
    } else {
      nextSelected.add(option.value);
    }
    applySelected(nextSelected);
  };

  const addValue = (rawValue: string) => {
    const nextValue = normalizeCustomValue(rawValue);
    if (disabled || !nextValue) return;
    const matchingOption = uniqueOptions.find((option) => tagPickerOptionMatches(option, nextValue));
    if (matchingOption?.disabled) return;
    if (matchingOption) {
      const nextSelected = new Set(selectedValues);
      nextSelected.add(matchingOption.value);
      applySelected(nextSelected);
    } else {
      onChange([...selectedValues, nextValue]);
    }
    setQuery("");
  };

  const removeLastSelected = () => {
    if (disabled || selectedValues.length === 0) return;
    applySelected(new Set(selectedValues.slice(0, -1)));
  };

  const commitQuery = () => {
    if (!query.trim()) return;
    const firstEnabled = visibleOptions.find((option) => !option.disabled);
    if (firstEnabled) {
      const nextSelected = new Set(selectedValues);
      nextSelected.add(firstEnabled.value);
      applySelected(nextSelected);
      setQuery("");
      return;
    }
    if (allowCustomValues) addValue(query);
  };

  const showInput = searchable || allowCustomValues;
  const inputAccessibleLabel = inputLabel ?? `${resolvedLabel} input`;

  return (
    <div
      className={[
        "inline-table-form__tag-picker",
        disabled ? "inline-table-form__tag-picker--disabled" : null,
      ].filter(Boolean).join(" ")}
      role={GROUP_ROLE}
      aria-label={label ? undefined : resolvedLabel}
      aria-labelledby={label ? labelId : undefined}
      aria-disabled={disabled || undefined}
      data-inline-table-enter-save="false"
    >
      {label ? (
        <span id={labelId} className="inline-table-form__tag-picker-label">
          {label}
        </span>
      ) : null}
      {showInput ? (
        <label className="inline-table-form__tag-input-label" htmlFor={inputId}>
          {inputAccessibleLabel}
        </label>
      ) : null}
      {showInput ? (
        <input
          id={inputId}
          className="inline-table-form__tag-input"
          type="text"
          value={query}
          placeholder={placeholder}
          disabled={disabled}
          aria-label={label || ariaLabel ? inputAccessibleLabel : undefined}
          onChange={(event) => setQuery(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (disabled) return;
            if (event.key === "Enter" || event.key === ",") {
              event.preventDefault();
              event.stopPropagation();
              commitQuery();
            } else if (event.key === "Backspace" && query === "") {
              event.preventDefault();
              event.stopPropagation();
              removeLastSelected();
            }
          }}
        />
      ) : null}
      <div className="inline-table-form__tag-options">
        {visibleOptions.map((option) => {
          const isSelected = selected.has(option.value);
          const optionDisabled = disabled || option.disabled;
          return (
            <button
              key={option.value}
              type="button"
              className={[
                "inline-table-form__tag-option",
                isSelected ? "is-selected" : null,
              ].filter(Boolean).join(" ")}
              aria-pressed={isSelected}
              disabled={optionDisabled}
              onClick={() => toggleOption(option)}
              onKeyDown={(event) => (
                handleTagOptionKeyDown(event, () => toggleOption(option), optionDisabled)
              )}
            >
              {option.label}
            </button>
          );
        })}
        {visibleOptions.length === 0 ? (
          <span className="inline-table-form__tag-empty">
            {allowCustomValues && query.trim()
              ? `Press Enter to add "${query.trim()}"`
              : noResultsLabel}
          </span>
        ) : null}
      </div>
    </div>
  );
}

export interface InlineSearchableSelectBlankOption {
  label: string;
  secondaryText?: string;
  searchText?: string;
}

export interface InlineSearchableSelectFieldProps {
  value: string;
  options: readonly SearchableSelectOption[];
  onChange: (value: string) => void;
  label?: string;
  ariaLabel?: string;
  disabled?: boolean;
  blankOption?: InlineSearchableSelectBlankOption;
  placeholder?: string;
  noResultsLabel?: string;
  renderOptionSecondaryText?: (option: SearchableSelectOption) => ReactNode;
}

export function InlineSearchableSelectField({
  value,
  options,
  onChange,
  label,
  ariaLabel,
  disabled,
  blankOption,
  placeholder,
  noResultsLabel,
  renderOptionSecondaryText,
}: InlineSearchableSelectFieldProps) {
  const resolvedLabel = label ?? ariaLabel ?? "Select option";

  return (
    <div className="inline-table-form__searchable-select">
      <SearchableSelect
        label={resolvedLabel}
        value={value}
        options={options}
        onChange={onChange}
        disabled={disabled}
        blankOption={blankOption}
        placeholder={placeholder}
        noResultsLabel={noResultsLabel}
        renderOptionSecondaryText={renderOptionSecondaryText}
        onInputKeyDown={(event) => {
          if (event.defaultPrevented && isInlineSearchableSelectKey(event.key) && isComboboxEventTarget(event.target)) {
            event.stopPropagation();
          }
        }}
        className="inline-table-form__searchable-select-field"
        inputClassName="inline-table-form__control inline-table-form__control--searchable-select"
      />
    </div>
  );
}

export interface InlineIconFieldProps {
  value: string;
  onChange: (value: string) => void;
  label: string;
  disabled?: boolean;
  allowEmpty?: boolean;
  error?: string;
  errorId?: string;
}

export function InlineIconField({
  value,
  onChange,
  label,
  disabled,
  allowEmpty,
  error,
  errorId,
}: InlineIconFieldProps) {
  return (
    <div
      className="inline-table-form__icon-field"
      data-inline-table-enter-save="false"
    >
      <IconSelector
        label={label}
        value={value}
        onChange={onChange}
        disabled={disabled}
        allowEmpty={allowEmpty}
        error={error}
        errorId={errorId}
        showSelectedLabel={false}
        className="inline-table-form__icon-selector"
      />
    </div>
  );
}

export function InlineCheckboxField({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: ReactNode;
  disabled?: boolean;
}) {
  return (
    <label className="inline-table-form__check">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        aria-label={plainLabel(label)}
        onChange={(event: ChangeEvent<HTMLInputElement>) => onChange(event.currentTarget.checked)}
      />
      <span>{label}</span>
    </label>
  );
}

export function InlineNoteField({
  value,
  onChange,
  placeholder,
  disabled,
  ariaLabel,
  ariaInvalid,
  ariaDescribedBy,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  ariaLabel?: string;
  ariaInvalid?: boolean;
  ariaDescribedBy?: string;
}) {
  return (
    <textarea
      className="inline-table-form__note"
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      aria-label={ariaLabel}
      aria-invalid={ariaInvalid ? "true" : undefined}
      aria-describedby={ariaDescribedBy}
      onChange={(event) => onChange(event.currentTarget.value)}
    />
  );
}

export function InlineNoteDisplay({
  children,
  className,
  preserveLineBreaks = true,
  as: Element = "p",
}: {
  children: ReactNode;
  className?: string;
  preserveLineBreaks?: boolean;
  as?: "p" | "span" | "div";
}) {
  return (
    <Element
      className={[
        "inline-table-form__note-display",
        preserveLineBreaks ? null : "inline-table-form__note-display--collapsed",
        className,
      ].filter(Boolean).join(" ")}
    >
      {children}
    </Element>
  );
}

function rowStatus<TDraft>(row: InlineTableRow<TDraft>): InlineTableRowStatus {
  if (row.disabled) return "disabled";
  if (row.saving) return "saving";
  if (row.error) return "error";
  if (row.dirty) return "dirty";
  return "idle";
}

function makeBatchContext<TDraft>(
  rows: readonly InlineTableRow<TDraft>[],
  onBatchCancel: (() => void) | undefined,
): InlineTableBatchContext<TDraft> {
  const dirtyRows = rows.filter((row) => row.dirty);
  const dirtyDisabledRows = dirtyRows.filter((row) => row.disabled);
  const validationRows = rows.filter((row) => row.validation);
  const errorRows = rows.filter((row) => row.error);
  const savingRows = rows.filter((row) => row.saving);
  const disabledRows = rows.filter((row) => row.disabled);
  const hasValidation = validationRows.length > 0;
  const hasErrors = errorRows.length > 0;
  const isSaving = savingRows.length > 0;

  return {
    rows,
    dirtyRows,
    validationRows,
    errorRows,
    savingRows,
    disabledRows,
    hasDirtyRows: dirtyRows.length > 0,
    hasValidation,
    hasErrors,
    isSaving,
    canSubmit: dirtyRows.length > 0
      && dirtyDisabledRows.length === 0
      && !hasValidation
      && !hasErrors
      && !isSaving,
    canDiscard: Boolean(onBatchCancel) && dirtyRows.length > 0 && !isSaving,
    discard: () => onBatchCancel?.(),
  };
}

function isRowEditing<TDraft>(row: InlineTableRow<TDraft>) {
  return row.editing ?? false;
}

function isRowDisabled<TDraft>(row: InlineTableRow<TDraft>) {
  return Boolean(row.disabled || row.saving || rowStatus(row) === "disabled");
}

function statusClass(status: InlineTableRowStatus) {
  return `inline-table-form__status inline-table-form__status--${status}`;
}

function statusLabel(status: InlineTableRowStatus) {
  if (status === "error") return "Needs review";
  if (status === "disabled") return "Locked";
  return "";
}

function normalizeTagPickerValue(
  value: readonly string[],
  options: readonly InlineTagPickerOption[],
) {
  return orderedTagPickerValues(options, new Set(value));
}

function uniqueTagPickerOptions(options: readonly InlineTagPickerOption[]) {
  const seen = new Set<string>();
  return options.filter((option) => {
    if (seen.has(option.value)) return false;
    seen.add(option.value);
    return true;
  });
}

function orderedTagPickerValues(
  options: readonly InlineTagPickerOption[],
  selected: ReadonlySet<string>,
) {
  const seen = new Set<string>();
  const next: string[] = [];
  for (const option of options) {
    if (!selected.has(option.value) || seen.has(option.value)) continue;
    seen.add(option.value);
    next.push(option.value);
  }
  return next;
}

function filterTagPickerOptions(
  options: readonly InlineTagPickerOption[],
  query: string,
) {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return options;
  return options.filter((option) => (
    option.label.toLocaleLowerCase().includes(needle)
    || option.value.toLocaleLowerCase().includes(needle)
  ));
}

function tagPickerOptionMatches(option: InlineTagPickerOption, value: string) {
  const needle = value.toLocaleLowerCase();
  return option.value.toLocaleLowerCase() === needle
    || option.label.toLocaleLowerCase() === needle;
}

function defaultNormalizeCustomTagValue(value: string) {
  return value.trim();
}

function handleTagOptionKeyDown(
  event: KeyboardEvent<HTMLButtonElement>,
  toggle: () => void,
  disabled?: boolean,
) {
  if (disabled) return;
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  event.stopPropagation();
  toggle();
}

function cellClasses<TDraft>(column: InlineTableColumn<TDraft>, base: string) {
  return [
    base,
    column.className,
    column.align ? `${base}--${column.align}` : null,
  ].filter(Boolean).join(" ");
}

function columnTemplate(width: InlineTableColumnWidth | undefined) {
  if (!width) return "minmax(120px, 1fr)";
  if (typeof width === "number") return `minmax(120px, ${width}fr)`;
  if ("px" in width) return `${width.px}px`;
  const flex = width.flex ?? 1;
  const min = cssSize(width.min ?? 120);
  const max = width.max ? cssSize(width.max) : `${flex}fr`;
  return `minmax(${min}, ${max})`;
}

function cssSize(value: number | string) {
  return typeof value === "number" ? `${value}px` : value;
}

function makeFactoryCreateRow<TDraft>(
  createEmptyDraft: () => TDraft,
  label: string,
): InlineTableRow<TDraft> {
  createRowCounter += 1;
  const draft = createEmptyDraft();
  return {
    id: `inline-create-${Date.now()}-${createRowCounter}`,
    label,
    isNew: true,
    editing: true,
    dirty: false,
    draft,
    committedDraft: draft,
  };
}

function rowLabel<TDraft>(
  row: InlineTableRow<TDraft>,
  index: number,
  getRowLabel?: (row: InlineTableRow<TDraft>, index: number) => string,
) {
  return getRowLabel?.(row, index) ?? row.label ?? firstReadableDraftValue(row.draft) ?? `Row ${index + 1}`;
}

function firstReadableDraftValue<TDraft>(draft: TDraft) {
  if (!draft || typeof draft !== "object") return readableValue(draft);
  let booleanFallback: string | null = null;
  for (const value of Object.values(draft)) {
    const label = readableValue(value);
    if (label) return label;
    if (booleanFallback === null && typeof value === "boolean") {
      booleanFallback = value ? "Yes" : "No";
    }
  }
  return booleanFallback;
}

function readableValue(value: unknown) {
  if (typeof value === "string") return value.trim() || null;
  if (typeof value === "number") return String(value);
  return null;
}

function moveRow<T>(rows: readonly T[], fromIndex: number, toIndex: number): T[] {
  const next = [...rows];
  const [moved] = next.splice(fromIndex, 1);
  if (moved === undefined) return next;
  next.splice(toIndex, 0, moved);
  return next;
}

function plainLabel(value: ReactNode) {
  return typeof value === "string" || typeof value === "number" ? String(value) : undefined;
}

function rowMessageId(tableId: string, rowId: string) {
  return `${tableId}-${rowId}-message`;
}

function rowValidationMessageId(tableId: string, rowId: string) {
  return `${tableId}-${rowId}-validation`;
}

function rowErrorMessageId(tableId: string, rowId: string) {
  return `${tableId}-${rowId}-error`;
}

function focusStayedInside(event: FocusEvent<HTMLElement>) {
  const next = event.relatedTarget;
  return next instanceof Node && event.currentTarget.contains(next);
}

function selectedRowContainsTarget(root: HTMLElement | null, rowId: string, target: EventTarget | null) {
  if (!root || !(target instanceof Node) || !root.contains(target)) return false;
  const element = target instanceof Element ? target : target.parentElement;
  const rowGroup = element?.closest("[data-inline-table-row-group]");
  return rowGroup instanceof HTMLElement
    && root.contains(rowGroup)
    && rowGroup.dataset.inlineTableRowGroup === rowId;
}

function handleReadRowKeyDown(
  event: KeyboardEvent<HTMLElement>,
  options: {
    rowId: string;
    disabled: boolean;
    selected: boolean;
    onEdit?: () => void;
    onDelete?: () => void;
    onArmDelete: () => void;
    onMoveSelection: (direction: "previous" | "next") => void;
    lastDeleteKeyRef: MutableRefObject<{ rowId: string; at: number } | null>;
  },
) {
  if (!options.selected || options.disabled || isInteractiveEventTarget(event.target)) return;
  if (event.key === "ArrowUp" || event.key === "ArrowDown") {
    event.preventDefault();
    options.onMoveSelection(event.key === "ArrowUp" ? "previous" : "next");
    return;
  }
  if (event.key === "e" || event.key === "E" || event.key === "Enter") {
    if (!options.onEdit) return;
    event.preventDefault();
    options.onEdit();
    return;
  }
  if (event.key !== "d" && event.key !== "D") return;
  if (!options.onDelete) return;
  const now = Date.now();
  const previous = options.lastDeleteKeyRef.current;
  if (previous?.rowId === options.rowId && now - previous.at <= DELETE_KEY_WINDOW_MS) {
    event.preventDefault();
    options.lastDeleteKeyRef.current = null;
    options.onDelete();
    return;
  }
  options.lastDeleteKeyRef.current = { rowId: options.rowId, at: now };
  event.preventDefault();
  options.onArmDelete();
}

function handleGroupKeyDown(
  event: KeyboardEvent<HTMLElement>,
  options: { canSave: boolean; onSave: () => void; onCancel: () => void },
) {
  if (event.defaultPrevented) return;
  if (event.key === "Escape") {
    event.preventDefault();
    options.onCancel();
    return;
  }

  if (event.key !== "Enter") return;
  if (!options.canSave) return;
  const target = event.target;
  if (target instanceof HTMLTextAreaElement && event.shiftKey) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (target instanceof HTMLElement && target.closest("[data-inline-table-enter-save='false']")) return;
  event.preventDefault();
  options.onSave();
}

function handleBatchGroupKeyDown(event: KeyboardEvent<HTMLElement>) {
  if (event.key !== "Enter") return;
  const target = event.target;
  if (target instanceof HTMLTextAreaElement && event.shiftKey) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (target instanceof HTMLElement && target.closest("[data-inline-table-enter-save='false']")) return;
  event.preventDefault();
}

function isInteractiveEventTarget(target: EventTarget) {
  return target instanceof HTMLElement && Boolean(
    target.closest("button, input, select, textarea, a, [role='button']"),
  );
}

function isEditableShortcutTarget(target: EventTarget) {
  return target instanceof HTMLElement && Boolean(target.closest("input, select, textarea, [contenteditable='true']"));
}

function isComboboxEventTarget(target: EventTarget) {
  return target instanceof HTMLElement && target.getAttribute("role") === "combobox";
}

function isInlineSearchableSelectKey(key: string) {
  return key === "Enter" || key === "Escape" || key === "ArrowDown" || key === "ArrowUp";
}

function focusCellControl(root: HTMLElement | null, rowId: string, columnKey: string) {
  if (!root) return false;
  const cells = root.querySelectorAll<HTMLElement>("[data-inline-table-row][data-inline-table-column]");
  const cell = Array.from(cells).find((item) =>
    item.dataset.inlineTableRow === rowId && item.dataset.inlineTableColumn === columnKey,
  );
  const control = cell?.querySelector<HTMLElement>(
    "input:not([disabled]), select:not([disabled]), textarea:not([disabled]), button:not([disabled]), [tabindex]:not([tabindex='-1'])",
  );
  control?.focus();
  return Boolean(control);
}

function focusRowGroup(root: HTMLElement | null, rowId: string) {
  if (!root) return false;
  const groups = root.querySelectorAll<HTMLElement>("[data-inline-table-row-group]");
  const group = Array.from(groups).find((item) => item.dataset.inlineTableRowGroup === rowId);
  if (!group || group.getAttribute("tabindex") === null) return false;
  group.focus();
  return true;
}
