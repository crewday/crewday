import { useEffect, useMemo, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fetchJson } from "@/lib/api";
import { type ListEnvelope, unwrapList } from "@/lib/listResponse";
import { qk } from "@/lib/queryKeys";
import type { Me, Property, Task } from "@/types/api";
import FormModal, { FormModalField } from "@/components/FormModal";
import { Checkbox } from "@/components/common";
import SearchableSelect, { type SearchableSelectOption } from "@/components/SearchableSelect";
import { usePatchReducer } from "@/lib/usePatchReducer";

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

interface NewTaskFormState {
  title: string;
  due: string;
  propertyId: string;
  areaId: string;
  personal: boolean;
  formError: string | null;
  open: boolean;
}

export default function NewTaskButton() {
  // code-health: ignore[ccn nloc] Quick-add task dialog keeps compact form state, validation, and mutation beside the button that opens it.
  const qc = useQueryClient();
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const meQ = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });
  const systemTodayIso = new Date().toISOString().slice(0, 10);
  const defaultDue = meQ.data?.today ?? systemTodayIso;
  const [form, setForm] = usePatchReducer<NewTaskFormState>(() => ({
    title: "",
    due: defaultDue,
    propertyId: "",
    areaId: "",
    personal: true,
    formError: null,
    open: false,
  }));
  const { title, due, propertyId, areaId, personal, formError, open } = form;
  const areasQ = useQuery({
    queryKey: qk.propertyAreas(propertyId),
    queryFn: () =>
      fetchJson<ListEnvelope<Area>>("/api/v1/properties/" + propertyId + "/areas").then(unwrapList),
    enabled: Boolean(propertyId),
  });

  const submitLocked = useRef(false);
  const propertyOptions = useMemo(
    () => (propsQ.data ?? []).map(propertyOption),
    [propsQ.data],
  );
  const areaOptions = useMemo(
    () => (areasQ.data ?? []).map(areaOption),
    [areasQ.data],
  );

  useEffect(() => {
    setForm((current) => (
      current.due === systemTodayIso ? { ...current, due: defaultDue } : current
    ));
  }, [defaultDue, systemTodayIso]);

  const reset = () => {
    setForm({
      title: "",
      due: defaultDue,
      propertyId: "",
      areaId: "",
      personal: true,
      formError: null,
    });
  };

  const create = useMutation({
    mutationFn: (payload: NewTaskBody) =>
      fetchJson<Task>("/api/v1/tasks", { method: "POST", body: payload }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.today() });
      qc.invalidateQueries({ queryKey: qk.week() });
      qc.invalidateQueries({ queryKey: qk.tasks() });
      setForm({ open: false });
      reset();
    },
    onError: (error) => {
      setForm({ formError: taskCreateErrorMessage(error) });
    },
    onSettled: () => {
      submitLocked.current = false;
    },
  });

  const currentUserId = meQ.data?.user_id ?? "";
  const closeForm = () => {
    if (submitLocked.current || create.isPending) return;
    setForm({ open: false });
    reset();
  };
  const handleDialogClose = () => {
    setForm({ open: false });
    reset();
  };

  return (
    <>
      <button
        type="button"
        className="btn btn--moss"
        onClick={() => setForm({ open: true })}
      >
        + New task
      </button>

      <FormModal
        open={open}
        title="New task"
        eyebrow="Quick add"
        subtitle={
          personal
            ? "Personal - only you can see this."
            : "Team task - visible to your manager."
        }
        formClassName="new-task-form"
        onClose={handleDialogClose}
        onCancel={(e) => {
          if (submitLocked.current || create.isPending) e.preventDefault();
        }}
        closeDisabled={create.isPending}
        onSubmit={(e) => {
          e.preventDefault();
          if (submitLocked.current || create.isPending) return;
          const trimmed = title.trim();
          if (!trimmed) return;
          if (!due) {
            setForm({ formError: "Choose a due date before adding the task." });
            return;
          }
          if (personal && !currentUserId) {
            setForm({ formError: "Your profile is still loading. Wait a moment and try again." });
            return;
          }
          submitLocked.current = true;
          setForm({ formError: null });
          create.mutate({
            title: trimmed,
            scheduled_for_local: due + "T09:00:00",
            property_id: propertyId || undefined,
            area_id: areaId || undefined,
            assigned_user_id: personal ? currentUserId : undefined,
            is_personal: personal,
          });
        }}
        actions={
          <>
            <button
              type="button"
              className="btn btn--ghost"
              disabled={create.isPending}
              onClick={closeForm}
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
          </>
        }
      >
        <FormModalField label="Title" requirement="required" className="new-task-form__field">
          <input
            required
            value={title}
            onChange={(e) => {
              setForm({ title: e.target.value, formError: null });
            }}
            placeholder="e.g. Call back Maria about the stay"
           aria-label="Title"/>
        </FormModalField>

        <FormModalField label="Due" requirement="required" className="new-task-form__field">
          <input
            type="date"
            required
            value={due}
            onChange={(e) => {
              setForm({ due: e.target.value, formError: null });
            }}
           aria-label="Due"/>
        </FormModalField>

        <SearchableSelect
          label="Property"
          requirement="optional"
          className="form-modal__field new-task-form__field"
          value={propertyId}
          options={propertyOptions}
          blankOption={{ label: "No property" }}
          onChange={(value) => {
            setForm({ propertyId: value, areaId: "", formError: null });
          }}
        />

        {propertyId && areaOptions.length > 0 && (
          <SearchableSelect
            label="Area"
            requirement="optional"
            className="form-modal__field new-task-form__field"
            value={areaId}
            options={areaOptions}
            blankOption={{ label: "No area" }}
            onChange={(value) => {
              setForm({ areaId: value, formError: null });
            }}
          />
        )}

        <Checkbox
          checked={personal}
          onChange={(e) => {
            setForm({ personal: e.target.checked, formError: null });
          }}
          label="Keep this personal (only I can see it)"
        />

        {formError && <p className="form-error" role="alert">{formError}</p>}
      </FormModal>
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

function propertyOption(property: Property): SearchableSelectOption {
  return {
    value: property.id,
    label: property.name,
    secondaryText: property.city || property.timezone,
    searchText: [property.name, property.city, property.timezone].filter(Boolean).join(" "),
  };
}

function areaOption(area: Area): SearchableSelectOption {
  return {
    value: area.id,
    label: area.name,
  };
}
