import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { MapPinned } from "lucide-react";
import { fetchJson } from "@/lib/api";
import { type ListEnvelope, unwrapList } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import SearchableSelect, { type SearchableSelectOption } from "@/components/SearchableSelect";
import { EmptyState, Loading } from "@/components/common";

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

const AREA_KINDS: readonly AreaKind[] = ["indoor_room", "outdoor", "service"];

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
  // code-health: ignore[ccn nloc] Areas editor keeps table, draft form, and delete confirmation coupled to one property query.
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<AreaDraft>(() => emptyAreaDraft());
  const [formOpen, setFormOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deleteConfirmationId, setDeleteConfirmationId] = useState<string | null>(null);

  const areasQ = useQuery({
    queryKey: qk.propertyAreas(propertyId),
    queryFn: () =>
      fetchJson<ListEnvelope<Area>>("/api/v1/properties/" + propertyId + "/areas").then(unwrapList),
  });

  const saveArea = useMutation({
    mutationFn: (nextDraft: AreaDraft) => {
      const body = bodyFromDraft(nextDraft);
      if (nextDraft.id) {
        return fetchJson<Area>("/api/v1/areas/" + nextDraft.id, {
          method: "PATCH",
          body,
        });
      }
      return fetchJson<Area>("/api/v1/properties/" + propertyId + "/areas", {
        method: "POST",
        body,
      });
    },
    onSuccess: async () => {
      setError(null);
      setDeleteConfirmationId(null);
      setDraft(emptyAreaDraft());
      setFormOpen(false);
      await invalidatePropertyAreaViews(queryClient, propertyId);
    },
    onError: (err) => {
      setError(errorMessage(err, "Area could not be saved."));
    },
  });

  const deleteArea = useMutation({
    mutationFn: (areaId: string) =>
      fetchJson<null>("/api/v1/areas/" + areaId, { method: "DELETE" }),
    onSuccess: async () => {
      setError(null);
      setDeleteConfirmationId(null);
      setDraft(emptyAreaDraft());
      setFormOpen(false);
      await invalidatePropertyAreaViews(queryClient, propertyId);
    },
    onError: (err) => {
      setError(errorMessage(err, "Area could not be deleted."));
    },
  });

  const areas = areasQ.data ?? [];
  const parentOptions = areas.filter((area) => area.parent_area_id === null && area.id !== draft.id);
  const childDeleteCount = draft.id
    ? areas.filter((area) => area.parent_area_id === draft.id).length
    : 0;
  const confirmingDelete = draft.id !== null && deleteConfirmationId === draft.id;
  const deleteWarning = childDeleteCount > 0
    ? "Delete " + draft.name + "? This will also delete " + childDeleteCount + " child " +
      (childDeleteCount === 1 ? "area." : "areas.")
    : "Delete " + draft.name + "? This cannot be undone.";
  const busy = saveArea.isPending || deleteArea.isPending;

  function openCreate() {
    setDraft(emptyAreaDraft());
    setError(null);
    setDeleteConfirmationId(null);
    setFormOpen(true);
  }

  function openEdit(area: Area) {
    setDraft(draftFromArea(area));
    setError(null);
    setDeleteConfirmationId(null);
    setFormOpen(true);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setDeleteConfirmationId(null);
    saveArea.mutate(draft);
  }

  function requestDelete() {
    if (!draft.id) return;
    if (!confirmingDelete) {
      setError(null);
      setDeleteConfirmationId(draft.id);
      return;
    }
    deleteArea.mutate(draft.id);
  }

  if (areasQ.isPending) return <Loading />;
  if (areasQ.isError || !areasQ.data) {
    return (
      <p className="form-error" role="alert">
        {errorMessage(areasQ.error, "Failed to load areas.")}
      </p>
    );
  }

  return (
    <>
      <div className="panel">
        <header className="panel__head">
          <div className="panel__head-stack">
            <h2>Areas</h2>
            <p className="panel__sub">Rooms and shared spaces used by tasks, instructions, and inventory.</p>
          </div>
          <button type="button" className="btn btn--moss" onClick={openCreate}>
            New area
          </button>
        </header>
        <table className="table table--roomy">
          <thead>
            <tr><th>Name</th><th>Kind</th><th>Parent</th><th>Order</th><th></th></tr>
          </thead>
          <tbody>
            {areas.length === 0 ? (
              <tr>
                <td colSpan={5}>
                  <EmptyState
                    icon={MapPinned}
                    title="No areas yet"
                    copy="Rooms and shared spaces will appear here once they are added."
                    variant="quiet"
                  />
                </td>
              </tr>
            ) : (
              areas.map((area) => {
                const parentName = areas.find((candidate) => candidate.id === area.parent_area_id)?.name;
                return (
                  <tr key={area.id}>
                    <td>
                      <strong>{area.name}</strong>
                      {area.notes_md && <div className="table__sub">{area.notes_md}</div>}
                    </td>
                    <td>{area.kind}</td>
                    <td>{parentName ?? "Property-level"}</td>
                    <td className="mono">{area.order_hint}</td>
                    <td>
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost"
                        onClick={() => openEdit(area)}
                      >
                        Edit
                      </button>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {formOpen && (
        <div className="panel">
          <header className="panel__head">
            <div className="panel__head-stack">
              <h2>{draft.id ? "Edit area" : "Create area"}</h2>
              <p className="panel__sub">Area nesting is limited to one level.</p>
            </div>
          </header>
          <form className="form" onSubmit={submit}>
            <div className="form-grid form-grid--two">
              <label className="field">
                <span>Name</span>
                <input
                  value={draft.name}
                  onChange={(event) => setDraft({ ...draft, name: event.currentTarget.value })}
                  required
                />
              </label>
              <label className="field">
                <span>Kind</span>
                <select
                  value={draft.kind}
                  onChange={(event) => setDraft({ ...draft, kind: event.currentTarget.value as AreaKind })}
                >
                  {AREA_KINDS.map((kind) => (
                    <option key={kind} value={kind}>{kind}</option>
                  ))}
                </select>
              </label>
              <SearchableSelect
                label="Parent"
                requirement="optional"
                value={draft.parent_area_id}
                options={parentOptions.map(areaSelectOption)}
                blankOption={{ label: "Property-level" }}
                renderOptionSecondaryText={() => null}
                onChange={(value) => setDraft({ ...draft, parent_area_id: value })}
              />
              <label className="field">
                <span>Order</span>
                <input
                  type="number"
                  min={0}
                  value={draft.order_hint}
                  onChange={(event) => setDraft({ ...draft, order_hint: event.currentTarget.value })}
                />
              </label>
            </div>
            <label className="field">
              <span>Notes</span>
              <textarea
                value={draft.notes_md}
                onChange={(event) => setDraft({ ...draft, notes_md: event.currentTarget.value })}
                rows={3}
              />
            </label>
            {error && <p className="form-error" role="alert">{error}</p>}
            {confirmingDelete && (
              <p id="area-delete-warning" className="form-error" role="alert">
                {deleteWarning}
              </p>
            )}
            <div className="inline-actions">
              {draft.id && (
                <button
                  type="button"
                  className="btn btn--rust"
                  disabled={busy}
                  aria-describedby={confirmingDelete ? "area-delete-warning" : undefined}
                  onClick={requestDelete}
                >
                  {confirmingDelete ? "Confirm delete" : "Delete"}
                </button>
              )}
              <button
                type="button"
                className="btn btn--ghost"
                disabled={busy}
                onClick={() => {
                  setDraft(emptyAreaDraft());
                  setDeleteConfirmationId(null);
                  setFormOpen(false);
                }}
              >
                Cancel
              </button>
              <button type="submit" className="btn btn--moss" disabled={busy}>
                {saveArea.isPending ? "Saving..." : "Save area"}
              </button>
            </div>
          </form>
        </div>
      )}
    </>
  );
}
