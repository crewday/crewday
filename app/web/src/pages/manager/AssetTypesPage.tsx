import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import {
  InlineIconField,
  InlineNumberField,
  InlineSelectField,
  InlineTableForm,
  InlineTextField,
  type InlineTableColumn,
  type InlineTableRow,
} from "@/components/InlineTableForm";
import { AssetIcon } from "@/components/AssetIcon";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { useWorkspace } from "@/context/WorkspaceContext";
import { ApiError, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { fieldErrorId, fieldErrorsByLoc } from "@/lib/apiErrorMessage";
import { clearMapValue, setMapValue } from "@/lib/mapState";
import { usePatchReducer } from "@/lib/usePatchReducer";
import type { AssetCategory, AssetType, DefaultAssetAction, Me, ResolvedPermission } from "@/types/api";

interface ListEnvelope<T> {
  data: T[];
}

interface AssetTypeDraft {
  key: string;
  name: string;
  category: AssetCategory;
  icon_name: string;
  description_md: string;
  default_lifespan_years: string;
  default_actions: DefaultAssetActionDraft[];
}

type AssetTypeField = keyof AssetTypeDraft;
type AssetTypeFieldErrors = Partial<Record<AssetTypeField, string>>;
type DefaultAssetActionKind = DefaultAssetAction["kind"];

interface DefaultAssetActionDraft {
  draft_id: string;
  kind: DefaultAssetActionKind;
  label: string;
  interval_days: string;
  warn_before_days: string;
}

interface AssetTypeMutationVariables {
  rowId: string;
  draft: AssetTypeDraft;
  includeDefaultActions: boolean;
}

interface AssetTypeWriteRequest {
  key: string;
  name: string;
  category: AssetCategory;
  icon_name: string | null;
  description_md: string | null;
  default_lifespan_years: number | null;
  default_actions?: DefaultAssetAction[];
}

interface AssetTypeCatalogState {
  editedDrafts: ReadonlyMap<string, AssetTypeDraft>;
  rowErrors: ReadonlyMap<string, string>;
  rowFieldErrors: ReadonlyMap<string, AssetTypeFieldErrors>;
  createDraft: AssetTypeDraft;
  createDirty: boolean;
  createError: string | null;
  createFieldErrors: AssetTypeFieldErrors;
}

const CREATE_ASSET_TYPE_ROW_ID = "__new_asset_type__";
const ASSET_TYPE_CATEGORIES: readonly { value: AssetCategory; label: string }[] = [
  { value: "climate", label: "Climate" },
  { value: "appliance", label: "Appliance" },
  { value: "plumbing", label: "Plumbing" },
  { value: "pool", label: "Pool" },
  { value: "heating", label: "Heating" },
  { value: "outdoor", label: "Outdoor" },
  { value: "safety", label: "Safety" },
  { value: "security", label: "Security" },
  { value: "vehicle", label: "Vehicle" },
  { value: "other", label: "Other" },
];
const DEFAULT_ACTION_KINDS: readonly { value: DefaultAssetActionKind; label: string }[] = [
  { value: "service", label: "Service" },
  { value: "repair", label: "Repair" },
  { value: "replace", label: "Replace" },
  { value: "inspect", label: "Inspect" },
  { value: "read", label: "Read" },
];
const EMPTY_ASSET_TYPE_DRAFT: AssetTypeDraft = {
  key: "",
  name: "",
  category: "other",
  icon_name: "",
  description_md: "",
  default_lifespan_years: "",
  default_actions: [],
};
let defaultActionDraftCounter = 0;

function unwrapList<T>(payload: T[] | ListEnvelope<T>): T[] {
  return Array.isArray(payload) ? payload : payload.data;
}

async function fetchList<T>(path: string): Promise<T[]> {
  return unwrapList(await fetchJson<T[] | ListEnvelope<T>>(path));
}

async function fetchManageTypesPermission(activeWsId: string): Promise<ResolvedPermission> {
  const params = new URLSearchParams({
    action_key: "assets.manage_types",
    scope_kind: "workspace",
    scope_id: activeWsId,
  });
  return fetchJson<ResolvedPermission>("/api/v1/permissions/resolved/self?" + params);
}

interface DefaultActionKeyInput {
  kind: DefaultAssetActionKind;
  label: string;
  interval_days: string | number;
  warn_before_days: string | number;
}

function defaultActionKeyBase(action: DefaultActionKeyInput): string {
  return [
    action.kind,
    action.label,
    action.interval_days,
    action.warn_before_days,
  ].join(":");
}

function defaultActionKey(
  action: DefaultActionKeyInput,
  index: number,
  actions: readonly DefaultActionKeyInput[],
): string {
  const base = defaultActionKeyBase(action);
  const firstMatchingIndex = actions.findIndex(
    (candidate) => defaultActionKeyBase(candidate) === base,
  );
  return firstMatchingIndex === index ? base : `${base}:${index}`;
}

// react-doctor-disable-next-line react-doctor/no-giant-component -- Route-level inline catalog editor mirrors existing promoted InlineTableForm management pages; splitting is deferred until a shared catalog abstraction emerges.
export default function AssetTypesPage() {
  // code-health: ignore[ccn nloc] Asset type catalog CRUD keeps row state, permission state, and column renderers adjacent to match existing InlineTableForm catalog pages.
  const queryClient = useQueryClient();
  const { workspaceId } = useWorkspace();
  const [catalogState, setCatalogState] = usePatchReducer<AssetTypeCatalogState>(() => ({
    editedDrafts: new Map(),
    rowErrors: new Map(),
    rowFieldErrors: new Map(),
    createDraft: EMPTY_ASSET_TYPE_DRAFT,
    createDirty: false,
    createError: null,
    createFieldErrors: {},
  }));
  const {
    editedDrafts,
    rowErrors,
    rowFieldErrors,
    createDraft,
    createDirty,
    createError,
    createFieldErrors,
  } = catalogState;

  const typesQ = useQuery({
    queryKey: qk.assetTypes(),
    queryFn: () => fetchList<AssetType>("/api/v1/asset_types"),
  });
  const meQ = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });
  const activeWsId = meQ.data?.current_workspace_id ?? workspaceId;
  const currentUserId = meQ.data?.user_id ?? null;
  const managePermissionQ = useQuery({
    queryKey:
      currentUserId && activeWsId
        ? qk.permissionResolved(currentUserId, "assets.manage_types", "workspace", activeWsId)
        : ["permission", "unresolved", "assets.manage_types", "workspace"],
    enabled: Boolean(currentUserId && activeWsId),
    queryFn: () => fetchManageTypesPermission(activeWsId ?? ""),
    retry: false,
  });
  const canManageTypes = managePermissionQ.data?.effect === "allow";
  const permissionPending = meQ.isPending || (Boolean(currentUserId && activeWsId) && managePermissionQ.isPending);

  const types = useMemo(() => typesQ.data ?? [], [typesQ.data]);
  const typesById = useMemo(() => new Map(types.map((type) => [type.id, type])), [types]);

  const invalidateAssetTypeConsumers = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: qk.assetTypes() }),
      queryClient.invalidateQueries({ queryKey: qk.assets() }),
    ]);
  };

  const saveType = useMutation({
    mutationFn: ({ rowId, draft, includeDefaultActions }: AssetTypeMutationVariables) => {
      const payload = assetTypeWritePayload(draft, { includeDefaultActions });
      if (rowId === CREATE_ASSET_TYPE_ROW_ID) {
        return fetchJson<AssetType>("/api/v1/asset_types", {
          method: "POST",
          body: payload,
        });
      }
      return fetchJson<AssetType>("/api/v1/asset_types/" + encodeURIComponent(rowId), {
        method: "PATCH",
        body: payload,
      });
    },
    onMutate: ({ rowId }) => {
      if (rowId === CREATE_ASSET_TYPE_ROW_ID) {
        setCatalogState({ createError: null });
        return;
      }
      setCatalogState((current) => ({
        ...current,
        rowErrors: clearMapValue(current.rowErrors, rowId),
      }));
    },
    onSuccess: async (saved, variables) => {
      queryClient.setQueryData<AssetType[] | ListEnvelope<AssetType>>(qk.assetTypes(), (current) =>
        upsertAssetTypeInList(current, saved),
      );
      if (variables.rowId === CREATE_ASSET_TYPE_ROW_ID) {
        resetCreateRow();
      } else {
        setCatalogState((current) => ({
        ...current,
          editedDrafts: clearMapValue(current.editedDrafts, variables.rowId),
          rowFieldErrors: clearMapValue(current.rowFieldErrors, variables.rowId),
          rowErrors: clearMapValue(current.rowErrors, variables.rowId),
        }));
      }
      await invalidateAssetTypeConsumers();
    },
    onError: (error, variables) => {
      const fieldErrors = assetTypeFieldErrors(error);
      const message = assetTypeErrorMessage(error, fieldErrors);
      if (variables.rowId === CREATE_ASSET_TYPE_ROW_ID) {
        setCatalogState({
          createFieldErrors: fieldErrors,
          createError: message,
        });
        return;
      }
      setCatalogState((current) => ({
        ...current,
        rowFieldErrors: setMapValue(current.rowFieldErrors, variables.rowId, fieldErrors),
        rowErrors: setMapValue(current.rowErrors, variables.rowId, message),
      }));
    },
  });

  const deleteType = useMutation({
    mutationFn: (typeId: string) =>
      fetchJson<null>("/api/v1/asset_types/" + encodeURIComponent(typeId), { method: "DELETE" }),
    onMutate: (typeId) => {
      setCatalogState((current) => ({
        ...current,
        rowErrors: clearMapValue(current.rowErrors, typeId),
      }));
    },
    onSuccess: async (_deleted, typeId) => {
      queryClient.setQueryData<AssetType[] | ListEnvelope<AssetType>>(qk.assetTypes(), (current) =>
        removeAssetTypeFromList(current, typeId),
      );
      setCatalogState((current) => ({
        ...current,
        editedDrafts: clearMapValue(current.editedDrafts, typeId),
        rowFieldErrors: clearMapValue(current.rowFieldErrors, typeId),
        rowErrors: clearMapValue(current.rowErrors, typeId),
      }));
      await invalidateAssetTypeConsumers();
    },
    onError: (error, typeId) => {
      setCatalogState((current) => ({
        ...current,
        rowErrors: setMapValue(current.rowErrors, typeId, assetTypeErrorMessage(error, {})),
      }));
    },
  });

  const busy = saveType.isPending || deleteType.isPending;
  const rows = useMemo(
    () => types.map((type): InlineTableRow<AssetTypeDraft> => {
      const draft = editedDrafts.get(type.id);
      const savingThisRow =
        (saveType.isPending && saveType.variables?.rowId === type.id) ||
        (deleteType.isPending && deleteType.variables === type.id);
      const locked = !canManageTypes || isSystemAssetType(type);
      return {
        id: type.id,
        label: type.name,
        draft: draft ?? draftFromAssetType(type),
        committedDraft: draftFromAssetType(type),
        editing: draft !== undefined && !locked,
        dirty: draft !== undefined,
        saving: savingThisRow,
        disabled: locked || (busy && !savingThisRow),
        error: rowErrors.get(type.id) ? <span role="alert">{rowErrors.get(type.id)}</span> : undefined,
        meta: isSystemAssetType(type) ? (
          <span className="muted">System type. Duplicate it as a new workspace type to customize it.</span>
        ) : undefined,
      };
    }),
    [busy, canManageTypes, deleteType.isPending, deleteType.variables, editedDrafts, rowErrors, saveType.isPending, saveType.variables, types],
  );
  const trailingCreateRow: InlineTableRow<AssetTypeDraft> | undefined = canManageTypes ? {
    id: CREATE_ASSET_TYPE_ROW_ID,
    label: "New asset type",
    draft: createDraft,
    editing: true,
    dirty: createDirty,
    isNew: true,
    saving: saveType.isPending && saveType.variables?.rowId === CREATE_ASSET_TYPE_ROW_ID,
    disabled: busy && saveType.variables?.rowId !== CREATE_ASSET_TYPE_ROW_ID,
    error: createError ? <span role="alert">{createError}</span> : undefined,
  } : undefined;
  const columns = useMemo(
    (): InlineTableColumn<AssetTypeDraft>[] => [
      {
        key: "icon",
        header: "Icon",
        width: { px: 112 },
        renderRead: ({ row }) => <AssetIcon name={row.draft.icon_name} size={18} />,
        renderEdit: ({ row, update, disabled }) => {
          const fieldErrors = fieldErrorsForAssetTypeRow(row.id, rowFieldErrors, createFieldErrors);
          return (
            <InlineIconField
              label="Icon"
              value={row.draft.icon_name}
              onChange={(icon_name) => update({ icon_name })}
              disabled={disabled}
              allowEmpty
              error={fieldErrors.icon_name}
              errorId={assetTypeFieldErrorId(row.id, "icon_name")}
            />
          );
        },
      },
      {
        key: "name",
        header: "Name",
        width: { flex: 1.2, min: 180 },
        renderRead: ({ row }) => <strong>{row.draft.name}</strong>,
        renderEdit: ({ row, update, disabled }) => {
          const fieldErrors = fieldErrorsForAssetTypeRow(row.id, rowFieldErrors, createFieldErrors);
          return (
            <span className="inline-table-form__field">
              <InlineTextField
                value={row.draft.name}
                onChange={(name) => update({ name })}
                disabled={disabled}
                ariaLabel="Name"
                placeholder="e.g. Pool heater"
                ariaInvalid={Boolean(fieldErrors.name)}
                ariaDescribedBy={fieldErrors.name ? assetTypeFieldErrorId(row.id, "name") : undefined}
              />
              {fieldErrors.name ? (
                <span id={assetTypeFieldErrorId(row.id, "name")} className="form-field-error">
                  {fieldErrors.name}
                </span>
              ) : null}
            </span>
          );
        },
      },
      {
        key: "key",
        header: "Key",
        width: { flex: 1, min: 150 },
        className: "mono",
        renderRead: ({ row }) => <span>{row.draft.key}</span>,
        renderEdit: ({ row, update, disabled }) => {
          const fieldErrors = fieldErrorsForAssetTypeRow(row.id, rowFieldErrors, createFieldErrors);
          return (
            <span className="inline-table-form__field">
              <InlineTextField
                value={row.draft.key}
                onChange={(key) => update({ key })}
                disabled={disabled}
                ariaLabel="Key"
                placeholder="e.g. pool_heater"
                ariaInvalid={Boolean(fieldErrors.key)}
                ariaDescribedBy={fieldErrors.key ? assetTypeFieldErrorId(row.id, "key") : undefined}
              />
              {fieldErrors.key ? (
                <span id={assetTypeFieldErrorId(row.id, "key")} className="form-field-error">
                  {fieldErrors.key}
                </span>
              ) : null}
            </span>
          );
        },
      },
      {
        key: "category",
        header: "Category",
        width: { flex: 0.9, min: 140 },
        renderRead: ({ row }) => <Chip tone="ghost" size="sm">{categoryLabel(row.draft.category)}</Chip>,
        renderEdit: ({ row, update, disabled }) => (
          <InlineSelectField
            value={row.draft.category}
            options={ASSET_TYPE_CATEGORIES}
            onChange={(category) => update({ category: category as AssetCategory })}
            disabled={disabled}
            ariaLabel="Category"
          />
        ),
      },
      {
        key: "lifespan",
        header: "Lifespan",
        width: { flex: 0.8, min: 120 },
        renderRead: ({ row }) => (
          <span className={row.draft.default_lifespan_years ? "" : "muted"}>
            {row.draft.default_lifespan_years ? `${row.draft.default_lifespan_years} years` : "None"}
          </span>
        ),
        renderEdit: ({ row, update, disabled }) => {
          const fieldErrors = fieldErrorsForAssetTypeRow(row.id, rowFieldErrors, createFieldErrors);
          return (
            <span className="inline-table-form__field">
              <InlineNumberField
                value={row.draft.default_lifespan_years}
                onChange={(default_lifespan_years) => update({ default_lifespan_years })}
                disabled={disabled}
                min={1}
                step={1}
                ariaLabel="Default lifespan years"
                placeholder="Years"
              />
              {fieldErrors.default_lifespan_years ? (
                <span id={assetTypeFieldErrorId(row.id, "default_lifespan_years")} className="form-field-error">
                  {fieldErrors.default_lifespan_years}
                </span>
              ) : null}
            </span>
          );
        },
      },
      {
        key: "description",
        header: "Description",
        width: { flex: 1.8, min: 220 },
        renderRead: ({ row }) => (
          <span className={row.draft.description_md ? "" : "muted"}>
            {row.draft.description_md || "No description"}
          </span>
        ),
        renderEdit: ({ row, update, disabled }) => {
          const fieldErrors = fieldErrorsForAssetTypeRow(row.id, rowFieldErrors, createFieldErrors);
          return (
            <span className="inline-table-form__field">
              <InlineTextField
                value={row.draft.description_md}
                onChange={(description_md) => update({ description_md })}
                disabled={disabled}
                ariaLabel="Description"
                placeholder="What this asset type covers."
                ariaInvalid={Boolean(fieldErrors.description_md)}
                ariaDescribedBy={
                  fieldErrors.description_md
                    ? assetTypeFieldErrorId(row.id, "description_md")
                    : undefined
                }
              />
              {fieldErrors.description_md ? (
                <span id={assetTypeFieldErrorId(row.id, "description_md")} className="form-field-error">
                  {fieldErrors.description_md}
                </span>
              ) : null}
            </span>
          );
        },
      },
      {
        key: "actions",
        header: "Default actions",
        width: { flex: 1.8, min: 340 },
        renderRead: ({ row }) => defaultActionsSummary(row.draft.default_actions),
        renderEdit: ({ row, update, disabled }) => {
          const fieldErrors = fieldErrorsForAssetTypeRow(row.id, rowFieldErrors, createFieldErrors);
          return (
            <DefaultActionsEditor
              actions={row.draft.default_actions}
              disabled={disabled}
              error={fieldErrors.default_actions}
              errorId={assetTypeFieldErrorId(row.id, "default_actions")}
              onChange={(default_actions) => update({ default_actions })}
            />
          );
        },
      },
    ],
    [createFieldErrors, rowFieldErrors],
  );

  function resetCreateRow(): void {
    setCatalogState({
      createDraft: EMPTY_ASSET_TYPE_DRAFT,
      createDirty: false,
      createError: null,
      createFieldErrors: {},
    });
  }

  function saveRow(rowId: string): void {
    const draft = rowId === CREATE_ASSET_TYPE_ROW_ID ? createDraft : editedDrafts.get(rowId);
    if (!draft) return;
    const nextErrors = validateAssetTypeDraft(draft);
    if (Object.keys(nextErrors).length > 0) {
      if (rowId === CREATE_ASSET_TYPE_ROW_ID) {
        setCatalogState({
          createFieldErrors: nextErrors,
          createError: "Fix the highlighted fields before saving.",
        });
        return;
      }
      setCatalogState((current) => ({
        ...current,
        rowFieldErrors: setMapValue(current.rowFieldErrors, rowId, nextErrors),
        rowErrors: setMapValue(current.rowErrors, rowId, "Fix the highlighted fields before saving."),
      }));
      return;
    }
    if (rowId === CREATE_ASSET_TYPE_ROW_ID) {
      setCatalogState({ createFieldErrors: {}, createError: null });
    } else {
      setCatalogState((current) => ({
        ...current,
        rowFieldErrors: clearMapValue(current.rowFieldErrors, rowId),
        rowErrors: clearMapValue(current.rowErrors, rowId),
      }));
    }
    const includeDefaultActions = rowId === CREATE_ASSET_TYPE_ROW_ID
      ? true
      : defaultActionDraftsChanged(draft.default_actions, typesById.get(rowId)?.default_actions ?? []);
    saveType.mutate({ rowId, draft, includeDefaultActions });
  }

  function cancelRow(rowId: string): void {
    if (rowId === CREATE_ASSET_TYPE_ROW_ID) {
      resetCreateRow();
      return;
    }
    setCatalogState((current) => ({
        ...current,
      editedDrafts: clearMapValue(current.editedDrafts, rowId),
      rowFieldErrors: clearMapValue(current.rowFieldErrors, rowId),
      rowErrors: clearMapValue(current.rowErrors, rowId),
    }));
  }

  function editRow(rowId: string): void {
    if (!canManageTypes) return;
    const type = typesById.get(rowId);
    if (!type || isSystemAssetType(type)) return;
    setCatalogState((current) => ({
      ...current,
      editedDrafts: setMapValue(current.editedDrafts, rowId, draftFromAssetType(type)),
      rowFieldErrors: clearMapValue(current.rowFieldErrors, rowId),
      rowErrors: clearMapValue(current.rowErrors, rowId),
    }));
    saveType.reset();
  }

  const sub = "Manage workspace-custom equipment categories and review system defaults.";

  if (typesQ.isPending) {
    return <DeskPage title="Asset type catalog" sub={sub}><Loading /></DeskPage>;
  }
  if (!typesQ.data) {
    return <DeskPage title="Asset type catalog" sub={sub}>Failed to load.</DeskPage>;
  }

  return (
    <DeskPage title="Asset type catalog" sub={sub}>
      <section className="panel asset-type-catalog" aria-labelledby="asset-type-catalog-title">
        <header className="panel__head">
          <div>
            <h2 id="asset-type-catalog-title">Asset types</h2>
            <p className="muted">
              System rows are locked. Workspace-custom rows can be created, edited, or archived by owners and managers.
            </p>
          </div>
        </header>
        {!canManageTypes && !permissionPending ? (
          <p className="muted">
            You can view this catalog, but you do not have permission to manage asset types.
          </p>
        ) : null}
        <InlineTableForm
          compact
          ariaLabel="Asset type catalog"
          className="asset-type-catalog__table"
          columns={columns}
          rows={rows}
          saveMode="explicit"
          onDraftChange={(rowId, patch) => {
            if (rowId === CREATE_ASSET_TYPE_ROW_ID) {
              setCatalogState((current) => ({
                ...current,
                createDraft: { ...current.createDraft, ...patch },
                createDirty: true,
                createFieldErrors: clearPatchedAssetTypeFieldErrors(current.createFieldErrors, patch),
                createError: null,
              }));
              return;
            }
            setCatalogState((current) => {
              const type = typesById.get(rowId);
              if (!type || isSystemAssetType(type)) return current;
              const draft = current.editedDrafts.get(rowId) ?? draftFromAssetType(type);
              const nextFieldErrors = clearPatchedAssetTypeFieldErrors(current.rowFieldErrors.get(rowId) ?? {}, patch);
              return {
                ...current,
                editedDrafts: setMapValue(current.editedDrafts, rowId, { ...draft, ...patch }),
                rowFieldErrors: Object.keys(nextFieldErrors).length > 0
                  ? setMapValue(current.rowFieldErrors, rowId, nextFieldErrors)
                  : clearMapValue(current.rowFieldErrors, rowId),
                rowErrors: clearMapValue(current.rowErrors, rowId),
              };
            });
          }}
          onEdit={canManageTypes ? editRow : undefined}
          onSave={saveRow}
          onCancel={cancelRow}
          onDelete={canManageTypes ? (rowId) => deleteType.mutate(rowId) : undefined}
          deleteActionLabel="Archive"
          trailingCreateRow={trailingCreateRow}
          getRowLabel={(row) => row.draft.name || row.label || "New asset type"}
          renderDeleteConfirmation={({ label }) => ({
            title: "Archive asset type?",
            confirmLabel: "Archive type",
            children: (
              <p>
                Archive <strong>{label}</strong>? Unused custom types may disappear immediately; referenced
                types are retained for existing assets and removed from future selection lists.
              </p>
            ),
          })}
        />
      </section>
    </DeskPage>
  );
}

