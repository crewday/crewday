import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  InlineNoteField,
  InlineNumberField,
  InlineSearchableSelectField,
  InlineSelectField,
  InlineTableForm,
  InlineTextField,
  type InlineTableColumn,
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
  order_hint: string;
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
}

const CREATE_ROW_ID = "__new_area__";
const AREA_KINDS: readonly AreaKind[] = ["indoor_room", "outdoor", "service"];
const AREA_KIND_OPTIONS = AREA_KINDS.map((kind) => ({ value: kind, label: kind }));

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

function emptyAreaDraft(): AreaDraft {
  return {
    id: null,
    unit_id: null,
    name: "",
    kind: "indoor_room",
    order_hint: "0",
    parent_area_id: "",
    notes_md: "",
  };
}

function draftFromArea(area: Area): AreaDraft {
  return {
    id: area.id,
    unit_id: area.unit_id,
    name: area.name,
    kind: area.kind,
    order_hint: String(area.order_hint),
    parent_area_id: area.parent_area_id ?? "",
    notes_md: area.notes_md,
  };
}

function areaSelectOption(area: Area): SearchableSelectOption {
  return { value: area.id, label: area.name };
}

function bodyFromDraft(draft: AreaDraft): AreaWriteBody {
  const parsedOrder = Number.parseInt(draft.order_hint, 10);
  return {
    name: draft.name.trim(),
    kind: draft.kind,
    unit_id: draft.unit_id,
    order_hint: Number.isFinite(parsedOrder) ? Math.max(0, parsedOrder) : 0,
    parent_area_id: draft.parent_area_id || null,
    notes_md: draft.notes_md.trim(),
  };
}

async function invalidatePropertyAreaViews(
  queryClient: QueryClient,
  propertyId: string,
): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: qk.propertyAreas(propertyId) }),
    queryClient.invalidateQueries({ queryKey: qk.property(propertyId) }),
    queryClient.invalidateQueries({ queryKey: qk.properties() }),
  ]);
}

