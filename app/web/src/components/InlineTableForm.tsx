import {
  type ChangeEvent,
  type CSSProperties,
  type FocusEvent,
  type KeyboardEvent,
  type MutableRefObject,
  type ReactNode,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { Check, Pencil, Trash2, X } from "lucide-react";
import ConfirmationModal from "@/components/ConfirmationModal";
import { EmptyState } from "@/components/common";

export type InlineTableSaveMode = "explicit" | "autosave";
export type InlineTableRowStatus = "idle" | "dirty" | "saving" | "error" | "disabled";
export type InlineTableActionDisplay = "text" | "icons";
export type InlineTableActivationMode = "doubleClick" | "singleClick";
export type InlineTableColumnWidth =
  | number
  | { px: number }
  | { flex?: number; min?: number | string; max?: number | string };

const DELETE_KEY_WINDOW_MS = 650;
let createRowCounter = 0;

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
  update: (patch: Partial<TDraft>) => void;
  save: () => void;
  cancel: () => void;
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
export interface InlineTableFormProps<TDraft> {
  ariaLabel: string;
  columns: readonly InlineTableColumn<TDraft>[];
  rows: readonly InlineTableRow<TDraft>[];
  /** Defaults to autosave; use explicit only when the workflow needs a deliberate commit button. */
  saveMode?: InlineTableSaveMode;
  onDraftChange: (rowId: string, patch: Partial<TDraft>) => void;
  onSave: (rowId: string) => void;
  onCancel: (rowId: string) => void;
  onEdit?: (rowId: string) => void;
  onDelete?: (rowId: string) => void;
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
  /** Optional full-width detail line for notes, validation help, subtasks, or row metadata. */
  renderDetail?: (context: InlineTableCellContext<TDraft>) => ReactNode;
  /** Improves accessible labels and delete confirmation copy when row content has a stable name. */
  getRowLabel?: (row: InlineTableRow<TDraft>, index: number) => string;
  /** Use only for semantic page-level variants; prefer the default class set. */
  className?: string;
  /** Defaults to the standard density; reserve compact for secondary or very high-volume sheets. */
  compact?: boolean;
}

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
  actionDisplay = "icons",
  activationMode = "singleClick",
  addRow,
  createEmptyDraft,
  onCreate,
  validateCreate,
  createRowLabel = "New row",
  trailingCreateRow,
  emptyState,
  renderDetail,
  getRowLabel,
  className,
  compact = false,
}: InlineTableFormProps<TDraft>) {
  const tableId = useId();
  const rootRef = useRef<HTMLElement | null>(null);
  const pendingFocusRef = useRef<{ rowId: string; columnKey: string } | null>(null);
  const pendingRowFocusRef = useRef<string | null>(null);
  const pendingCreatedSelectionRef = useRef<{ previousIds: Set<string>; sourceRowId: string } | null>(null);
  const pendingDeletedSelectionRef = useRef<string | null>(null);
  const lastDeleteKeyRef = useRef<{ rowId: string; at: number } | null>(null);
  const deleteArmTimerRef = useRef<number | null>(null);
  const suppressAutosaveBlurRef = useRef<string | null>(null);
  const [selectedRowId, setSelectedRowId] = useState<string | null>(null);
  const [deleteArmedRowId, setDeleteArmedRowId] = useState<string | null>(null);
  const [deleteConfirmationRowId, setDeleteConfirmationRowId] = useState<string | null>(null);
  const [factoryCreateRow, setFactoryCreateRow] = useState<InlineTableRow<TDraft> | null>(() => (
    createEmptyDraft && onCreate ? makeFactoryCreateRow(createEmptyDraft, createRowLabel) : null
  ));
  const templateColumns = [
    ...columns.map((column) => columnTemplate(column.width)),
    "minmax(112px, max-content)",
  ].join(" ");
  const classes = [
    "inline-table-form",
    `inline-table-form--${saveMode}`,
    compact ? "inline-table-form--compact" : null,
    className,
  ].filter(Boolean).join(" ");
  const activeTrailingCreateRow = trailingCreateRow ?? factoryCreateRow ?? undefined;
  const renderedRows = activeTrailingCreateRow ? [...rows, activeTrailingCreateRow] : rows;

  useEffect(() => {
    if (!createEmptyDraft || !onCreate || trailingCreateRow || factoryCreateRow) return;
    setFactoryCreateRow(makeFactoryCreateRow(createEmptyDraft, createRowLabel));
  }, [createEmptyDraft, onCreate, trailingCreateRow, factoryCreateRow, createRowLabel]);

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

  useEffect(() => {
    const pending = pendingCreatedSelectionRef.current;
    if (!pending) return;
    const createdRow = renderedRows.find((row) => (
      !pending.previousIds.has(row.id) && !isRowEditing(row) && !isRowDisabled(row)
    ));
    if (createdRow) {
      pendingCreatedSelectionRef.current = null;
      selectRow(createdRow.id, true);
      return;
    }
    const sourceRow = renderedRows.find((row) => row.id === pending.sourceRowId);
    if (!sourceRow || sourceRow.validation || sourceRow.error) {
      pendingCreatedSelectionRef.current = null;
    }
  }, [renderedRows]);

  useEffect(() => {
    const rowId = pendingDeletedSelectionRef.current;
    if (!rowId) return;
    const targetRow = renderedRows.find((row) => row.id === rowId && !isRowDisabled(row));
    pendingDeletedSelectionRef.current = null;
    if (targetRow) selectRow(targetRow.id, true);
  }, [renderedRows]);

  useEffect(() => () => {
    if (deleteArmTimerRef.current) window.clearTimeout(deleteArmTimerRef.current);
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

  const focusEditCell = (rowId: string, columnKey: string, options?: { alreadyEditing?: boolean }) => {
    clearDeleteArm();
    pendingFocusRef.current = { rowId, columnKey };
    if (options?.alreadyEditing && focusCellControl(rootRef.current, rowId, columnKey)) {
      pendingFocusRef.current = null;
      return;
    }
    onEdit?.(rowId);
  };

  const editRow = (rowId: string, columnKey: string) => {
    if (!onEdit) return;
    focusEditCell(rowId, columnKey);
  };

  const updateRowDraft = (row: InlineTableRow<TDraft>, patch: Partial<TDraft>) => {
    if (factoryCreateRow?.id === row.id) {
      setFactoryCreateRow((current) => current
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
      exitRow(row, () => onSave(row.id), { selectCreated: row.isNew || isTrailingCreate });
      return;
    }
    const validation = validateCreate?.(row.draft);
    if (validation) {
      setFactoryCreateRow((current) => current ? { ...current, dirty: true, validation } : current);
      return;
    }
    const result = onCreate?.(row.draft);
    if (result === false) return;
    pendingCreatedSelectionRef.current = {
      previousIds: new Set(renderedRows.map((candidate) => candidate.id)),
      sourceRowId: row.id,
    };
    clearDeleteArm();
    setFactoryCreateRow(makeFactoryCreateRow(createEmptyDraft ?? (() => row.draft), createRowLabel));
  };

  const cancelRow = (row: InlineTableRow<TDraft>) => {
    if (factoryCreateRow?.id === row.id) {
      clearDeleteArm();
      setFactoryCreateRow(makeFactoryCreateRow(createEmptyDraft ?? (() => row.draft), createRowLabel));
      return;
    }
    exitRow(row, () => onCancel(row.id));
  };

  const exitRow = (row: InlineTableRow<TDraft>, action: () => void, options?: { selectCreated?: boolean }) => {
    suppressAutosaveBlurRef.current = row.id;
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
    suppressAutosaveBlurRef.current = rowId;
    clearDeleteArm();
    selectRow(rowId);
    setDeleteConfirmationRowId(rowId);
  };

  const cancelDelete = () => {
    suppressAutosaveBlurRef.current = null;
    setDeleteConfirmationRowId(null);
  };

  const confirmDelete = () => {
    const rowId = deleteConfirmationRowId;
    if (!rowId || !onDelete) return;
    suppressAutosaveBlurRef.current = null;
    setDeleteConfirmationRowId(null);
    pendingDeletedSelectionRef.current = deleteSelectionTarget(rowId);
    onDelete(rowId);
  };

  const deleteFromKeyboard = (rowId: string) => {
    if (!onDelete) return;
    clearDeleteArm();
    pendingDeletedSelectionRef.current = deleteSelectionTarget(rowId);
    onDelete(rowId);
  };

  const deleteSelectionTarget = (rowId: string) => {
    const selectableRowIds = selectableRows().map((row) => row.id);
    const currentIndex = selectableRowIds.indexOf(rowId);
    if (currentIndex === -1) return null;
    return selectableRowIds[currentIndex + 1] ?? selectableRowIds[currentIndex - 1] ?? null;
  };

  const selectableRows = () => renderedRows.filter((candidate) => (
    (!isRowEditing(candidate) || candidate.id === activeTrailingCreateRow?.id) && !isRowDisabled(candidate)
  ));

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

  return (
    <section
      ref={rootRef}
      className={classes}
      style={{ "--inline-table-columns": templateColumns } as CSSProperties}
      aria-label={ariaLabel}
    >
      <div className="inline-table-form__table" role="table" aria-label={ariaLabel}>
        <div className="inline-table-form__head" role="rowgroup">
          <div className="inline-table-form__row inline-table-form__row--head" role="row">
            {columns.map((column) => (
              <div
                key={column.key}
                className={cellClasses(column, "inline-table-form__th")}
                role="columnheader"
              >
                {column.header}
              </div>
            ))}
            <div className="inline-table-form__th inline-table-form__th--actions" role="columnheader">
              State
            </div>
          </div>
        </div>

        <div className="inline-table-form__body" role="rowgroup">
          {renderedRows.length === 0 ? (
            <div className="inline-table-form__empty">
              {emptyState ?? (
                <EmptyState
                  variant="compact"
                  title="No rows yet"
                  copy="Rows will appear here when there is something to edit."
                />
              )}
            </div>
          ) : null}

          {renderedRows.map((row, index) => {
            const label = rowLabel(row, index, getRowLabel);
            const isTrailingCreate = activeTrailingCreateRow?.id === row.id;
            const status = rowStatus(row);
            const editing = row.editing ?? false;
            const disabled = row.disabled || row.saving || status === "disabled";
            const messageId = rowMessageId(tableId, row.id);
            const context: InlineTableCellContext<TDraft> = {
              row,
              saveMode,
              disabled,
              update: (patch) => updateRowDraft(row, patch),
              save: () => saveRow(row, isTrailingCreate),
              cancel: () => cancelRow(row),
            };
            const detail = renderDetail?.(context);
            const selected = selectedRowId === row.id && !disabled && (!editing || isTrailingCreate);
            const deleteArmed = deleteArmedRowId === row.id && selected;
            const activateCell = (columnKey: string) => {
              if (editing || disabled || !onEdit) return;
              editRow(row.id, columnKey);
            };

            return (
              <div
                key={row.id}
                tabIndex={(editing && !isTrailingCreate) || disabled ? undefined : 0}
                className={[
                  "inline-table-form__group",
                  row.isNew ? "inline-table-form__group--new" : null,
                  isTrailingCreate ? "inline-table-form__group--trailing-create" : null,
                  editing ? "is-editing" : "is-reading",
                  row.dirty ? "is-dirty" : null,
                  row.saving ? "is-saving" : null,
                  row.error ? "has-error" : null,
                  row.validation ? "has-validation" : null,
                  row.disabled ? "is-disabled" : null,
                  selected ? "is-selected" : null,
                  deleteArmed ? "is-delete-armed" : null,
                ].filter(Boolean).join(" ")}
                data-inline-table-row-group={row.id}
                aria-label={label}
                aria-selected={selected || undefined}
                onClick={(event) => {
                  if (activationMode !== "doubleClick" || editing || disabled) return;
                  if (isInteractiveEventTarget(event.target)) return;
                  selectRow(row.id);
                  event.currentTarget.focus();
                }}
                onBlur={(event) => {
                  if (saveMode !== "autosave" || row.saving || row.disabled) return;
                  if (focusStayedInside(event)) return;
                  if (suppressAutosaveBlurRef.current === row.id) {
                    suppressAutosaveBlurRef.current = null;
                    return;
                  }
                  if (row.dirty) {
                    onSave(row.id);
                    return;
                  }
                  if (editing && !isTrailingCreate) {
                    onCancel(row.id);
                  }
                }}
                onKeyDown={(event) => {
                  if (!editing || (isTrailingCreate && !isEditableShortcutTarget(event.target))) {
                    handleReadRowKeyDown(event, {
                      rowId: row.id,
                      disabled,
                      selected,
                      onEdit: onEdit || isTrailingCreate ? () => {
                        const firstColumn = columns[0];
                        if (firstColumn) focusEditCell(row.id, firstColumn.key, { alreadyEditing: editing });
                      } : undefined,
                      onDelete: onDelete && !isTrailingCreate ? () => deleteFromKeyboard(row.id) : undefined,
                      onArmDelete: () => armDeleteRow(row.id),
                      onMoveSelection: (direction) => moveSelectedRow(row.id, direction),
                      lastDeleteKeyRef,
                    });
                    return;
                  }
                  handleGroupKeyDown(event, {
                    canSave: !disabled,
                    onSave: () => saveRow(row, isTrailingCreate),
                    onCancel: () => cancelRow(row),
                  });
                }}
              >
                <div
                  className="inline-table-form__row"
                  role="row"
                  aria-describedby={detail || row.validation || row.error || row.meta ? messageId : undefined}
                >
                  {columns.map((column) => (
                    <div
                      key={column.key}
                      className={cellClasses(column, "inline-table-form__td")}
                      role="cell"
                      data-label={plainLabel(column.mobileLabel ?? column.header)}
                      data-inline-table-row={row.id}
                      data-inline-table-column={column.key}
                      onClick={() => {
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
                      {editing ? column.renderEdit(context) : column.renderRead(context)}
                    </div>
                  ))}
                  <div
                    className="inline-table-form__td inline-table-form__td--actions"
                    role="cell"
                    data-label="State"
                  >
                    <span className="inline-table-form__mobile-label">State</span>
                    <InlineTableActions
                      editing={editing}
                      dirty={Boolean(row.dirty)}
                      disabled={disabled}
                      saveMode={saveMode}
                      status={status}
                      onEdit={onEdit ? () => onEdit(row.id) : undefined}
                      onDelete={onDelete && !isTrailingCreate ? () => requestDelete(row.id) : undefined}
                      onSave={() => saveRow(row, isTrailingCreate)}
                      onCancel={() => cancelRow(row)}
                      actionDisplay={actionDisplay}
                      hideCancel={isTrailingCreate && !row.dirty}
                      onActionPointerDown={() => {
                        suppressAutosaveBlurRef.current = row.id;
                      }}
                    />
                  </div>
                </div>

                {(detail || row.validation || row.error || row.meta) && (
                  <div
                    id={messageId}
                    className="inline-table-form__detail"
                    role="row"
                  >
                    <div className="inline-table-form__detail-body" role="cell">
                      {detail}
                      {row.validation ? (
                        <p className="inline-table-form__message inline-table-form__message--validation">
                          {row.validation}
                        </p>
                      ) : null}
                      {row.error ? (
                        <p className="inline-table-form__message inline-table-form__message--error">
                          {row.error}
                        </p>
                      ) : null}
                      {row.meta ? <div className="inline-table-form__meta">{row.meta}</div> : null}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
      {addRow ? <div className="inline-table-form__add">{addRow}</div> : null}
      <ConfirmationModal
        open={Boolean(deleteConfirmationRow)}
        title="Delete this row?"
        eyebrow="Confirm delete"
        confirmLabel="Delete row"
        onCancel={cancelDelete}
        onConfirm={confirmDelete}
      >
        <p>
          Delete <strong>{deleteConfirmationLabel}</strong>? This removes the row immediately.
        </p>
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
  onSave,
  onCancel,
  actionDisplay,
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
  onSave: () => void;
  onCancel: () => void;
  actionDisplay: InlineTableActionDisplay;
  hideCancel: boolean;
  onActionPointerDown: () => void;
}) {
  if (!editing) {
    return (
      <div className="inline-table-form__actions">
        <InlineTableStatus status={status} />
        {onDelete ? (
          <InlineTableActionButton
            action="delete"
            display={actionDisplay}
            disabled={disabled}
            onClick={onDelete}
            onPointerDown={onActionPointerDown}
          />
        ) : null}
        {onEdit ? (
          <InlineTableActionButton
            action="edit"
            display={actionDisplay}
            disabled={disabled}
            onClick={onEdit}
            onPointerDown={onActionPointerDown}
          />
        ) : null}
      </div>
    );
  }

  return (
    <div className="inline-table-form__actions">
      <InlineTableStatus status={status} />
      {onDelete ? (
        <InlineTableActionButton
          action="delete"
          display={actionDisplay}
          disabled={disabled}
          onClick={onDelete}
          onPointerDown={onActionPointerDown}
        />
      ) : null}
      {!hideCancel ? (
        <InlineTableActionButton
          action="cancel"
          display={actionDisplay}
          disabled={disabled}
          onClick={onCancel}
          onPointerDown={onActionPointerDown}
        />
      ) : null}
      {saveMode === "explicit" ? (
        <InlineTableActionButton
          action="save"
          display={actionDisplay}
          disabled={disabled || !dirty}
          onClick={onSave}
          onPointerDown={onActionPointerDown}
        />
      ) : null}
    </div>
  );
}

function InlineTableActionButton({
  action,
  display,
  disabled,
  onClick,
  onPointerDown,
}: {
  action: "edit" | "delete" | "cancel" | "save";
  display: InlineTableActionDisplay;
  disabled: boolean;
  onClick: () => void;
  onPointerDown: () => void;
}) {
  const label = actionLabel(action);
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
      aria-label={display === "icons" ? label : undefined}
      title={display === "icons" ? label : undefined}
      disabled={disabled}
      onPointerDown={onPointerDown}
      onClick={onClick}
    >
      {display === "icons" ? <Icon size={15} aria-hidden="true" /> : label}
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
  return <span className={statusClass(status)}>{statusLabel(status)}</span>;
}

export function InlineTextField({
  value,
  onChange,
  placeholder,
  disabled,
  ariaLabel,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  ariaLabel?: string;
}) {
  return (
    <input
      className="inline-table-form__control"
      type="text"
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      aria-label={ariaLabel}
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
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  ariaLabel?: string;
}) {
  return (
    <textarea
      className="inline-table-form__note"
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      aria-label={ariaLabel}
      onChange={(event) => onChange(event.currentTarget.value)}
    />
  );
}

function rowStatus<TDraft>(row: InlineTableRow<TDraft>): InlineTableRowStatus {
  if (row.disabled) return "disabled";
  if (row.saving) return "saving";
  if (row.error) return "error";
  if (row.dirty) return "dirty";
  return "idle";
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
  if (status === "saving") return "Saving";
  if (status === "error") return "Needs review";
  if (status === "disabled") return "Locked";
  return "";
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

function plainLabel(value: ReactNode) {
  return typeof value === "string" || typeof value === "number" ? String(value) : undefined;
}

function rowMessageId(tableId: string, rowId: string) {
  return `${tableId}-${rowId}-message`;
}

function focusStayedInside(event: FocusEvent<HTMLElement>) {
  const next = event.relatedTarget;
  return next instanceof Node && event.currentTarget.contains(next);
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
  if (!options.selected || options.disabled || isEditableShortcutTarget(event.target)) return;
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

function isInteractiveEventTarget(target: EventTarget) {
  return target instanceof HTMLElement && Boolean(
    target.closest("button, input, select, textarea, a, [role='button']"),
  );
}

function isEditableShortcutTarget(target: EventTarget) {
  return target instanceof HTMLElement && Boolean(target.closest("input, select, textarea, [contenteditable='true']"));
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