function draftFromAssetType(type: AssetType): AssetTypeDraft {
  return {
    key: type.key,
    name: type.name,
    category: type.category,
    icon_name: type.icon_name ?? "",
    description_md: type.description_md ?? "",
    default_lifespan_years: type.default_lifespan_years == null ? "" : String(type.default_lifespan_years),
    default_actions: (type.default_actions ?? type.default_actions_json ?? []).map(defaultActionDraftFromAction),
  };
}

function assetTypeWritePayload(
  draft: AssetTypeDraft,
  { includeDefaultActions }: { includeDefaultActions: boolean },
): AssetTypeWriteRequest {
  const lifespan = draft.default_lifespan_years.trim();
  const payload: AssetTypeWriteRequest = {
    key: draft.key.trim(),
    name: draft.name.trim(),
    category: draft.category,
    icon_name: draft.icon_name.trim() || null,
    description_md: draft.description_md.trim() || null,
    default_lifespan_years: lifespan ? Number(lifespan) : null,
  };
  if (includeDefaultActions) {
    payload.default_actions = draft.default_actions.map(defaultActionFromDraft);
  }
  return payload;
}

function validateAssetTypeDraft(draft: AssetTypeDraft): AssetTypeFieldErrors {
  const errors: AssetTypeFieldErrors = {};
  if (!draft.name.trim()) errors.name = "Enter an asset type name.";
  if (!draft.key.trim()) errors.key = "Enter an asset type key.";
  if (!ASSET_TYPE_CATEGORIES.some((option) => option.value === draft.category)) {
    errors.category = "Choose a category.";
  }
  const lifespan = draft.default_lifespan_years.trim();
  if (lifespan && (!Number.isInteger(Number(lifespan)) || Number(lifespan) < 1)) {
    errors.default_lifespan_years = "Enter a whole number of years.";
  }
  const defaultActionError = validateDefaultActionDrafts(draft.default_actions);
  if (defaultActionError) errors.default_actions = defaultActionError;
  return errors;
}

