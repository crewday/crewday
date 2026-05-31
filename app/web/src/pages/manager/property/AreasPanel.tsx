import { useCallback, useMemo, useReducer, type SetStateAction } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  InlineNoteDisplay,
  InlineNoteField,
  InlineSearchableSelectField,
  InlineSelectField,
  InlineTableForm,
  InlineTextField,
  type InlineTableColumn,
  type InlineTableReorder,
  type InlineTableRow,
} from "@/components/InlineTableForm";
import { Loading } from "@/components/common";
import { fetchJson } from "@/lib/api";
import { type ListEnvelope, unwrapList } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import type { SearchableSelectOption } from "@/components/SearchableSelect";

type AreaKind = "indoor_room" | "outdoor" | "service";

interface Area {
  id: string;
  property_id: string;
  unit_id: string | null;
  name: string;
  kind: AreaKind;
  order_hint: number;
  parent_area_id: string | null;
  notes_md: string;
}

interface AreaDraft {
  id: string | null;
  unit_id: string | null;
  name: string;
  kind: AreaKind;
  order_hint: number;
  parent_area_id: string;
  notes_md: string;
}

interface AreaWriteBody {
  name: string;
  kind: AreaKind;
  unit_id: string | null;
  order_hint: number;
  parent_area_id: string | null;
  notes_md: string;
}

interface AreaSaveVariables {
  rowId: string;
  draft: AreaDraft;
  orderHint?: number;
}

interface AreaReorderVariables {
  rowId: string;
  orderings: readonly AreaReorderPatch[];
}

interface AreaReorderPatch {
  area: Area;
  draft: AreaDraft;
  orderHint: number;
}

interface AreasPanelState {
  editedDrafts: ReadonlyMap<string, AreaDraft>;
  savedDrafts: ReadonlyMap<string, AreaDraft>;
  rowErrors: ReadonlyMap<string, string>;
  createDraft: AreaDraft;
  createDirty: boolean;
  createError: string | null;
}

type AreasPanelAction =
  | { type: "editedDrafts"; value: SetStateAction<ReadonlyMap<string, AreaDraft>> }
  | { type: "savedDrafts"; value: SetStateAction<ReadonlyMap<string, AreaDraft>> }
  | { type: "rowErrors"; value: SetStateAction<ReadonlyMap<string, string>> }
  | { type: "createDraft"; value: SetStateAction<AreaDraft> }
  | { type: "createDirty"; value: boolean }
  | { type: "createError"; value: string | null };

const CREATE_ROW_ID = "__new_area__";
const AREA_KINDS: readonly AreaKind[] = ["indoor_room", "outdoor", "service"];
const AREA_KIND_OPTIONS = AREA_KINDS.map((kind) => ({ value: kind, label: kind }));

interface AreaTreeRow {
  area: Area;
  depth: number;
  hasChildren: boolean;
  isLastChild: boolean;
}

