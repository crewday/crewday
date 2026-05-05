import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fetchJson } from "@/lib/api";
import { type ListEnvelope, unwrapList } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import type { Me, Property, Task } from "@/types/api";
import { Checkbox } from "@/components/common";

// §06 quick-add. Clicking the button opens a <dialog> (same pattern as
// the task skip modal in TaskDetailPage) and POSTs to /api/v1/tasks.
// Default is `is_personal = true` — a flip-to-team toggle lives in the
// modal so team tasks still take one click.

interface NewTaskBody {
  title: string;
  scheduled_for_local: string;
  property_id?: string;
  area_id?: string;
  assigned_user_id?: string;
  is_personal: boolean;
}

interface Area {
  id: string;
  name: string;
}

export default function NewTaskButton() {
  // code-health: ignore[nloc] Quick-add task dialog keeps its compact form state and mutation beside the button that opens it.
  const ref = useRef<HTMLDialogElement>(null);
  const qc = useQueryClient();
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const meQ = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });
  const [propertyId, setPropertyId] = useState("");
  const areasQ = useQuery({
    queryKey: qk.propertyAreas(propertyId),
    queryFn: () =>
      fetchJson<ListEnvelope<Area>>("/api/v1/properties/" + propertyId + "/areas").then(unwrapList),
    enabled: Boolean(propertyId),
  });

  const systemTodayIso = new Date().toISOString().slice(0, 10);
  const defaultDue = meQ.data?.today ?? systemTodayIso;
  const [title, setTitle] = useState("");
  const [due, setDue] = useState(defaultDue);
  const [areaId, setAreaId] = useState("");
  const [personal, setPersonal] = useState(true);
  const [formError, setFormError] = useState<string | null>(null);
  const submitLocked = useRef(false);

  useEffect(() => {
    setDue((current) => (current === systemTodayIso ? defaultDue : current));
  }, [defaultDue, systemTodayIso]);

  const reset = () => {
    setTitle("");
    setDue(defaultDue);
    setPropertyId("");
    setAreaId("");
    setPersonal(true);
    setFormError(null);
  };

  const create = useMutation({
    mutationFn: (payload: NewTaskBody) =>
      fetchJson<Task>("/api/v1/tasks", { method: "POST", body: payload }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.today() });
      qc.invalidateQueries({ queryKey: qk.week() });
      qc.invalidateQueries({ queryKey: qk.tasks() });
      ref.current?.close();
      reset();
    },
    onError: (error) => {
      setFormError(taskCreateErrorMessage(error));
    },
    onSettled: () => {
      submitLocked.current = false;
    },
  });

  const currentUserId = meQ.data?.user_id ?? "";

  return (
    <>
      <button
        type="button"
        className="btn btn--moss"
        onClick={() => ref.current?.showModal()}
      >
        + New task
      </button>

      <dialog
        className="modal"
        ref={ref}
        onCancel={(e) => {
          if (submitLocked.current || create.isPending) e.preventDefault();
        }}
        onClose={reset}
      >
        <form
          className="modal__body"
          onSubmit={(e) => {
            e.preventDefault();
            if (submitLocked.current || create.isPending) return;
            const trimmed = title.trim();
            if (!trimmed) return;
            if (!due) {
              setFormError("Choose a due date before adding the task.");
              return;
            }
            if (personal && !currentUserId) {
              setFormError("Your profile is still loading. Wait a moment and try again.");
              return;
            }
            submitLocked.current = true;
            setFormError(null);
            create.mutate({
              title: trimmed,
              scheduled_for_local: due + "T09:00:00",
              property_id: propertyId || undefined,
              area_id: areaId || undefined,
              assigned_user_id: personal ? currentUserId : undefined,
              is_personal: personal,
            });
          }}
        >
          <h3 className="modal__title">New task</h3>
          <p className="modal__sub">
            {personal
              ? "Personal — only you can see this."
              : "Team task — visible to your manager."}
          </p>

          <label className="field">
            <span>Title</span>
            <input
              autoFocus
              required
              value={title}
              onChange={(e) => {
                setTitle(e.target.value);
                setFormError(null);
              }}
              placeholder="e.g. Call back Maria about the stay"
            />
          </label>

          <label className="field">
            <span>Due</span>
            <input
              type="date"
              required
              value={due}
              onChange={(e) => {
                setDue(e.target.value);
                setFormError(null);
              }}
            />
          </label>

          <label className="field">
            <span>Property</span>
            <select
              value={propertyId}
              onChange={(e) => {
                setPropertyId(e.target.value);
                setAreaId("");
                setFormError(null);
              }}
            >
              <option value="">No property</option>
              {(propsQ.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </label>

          {propertyId && (areasQ.data ?? []).length > 0 && (
            <label className="field">
              <span>Area (optional)</span>
              <select
                value={areaId}
                onChange={(e) => {
                  setAreaId(e.target.value);
                  setFormError(null);
                }}
              >
                <option value="">No area</option>
                {(areasQ.data ?? []).map((area) => (
                  <option key={area.id} value={area.id}>{area.name}</option>
                ))}
              </select>
            </label>
          )}

          <Checkbox
            checked={personal}
            onChange={(e) => {
              setPersonal(e.target.checked);
              setFormError(null);
            }}
            label="Keep this personal (only I can see it)"
          />

          {formError && <p className="form-error" role="alert">{formError}</p>}

          <div className="modal__actions">
            <button
              type="button"
              className="btn btn--ghost"
              disabled={create.isPending}
              onClick={() => ref.current?.close()}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn--moss"
              disabled={create.isPending || !title.trim()}
            >
              {create.isPending ? "Adding…" : "Add task"}
            </button>
          </div>
        </form>
      </dialog>
    </>
  );
}

function taskCreateErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const fieldMessages = error.fieldErrors
      .map((fieldError) => {
        const label = fieldErrorLabel(fieldError.loc);
        const message = fieldError.msg?.trim();
        if (!message) return null;
        return label ? `${label}: ${message}` : message;
      })
      .filter((message): message is string => Boolean(message));
    if (fieldMessages.length > 0) {
      return "Could not add task. " + fieldMessages.join(" ");
    }
    return (
      error.detail ??
      error.title ??
      error.message ??
      "Could not add task. Check the fields and try again."
    );
  }
  if (error instanceof Error && error.message) return error.message;
  return "Could not add task. Check the fields and try again.";
}

function fieldErrorLabel(loc: readonly (string | number)[] | undefined): string | null {
  const field = loc?.at(-1);
  if (field === "title") return "Title";
  if (field === "scheduled_for_local") return "Due date";
  if (field === "assigned_user_id") return "Assignee";
  if (field === "property_id") return "Property";
  if (field === "area_id") return "Area";
  return null;
}