function fieldErrorsForAssetTypeRow(
  rowId: string,
  rowFieldErrors: ReadonlyMap<string, AssetTypeFieldErrors>,
  createFieldErrors: AssetTypeFieldErrors,
): AssetTypeFieldErrors {
  return rowId === CREATE_ASSET_TYPE_ROW_ID ? createFieldErrors : rowFieldErrors.get(rowId) ?? {};
}

function clearPatchedAssetTypeFieldErrors(
  fieldErrors: AssetTypeFieldErrors,
  patch: Partial<AssetTypeDraft>,
): AssetTypeFieldErrors {
  const next = { ...fieldErrors };
  for (const field of Object.keys(patch) as AssetTypeField[]) {
    delete next[field];
  }
  return next;
}

function assetTypeFieldErrorId(rowId: string, field: AssetTypeField): string {
  return fieldErrorId("asset-type", rowId, field);
}

function assetTypeFieldErrors(error: unknown): AssetTypeFieldErrors {
  return fieldErrorsByLoc(error, assetTypeFieldFromLoc);
}

function assetTypeFieldFromLoc(loc: readonly (string | number)[] | undefined): AssetTypeField | null {
  const field = loc?.at(-1);
  if (
    field === "key" ||
    field === "name" ||
    field === "category" ||
    field === "icon_name" ||
    field === "description_md" ||
    field === "default_lifespan_years" ||
    field === "default_actions"
  ) {
    return field;
  }
  if (loc?.includes("default_actions")) return "default_actions";
  return null;
}