interface AreaTree {
  rows: AreaTreeRow[];
  descendantIdsByAreaId: ReadonlyMap<string, ReadonlySet<string>>;
  pathLabelsByAreaId: ReadonlyMap<string, string>;
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

function emptyAreaDraft(): AreaDraft {
  return {
    id: null,
    unit_id: null,
    name: "",
    kind: "indoor_room",
    order_hint: 0,
    parent_area_id: "",
    notes_md: "",
  };
}

function initialAreasPanelState(): AreasPanelState {
  return {
    editedDrafts: new Map(),
    savedDrafts: new Map(),
    rowErrors: new Map(),
    createDraft: emptyAreaDraft(),
    createDirty: false,
    createError: null,
  };
}

function resolveStateAction<TValue>(current: TValue, action: SetStateAction<TValue>): TValue {
  return typeof action === "function" ? (action as (current: TValue) => TValue)(current) : action;
}

function areasPanelReducer(state: AreasPanelState, action: AreasPanelAction): AreasPanelState {
  switch (action.type) {
    case "editedDrafts":
      return { ...state, editedDrafts: resolveStateAction(state.editedDrafts, action.value) };
    case "savedDrafts":
      return { ...state, savedDrafts: resolveStateAction(state.savedDrafts, action.value) };
    case "rowErrors":
      return { ...state, rowErrors: resolveStateAction(state.rowErrors, action.value) };
    case "createDraft":
      return { ...state, createDraft: resolveStateAction(state.createDraft, action.value) };
    case "createDirty":
      return { ...state, createDirty: action.value };
    case "createError":
      return { ...state, createError: action.value };
  }
}

function draftFromArea(area: Area): AreaDraft {
  return {
    id: area.id,
    unit_id: area.unit_id,
    name: area.name,
    kind: area.kind,
    order_hint: area.order_hint,
    parent_area_id: area.parent_area_id ?? "",
    notes_md: area.notes_md,
  };
}

function bodyFromDraft(draft: AreaDraft, orderHint = draft.order_hint): AreaWriteBody {
  return {
    name: draft.name.trim(),
    kind: draft.kind,
    unit_id: draft.unit_id,
    order_hint: Math.max(0, orderHint),
    parent_area_id: draft.parent_area_id || null,
    notes_md: draft.notes_md.trim(),
  };
}

function nextAreaOrderHint(areas: readonly Area[]): number {
  if (areas.length === 0) return 0;
  return Math.max(...areas.map((area) => area.order_hint)) + 1;
}

// react-doctor-disable-next-line react-doctor/no-giant-component -- Existing promoted surface is intentionally deferred until a focused component split preserves behavior.
export default function AreasPanel({ propertyId }: { propertyId: string }) {
  const queryClient = useQueryClient();
  const [state, dispatch] = useReducer(areasPanelReducer, undefined, initialAreasPanelState);
  const { editedDrafts, savedDrafts, rowErrors, createDraft, createDirty, createError } = state;
  const setEditedDrafts = (value: SetStateAction<ReadonlyMap<string, AreaDraft>>): void => {
    dispatch({ type: "editedDrafts", value });
  };
  const setSavedDrafts = (value: SetStateAction<ReadonlyMap<string, AreaDraft>>): void => {
    dispatch({ type: "savedDrafts", value });
  };
  const setRowErrors = (value: SetStateAction<ReadonlyMap<string, string>>): void => {
    dispatch({ type: "rowErrors", value });
  };
  const setCreateDraft = (value: SetStateAction<AreaDraft>): void => {
    dispatch({ type: "createDraft", value });
  };
  const setCreateDirty = (value: boolean): void => {
    dispatch({ type: "createDirty", value });
  };
  const setCreateError = (value: string | null): void => {
    dispatch({ type: "createError", value });
  };

  const areasQ = useQuery({
    queryKey: qk.propertyAreas(propertyId),
    queryFn: () =>
      fetchJson<ListEnvelope<Area>>("/api/v1/properties/" + propertyId + "/areas").then(unwrapList),
  });

  const saveArea = useMutation({
    mutationFn: ({ draft, orderHint }: AreaSaveVariables) => {
      const body = bodyFromDraft(draft, orderHint);
      if (draft.id) {
        return fetchJson<Area>("/api/v1/areas/" + draft.id, {
          method: "PATCH",
          body,
        });
      }
      return fetchJson<Area>("/api/v1/properties/" + propertyId + "/areas", {
        method: "POST",
        body,
      });
    },
    onSuccess: async (area, variables) => {
      setRowErrors((current) => clearMapValue(current, variables.rowId));
      if (variables.rowId === CREATE_ROW_ID) {
        setCreateDraft(emptyAreaDraft());
        setCreateDirty(false);
        setCreateError(null);
      } else {
        const savedDraft = draftFromArea(area);
        setEditedDrafts((current) => clearMapValue(current, variables.rowId));
        setSavedDrafts((current) => setMapValue(current, variables.rowId, savedDraft));
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: qk.propertyAreas(propertyId) }),
        queryClient.invalidateQueries({ queryKey: qk.property(propertyId) }),
        queryClient.invalidateQueries({ queryKey: qk.properties() }),
      ]);
    },
    onError: (err, variables) => {
      const message = errorMessage(err, "Area could not be saved.");
      if (variables.rowId === CREATE_ROW_ID) {
        setCreateError(message);
        return;
      }
      setRowErrors((current) => setMapValue(current, variables.rowId, message));
    },
  });

  const deleteArea = useMutation({
    mutationFn: (areaId: string) =>
      fetchJson<null>("/api/v1/areas/" + areaId, { method: "DELETE" }),
    onSuccess: async (_result, areaId) => {
      setEditedDrafts((current) => clearMapValue(current, areaId));
      setSavedDrafts((current) => clearMapValue(current, areaId));
      setRowErrors((current) => clearMapValue(current, areaId));
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: qk.propertyAreas(propertyId) }),
        queryClient.invalidateQueries({ queryKey: qk.property(propertyId) }),
        queryClient.invalidateQueries({ queryKey: qk.properties() }),
      ]);
    },
    onError: (err, areaId) => {
      setRowErrors((current) => setMapValue(current, areaId, errorMessage(err, "Area could not be deleted.")));
    },
  });

  const areas = useMemo(() => areasQ.data ?? [], [areasQ.data]);
  const areasById = useMemo(() => new Map(areas.map((area) => [area.id, area])), [areas]);
  let activeSavedDrafts = savedDrafts;
  const normalizedSavedDrafts = normalizeSavedAreaDrafts(savedDrafts, areasById);
  if (normalizedSavedDrafts !== savedDrafts) {
    activeSavedDrafts = normalizedSavedDrafts;
    setSavedDrafts(normalizedSavedDrafts);
  }

  const reorderAreas = useMutation({
    mutationFn: ({ orderings }: AreaReorderVariables) =>
      Promise.all(orderings.map(({ area, draft, orderHint }) => fetchJson<Area>("/api/v1/areas/" + area.id, {
        method: "PATCH",
        body: bodyFromDraft(draft, orderHint),
      }))),
    onSuccess: async (_areas, variables) => {
      setRowErrors((current) => clearMapValue(current, variables.rowId));
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: qk.propertyAreas(propertyId) }),
        queryClient.invalidateQueries({ queryKey: qk.property(propertyId) }),
        queryClient.invalidateQueries({ queryKey: qk.properties() }),
      ]);
    },
    onError: (err, variables) => {
      setRowErrors((current) => setMapValue(current, variables.rowId, errorMessage(err, "Areas could not be reordered.")));
    },
  });

  const busy = saveArea.isPending || deleteArea.isPending || reorderAreas.isPending;
  const areaTree = useMemo(() => buildAreaTree(areas), [areas]);
  const rows = useMemo(
    () => areaTree.rows.map(({ area, depth, hasChildren, isLastChild }): InlineTableRow<AreaDraft> => {
      const editedDraft = editedDrafts.get(area.id);
      const savedDraft = activeSavedDrafts.get(area.id);
      const savingThisRow = saveArea.isPending && saveArea.variables?.rowId === area.id;
      return {
        id: area.id,
        label: area.name,
        draft: editedDraft ?? savedDraft ?? draftFromArea(area),
        committedDraft: draftFromArea(area),
        editing: editedDraft !== undefined,
        dirty: editedDraft !== undefined,
        saving: savingThisRow,
        disabled: busy && !savingThisRow,
        error: rowErrors.get(area.id),
        tree: { depth, parentId: area.parent_area_id ?? undefined, hasChildren, isLastChild },
      };
    }),
    [areaTree.rows, busy, editedDrafts, rowErrors, activeSavedDrafts, saveArea.isPending, saveArea.variables],
  );
  const trailingCreateRow: InlineTableRow<AreaDraft> = {
    id: CREATE_ROW_ID,
    label: "New area",
    draft: createDraft,
    editing: true,
    dirty: createDirty,
    isNew: true,
    saving: saveArea.isPending && saveArea.variables?.rowId === CREATE_ROW_ID,
    disabled: busy && saveArea.variables?.rowId !== CREATE_ROW_ID,
    error: createError,
  };

  const reorderAreaRows = useCallback(({ rowId, orderedRows }: InlineTableReorder<AreaDraft>): void => {
    if (reorderAreas.isPending) return;
    const orderings: AreaReorderPatch[] = [];
    for (const [index, row] of orderedRows.entries()) {
      const area = areasById.get(row.id);
      if (!area) return;
      orderings.push({
        area,
        draft: row.draft,
        orderHint: index,
      });
    }
    reorderAreas.mutate({ rowId, orderings });
  }, [areasById, reorderAreas]);

  const columns = useMemo(
    (): InlineTableColumn<AreaDraft>[] => [
      {
        key: "name",
        header: "Name",
        width: { flex: 1.45, min: 160 },
        renderRead: ({ row }) => <strong>{row.draft.name}</strong>,
        renderEdit: ({ row, update, disabled }) => (
          <InlineTextField
            value={row.draft.name}
            onChange={(name) => update({ name })}
            disabled={disabled}
            ariaLabel="Name"
          />
        ),
      },
      {
        key: "kind",
        header: "Kind",
        width: { flex: 0.9, min: 132 },
        renderRead: ({ row }) => row.draft.kind,
        renderEdit: ({ row, update, disabled }) => (
          <InlineSelectField
            value={row.draft.kind}
            options={AREA_KIND_OPTIONS}
            onChange={(kind) => update({ kind: kind as AreaKind })}
            disabled={disabled}
            ariaLabel="Kind"
          />
        ),
      },
      {
        key: "parent",
        header: "Parent",
        width: { flex: 1.1, min: 170 },
        renderRead: ({ row }) => parentAreaName(areaTree.pathLabelsByAreaId, row.draft.parent_area_id),
        renderEdit: ({ row, update, disabled }) => (
          <InlineSearchableSelectField
            value={row.draft.parent_area_id}
            options={parentOptions(areaTree, row.draft)}
            blankOption={{ label: "Property-level" }}
            noResultsLabel="No parent areas"
            onChange={(parent_area_id) => update({ parent_area_id })}
            disabled={disabled}
            label="Parent"
          />
        ),
      },
    ],
    [areaTree],
  );

  if (areasQ.isPending) return <Loading />;
  if (areasQ.isError || !areasQ.data) {
    return (
      <p className="form-error" role="alert">
        {errorMessage(areasQ.error, "Failed to load areas.")}
      </p>
    );
  }

  return (
    <div className="panel">
      <header className="panel__head">
        <div className="panel__head-stack">
          <h2>Areas</h2>
          <p className="panel__sub">Rooms and shared spaces used by tasks, instructions, and inventory.</p>
        </div>
      </header>
      <InlineTableForm
        ariaLabel="Property areas"
        columns={columns}
        rows={rows}
        saveMode="explicit"
        onDraftChange={(rowId, patch) => {
          if (rowId === CREATE_ROW_ID) {
            setCreateDraft((current) => ({ ...current, ...patch }));
            setCreateDirty(true);
            setCreateError(null);
            return;
          }
          setEditedDrafts((current) => {
            const area = areasById.get(rowId);
            if (!area) return current;
            const draft = current.get(rowId) ?? activeSavedDrafts.get(rowId) ?? draftFromArea(area);
            return setMapValue(current, rowId, { ...draft, ...patch });
          });
          setSavedDrafts((current) => clearMapValue(current, rowId));
          setRowErrors((current) => clearMapValue(current, rowId));
        }}
        onEdit={(rowId) => {
          const area = areasById.get(rowId);
          if (!area) return;
          setEditedDrafts((current) => setMapValue(current, rowId, activeSavedDrafts.get(rowId) ?? draftFromArea(area)));
          setSavedDrafts((current) => clearMapValue(current, rowId));
          setRowErrors((current) => clearMapValue(current, rowId));
        }}
        onSave={(rowId) => {
          if (rowId === CREATE_ROW_ID) {
            saveArea.mutate({
              rowId,
              draft: createDraft,
              orderHint: nextAreaOrderHint(areas),
            });
            return;
          }
          const draft = editedDrafts.get(rowId);
          if (draft) {
            saveArea.mutate({ rowId, draft, orderHint: areasById.get(rowId)?.order_hint });
          }
        }}
        onCancel={(rowId) => {
          if (rowId === CREATE_ROW_ID) {
            setCreateDraft(emptyAreaDraft());
            setCreateDirty(false);
            setCreateError(null);
            return;
          }
          setEditedDrafts((current) => clearMapValue(current, rowId));
          setSavedDrafts((current) => clearMapValue(current, rowId));
          setRowErrors((current) => clearMapValue(current, rowId));
        }}
        onReorder={reorderAreaRows}
        getReorderScope={({ row }) => {
          const area = areasById.get(row.id);
          if (!area || siblingAreasForArea(areas, area).length < 2) return null;
          return area.parent_area_id ?? "";
        }}
        isReorderDisabled={({ row }) => {
          const area = areasById.get(row.id);
          if (!area) return true;
          const siblingIds = new Set(siblingAreasForArea(areas, area).map((sibling) => sibling.id));
          return rows.some((candidate) =>
            siblingIds.has(candidate.id) && (candidate.editing || candidate.saving || candidate.disabled),
          );
        }}
        onDelete={(rowId) => deleteArea.mutate(rowId)}
        trailingCreateRow={trailingCreateRow}
        getRowLabel={(row) => row.draft.name || row.label || "New area"}
        renderDetail={({ row, update, disabled }) => {
          if (row.editing) {
            return (
              <InlineNoteField
                value={row.draft.notes_md}
                onChange={(notes_md) => update({ notes_md })}
                disabled={disabled}
                ariaLabel="Notes"
                placeholder="Notes"
              />
            );
          }
          return row.draft.notes_md ? <InlineNoteDisplay>{row.draft.notes_md}</InlineNoteDisplay> : null;
        }}
        renderDeleteConfirmation={({ row, label }) => {
          const descendantCount = areaTree.descendantIdsByAreaId.get(row.id)?.size ?? 0;
          return {
            title: "Delete area?",
            confirmLabel: "Delete area",
            children: (
              <p>
                Delete <strong>{label}</strong>? {descendantCount > 0
                  ? "This will also delete " + descendantCount + " descendant "
                    + (descendantCount === 1 ? "area." : "areas.")
                  : "This cannot be undone."}
              </p>
            ),
          };
        }}
      />
    </div>
  );
}