export default function AreasPanel({ propertyId }: { propertyId: string }) {
  const queryClient = useQueryClient();
  const [editedDrafts, setEditedDrafts] = useState<ReadonlyMap<string, AreaDraft>>(() => new Map());
  const [rowErrors, setRowErrors] = useState<ReadonlyMap<string, string>>(() => new Map());
  const [createDraft, setCreateDraft] = useState<AreaDraft>(() => emptyAreaDraft());
  const [createDirty, setCreateDirty] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const areasQ = useQuery({
    queryKey: qk.propertyAreas(propertyId),
    queryFn: () =>
      fetchJson<ListEnvelope<Area>>("/api/v1/properties/" + propertyId + "/areas").then(unwrapList),
  });

  const saveArea = useMutation({
    mutationFn: ({ draft }: AreaSaveVariables) => {
      const body = bodyFromDraft(draft);
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
    onSuccess: async (_area, variables) => {
      setRowErrors((current) => clearMapValue(current, variables.rowId));
      if (variables.rowId === CREATE_ROW_ID) {
        setCreateDraft(emptyAreaDraft());
        setCreateDirty(false);
        setCreateError(null);
      } else {
        setEditedDrafts((current) => clearMapValue(current, variables.rowId));
      }
      await invalidatePropertyAreaViews(queryClient, propertyId);
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
      setRowErrors((current) => clearMapValue(current, areaId));
      await invalidatePropertyAreaViews(queryClient, propertyId);
    },
    onError: (err, areaId) => {
      setRowErrors((current) => setMapValue(current, areaId, errorMessage(err, "Area could not be deleted.")));
    },
  });

  const areas = areasQ.data ?? [];
  const areasById = useMemo(() => new Map(areas.map((area) => [area.id, area])), [areas]);
  const busy = saveArea.isPending || deleteArea.isPending;
  const rows = useMemo(
    () => areas.map((area): InlineTableRow<AreaDraft> => {
      const editedDraft = editedDrafts.get(area.id);
      const savingThisRow = saveArea.isPending && saveArea.variables?.rowId === area.id;
      return {
        id: area.id,
        label: area.name,
        draft: editedDraft ?? draftFromArea(area),
        committedDraft: draftFromArea(area),
        editing: editedDraft !== undefined,
        dirty: editedDraft !== undefined,
        saving: savingThisRow,
        disabled: busy && !savingThisRow,
        error: rowErrors.get(area.id),
      };
    }),
    [areas, busy, editedDrafts, rowErrors, saveArea.isPending, saveArea.variables],
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
        renderRead: ({ row }) => parentAreaName(areasById, row.draft.parent_area_id),
        renderEdit: ({ row, update, disabled }) => (
          <InlineSearchableSelectField
            value={row.draft.parent_area_id}
            options={parentOptions(areas, row.draft).map(areaSelectOption)}
            blankOption={{ label: "Property-level" }}
            noResultsLabel="No parent areas"
            renderOptionSecondaryText={() => null}
            onChange={(parent_area_id) => update({ parent_area_id })}
            disabled={disabled}
            label="Parent"
          />
        ),
      },
      {
        key: "order",
        header: "Order",
        width: { px: 96 },
        className: "mono",
        align: "end",
        renderRead: ({ row }) => row.draft.order_hint,
        renderEdit: ({ row, update, disabled }) => (
          <InlineNumberField
            value={row.draft.order_hint}
            min={0}
            step={1}
            onChange={(order_hint) => update({ order_hint })}
            disabled={disabled}
            ariaLabel="Order"
          />
        ),
      },
    ],
    [areas, areasById],
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
            const draft = current.get(rowId) ?? draftFromArea(area);
            return setMapValue(current, rowId, { ...draft, ...patch });
          });
          setRowErrors((current) => clearMapValue(current, rowId));
        }}
        onEdit={(rowId) => {
          const area = areasById.get(rowId);
          if (!area) return;
          setEditedDrafts((current) => setMapValue(current, rowId, draftFromArea(area)));
          setRowErrors((current) => clearMapValue(current, rowId));
        }}
        onSave={(rowId) => {
          if (rowId === CREATE_ROW_ID) {
            saveArea.mutate({ rowId, draft: createDraft });
            return;
          }
          const draft = editedDrafts.get(rowId);
          if (draft) saveArea.mutate({ rowId, draft });
        }}
        onCancel={(rowId) => {
          if (rowId === CREATE_ROW_ID) {
            setCreateDraft(emptyAreaDraft());
            setCreateDirty(false);
            setCreateError(null);
            return;
          }
          setEditedDrafts((current) => clearMapValue(current, rowId));
          setRowErrors((current) => clearMapValue(current, rowId));
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
          return row.draft.notes_md ? <p>{row.draft.notes_md}</p> : null;
        }}
        renderDeleteConfirmation={({ row, label }) => {
          const childCount = childAreaCount(areas, row.id);
          return {
            title: "Delete area?",
            confirmLabel: "Delete area",
            children: (
              <p>
                Delete <strong>{label}</strong>? {childCount > 0
                  ? "This will also delete " + childCount + " child " + (childCount === 1 ? "area." : "areas.")
                  : "This cannot be undone."}
              </p>
            ),
          };
        }}
      />
    </div>
  );
}

function parentAreaName(areasById: ReadonlyMap<string, Area>, parentAreaId: string): string {
  return areasById.get(parentAreaId)?.name ?? "Property-level";
}

function parentOptions(areas: readonly Area[], draft: AreaDraft): Area[] {
  if (draft.id && childAreaCount(areas, draft.id) > 0) return [];
  return areas.filter((area) => area.parent_area_id === null && area.id !== draft.id);
}

function childAreaCount(areas: readonly Area[], areaId: string): number {
  return areas.filter((area) => area.parent_area_id === areaId).length;
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