function assetTypeErrorMessage(error: unknown, fieldErrors: AssetTypeFieldErrors): string {
  const fieldMessages = Object.values(fieldErrors).filter((message): message is string => Boolean(message));
  if (fieldMessages.length > 0) {
    return "Could not save asset type. " + fieldMessages.join(" ");
  }
  if (error instanceof ApiError) {
    if (assetTypeProblemKey(error) === "asset_type_key_conflict") {
      return "That asset type key is already used. Choose a unique key.";
    }
    if (assetTypeProblemKey(error) === "asset_type_read_only") {
      return "System asset types are read-only.";
    }
    if (error.status === 403) return "You do not have permission to manage asset types.";
    if (error.status === 401) return "Sign in again before managing asset types.";
    return error.detail ?? error.title ?? "Could not save asset type. Check the fields and try again.";
  }
  if (error instanceof Error && error.message) return error.message;
  return "Could not save asset type. Try again in a moment.";
}

function assetTypeProblemKey(error: ApiError): string | null {
  return error.machineCode ?? error.type;
}

function isSystemAssetType(type: AssetType): boolean {
  return type.is_system || type.workspace_id === null;
}

function upsertAssetTypeInList(
  current: AssetType[] | ListEnvelope<AssetType> | undefined,
  saved: AssetType,
): AssetType[] | ListEnvelope<AssetType> | undefined {
  if (!current) return current;
  const rows = unwrapList(current);
  const existingIndex = rows.findIndex((row) => row.id === saved.id);
  const nextRows = existingIndex >= 0
    ? rows.map((row) => (row.id === saved.id ? saved : row))
    : [...rows, saved];
  return Array.isArray(current) ? nextRows : { ...current, data: nextRows };
}