function parentAreaName(pathLabelsByAreaId: ReadonlyMap<string, string>, parentAreaId: string): string {
  return pathLabelsByAreaId.get(parentAreaId) ?? "Property-level";
}

function parentOptions(areaTree: AreaTree, draft: AreaDraft): SearchableSelectOption[] {
  const excludedIds = new Set(draft.id ? [draft.id, ...(areaTree.descendantIdsByAreaId.get(draft.id) ?? [])] : []);
  return areaTree.rows.flatMap(({ area }) => {
    if (excludedIds.has(area.id)) return [];
    const pathLabel = areaTree.pathLabelsByAreaId.get(area.id) ?? area.name;
    return [{
      value: area.id,
      label: pathLabel,
      searchText: pathLabel,
    }];
  });
}

function buildAreaTree(areas: readonly Area[]): AreaTree {
  const areasById = new Map(areas.map((area) => [area.id, area]));
  const childrenByParentId = new Map<string, Area[]>();
  const roots: Area[] = [];
  for (const area of sortAreas(areas)) {
    if (!area.parent_area_id || !areasById.has(area.parent_area_id)) {
      roots.push(area);
      continue;
    }
    const siblings = childrenByParentId.get(area.parent_area_id) ?? [];
    siblings.push(area);
    childrenByParentId.set(area.parent_area_id, siblings);
  }
  for (const [parentId, children] of childrenByParentId) {
    childrenByParentId.set(parentId, sortAreas(children));
  }

  const rows: AreaTreeRow[] = [];
  const pathLabelsByAreaId = new Map<string, string>();
  const visited = new Set<string>();
  const collect = (area: Area, depth: number, isLastChild: boolean, path: readonly string[]): ReadonlySet<string> => {
    visited.add(area.id);
    const nextPath = [...path, area.name];
    pathLabelsByAreaId.set(area.id, nextPath.join(" / "));
    const children = childrenByParentId.get(area.id)?.filter((child) => !visited.has(child.id)) ?? [];
    rows.push({ area, depth, hasChildren: children.length > 0, isLastChild });
    const descendantIds = new Set<string>();
    children.forEach((child, index) => {
      descendantIds.add(child.id);
      const childDescendantIds = collect(child, depth + 1, index === children.length - 1, nextPath);
      for (const descendantId of childDescendantIds) {
        descendantIds.add(descendantId);
      }
    });
    return descendantIds;
  };

  const sortedRoots = sortAreas(roots);
  sortedRoots.forEach((area, index) => {
    if (!visited.has(area.id)) {
      collect(area, 0, index === sortedRoots.length - 1, []);
    }
  });
  for (const area of sortAreas(areas)) {
    if (!visited.has(area.id)) {
      collect(area, 0, true, []);
    }
  }
  const descendantIdsByAreaId = descendantIdsByOriginalGraph(areas, childrenByParentId);
  return { rows, descendantIdsByAreaId, pathLabelsByAreaId };
}