function removeAssetTypeFromList(
  current: AssetType[] | ListEnvelope<AssetType> | undefined,
  typeId: string,
): AssetType[] | ListEnvelope<AssetType> | undefined {
  if (!current) return current;
  const nextRows = unwrapList(current).filter((row) => row.id !== typeId);
  return Array.isArray(current) ? nextRows : { ...current, data: nextRows };
}

function categoryLabel(category: AssetCategory): string {
  return ASSET_TYPE_CATEGORIES.find((option) => option.value === category)?.label ?? category;
}

function defaultActionsSummary(actions: readonly DefaultAssetActionDraft[]) {
  if (actions.length === 0) return <span className="muted">No default actions</span>;
  return (
    <ul className="inline-table-form__list">
      {actions.map((action, index) => (
        <li key={defaultActionKey(action, index, actions)}>
          <span>{action.label}</span>
          {action.interval_days != null ? (
            <span className="muted"> every {action.interval_days}d</span>
          ) : null}
          {action.warn_before_days ? (
            <span className="muted">, warn {action.warn_before_days}d ahead</span>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function DefaultActionsEditor({
  actions,
  disabled,
  error,
  errorId,
  onChange,
}: {
  actions: readonly DefaultAssetActionDraft[];
  disabled: boolean;
  error?: string;
  errorId: string;
  onChange: (actions: DefaultAssetActionDraft[]) => void;
}) {
  const updateAction = (index: number, patch: Partial<DefaultAssetActionDraft>) => {
    onChange(actions.map((action, candidateIndex) => (
      candidateIndex === index ? { ...action, ...patch } : action
    )));
  };
  const removeAction = (index: number) => {
    onChange(actions.filter((_action, candidateIndex) => candidateIndex !== index));
  };
  return (
    <div className="asset-default-actions">
      {actions.length === 0 ? (
        <p className="asset-default-actions__empty muted">No default actions</p>
      ) : null}
      {actions.map((action, index) => (
        <div className="asset-default-actions__row" key={action.draft_id}>
          <label className="asset-default-actions__field">
            <span>Kind</span>
            <select
              aria-label={`Default action ${index + 1} kind`}
              value={action.kind}
              disabled={disabled}
              onChange={(event) => updateAction(index, { kind: event.target.value as DefaultAssetActionKind })}
            >
              {DEFAULT_ACTION_KINDS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label className="asset-default-actions__field asset-default-actions__field--label">
            <span>Label</span>
            <input
              type="text"
              aria-label={`Default action ${index + 1} label`}
              value={action.label}
              disabled={disabled}
              aria-invalid={Boolean(error)}
              aria-describedby={error ? errorId : undefined}
              onChange={(event) => updateAction(index, { label: event.target.value })}
            />
          </label>
          <label className="asset-default-actions__field">
            <span>Every</span>
            <input
              type="number"
              min={1}
              step={1}
              aria-label={`Default action ${index + 1} interval days`}
              value={action.interval_days}
              disabled={disabled}
              aria-invalid={Boolean(error)}
              aria-describedby={error ? errorId : undefined}
              onChange={(event) => updateAction(index, { interval_days: event.target.value })}
            />
          </label>
          <label className="asset-default-actions__field">
            <span>Warn</span>
            <input
              type="number"
              min={0}
              step={1}
              aria-label={`Default action ${index + 1} warn before days`}
              value={action.warn_before_days}
              disabled={disabled}
              aria-invalid={Boolean(error)}
              aria-describedby={error ? errorId : undefined}
              onChange={(event) => updateAction(index, { warn_before_days: event.target.value })}
            />
          </label>
          <button
            type="button"
            className="asset-default-actions__remove"
            disabled={disabled}
            aria-label={`Remove default action ${index + 1}`}
            title="Remove default action"
            onClick={() => removeAction(index)}
          >
            <Trash2 size={14} aria-hidden="true" />
          </button>
        </div>
      ))}
      <button
        type="button"
        className="asset-default-actions__add"
        disabled={disabled}
        onClick={() => onChange([...actions, emptyDefaultActionDraft()])}
      >
        <Plus size={14} aria-hidden="true" />
        Add action
      </button>
      {error ? (
        <span id={errorId} className="form-field-error">
          {error}
        </span>
      ) : null}
    </div>
  );
}

function defaultActionDraftFromAction(action: DefaultAssetAction, index: number): DefaultAssetActionDraft {
  return {
    draft_id: `${defaultActionKeyBase(action)}:${index}`,
    kind: action.kind,
    label: action.label,
    interval_days: String(action.interval_days),
    warn_before_days: String(action.warn_before_days),
  };
}

function emptyDefaultActionDraft(): DefaultAssetActionDraft {
  defaultActionDraftCounter += 1;
  return {
    draft_id: `new-default-action-${defaultActionDraftCounter}`,
    kind: "service",
    label: "",
    interval_days: "",
    warn_before_days: "",
  };
}

function defaultActionFromDraft(action: DefaultAssetActionDraft): DefaultAssetAction {
  return {
    kind: action.kind,
    label: action.label.trim(),
    interval_days: Number(action.interval_days),
    warn_before_days: Number(action.warn_before_days),
  };
}

function defaultActionDraftsChanged(
  drafts: readonly DefaultAssetActionDraft[],
  committed: readonly DefaultAssetAction[],
): boolean {
  if (drafts.length !== committed.length) return true;
  return drafts.some((draft, index) => {
    const action = committed[index];
    if (!action) return true;
    return (
      draft.kind !== action.kind ||
      draft.label.trim() !== action.label ||
      Number(draft.interval_days) !== action.interval_days ||
      Number(draft.warn_before_days) !== action.warn_before_days
    );
  });
}

function validateDefaultActionDrafts(actions: readonly DefaultAssetActionDraft[]): string | null {
  for (const [index, action] of actions.entries()) {
    const actionLabel = `Default action ${index + 1}`;
    if (!DEFAULT_ACTION_KINDS.some((option) => option.value === action.kind)) {
      return `${actionLabel}: choose a kind.`;
    }
    if (!action.label.trim()) {
      return `${actionLabel}: enter a label.`;
    }
    const interval = Number(action.interval_days);
    if (!Number.isInteger(interval) || interval < 1) {
      return `${actionLabel}: enter a positive interval.`;
    }
    const warnBefore = Number(action.warn_before_days);
    if (!Number.isInteger(warnBefore) || warnBefore < 0) {
      return `${actionLabel}: enter a warning window of zero days or more.`;
    }
    if (warnBefore > interval) {
      return `${actionLabel}: warning days cannot exceed the interval.`;
    }
  }
  return null;
}