function siblingAreasForArea(areas: readonly Area[], area: Area): Area[] {
  return sortAreas(areas.filter((candidate) => candidate.parent_area_id === area.parent_area_id));
}

function sortAreas(areas: readonly Area[]): Area[] {
  return Array.from(areas).sort(
    (left, right) =>
      left.order_hint - right.order_hint
      || left.name.localeCompare(right.name)
      || left.id.localeCompare(right.id),
  );
}

function descendantIdsByOriginalGraph(
  areas: readonly Area[],
  childrenByParentId: ReadonlyMap<string, readonly Area[]>,
): ReadonlyMap<string, ReadonlySet<string>> {
  const descendantsByAreaId = new Map<string, ReadonlySet<string>>();
  for (const area of areas) {
    descendantsByAreaId.set(area.id, descendantIdsForArea(area.id, area.id, childrenByParentId, new Set()));
  }
  return descendantsByAreaId;
}

function descendantIdsForArea(
  areaId: string,
  rootId: string,
  childrenByParentId: ReadonlyMap<string, readonly Area[]>,
  visiting: ReadonlySet<string>,
): ReadonlySet<string> {
  if (visiting.has(areaId)) return new Set();
  const nextVisiting = new Set(visiting);
  nextVisiting.add(areaId);
  const descendantIds = new Set<string>();
  for (const child of childrenByParentId.get(areaId) ?? []) {
    if (child.id === rootId) continue;
    descendantIds.add(child.id);
    for (const childDescendantId of descendantIdsForArea(child.id, rootId, childrenByParentId, nextVisiting)) {
      descendantIds.add(childDescendantId);
    }
  }
  return descendantIds;
}

function areaDraftsEqual(left: AreaDraft, right: AreaDraft): boolean {
  return left.id === right.id
    && left.unit_id === right.unit_id
    && left.name === right.name
    && left.kind === right.kind
    && left.order_hint === right.order_hint
    && left.parent_area_id === right.parent_area_id
    && left.notes_md === right.notes_md;
}

function normalizeSavedAreaDrafts(
  current: ReadonlyMap<string, AreaDraft>,
  areasById: ReadonlyMap<string, Area>,
): ReadonlyMap<string, AreaDraft> {
  let changed = false;
  const next = new Map<string, AreaDraft>();
  for (const [areaId, draft] of current) {
    const area = areasById.get(areaId);
    if (area && areaDraftsEqual(draftFromArea(area), draft)) {
      changed = true;
      continue;
    }
    next.set(areaId, draft);
  }
  return changed ? next : current;
}

function setMapValue<TValue>(
  current: ReadonlyMap<string, TValue>,
  key: string,
  value: TValue,
): ReadonlyMap<string, TValue> {
  const next = new Map(current);
  next.set(key, value);
  return next;
}

function clearMapValue<TValue>(
  current: ReadonlyMap<string, TValue>,
  key: string,
): ReadonlyMap<string, TValue> {
  if (!current.has(key)) return current;
  const next = new Map(current);
  next.delete(key);
  return next;
}
