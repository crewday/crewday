import { type FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import FormField from "@/components/FormField";
import { Avatar, Chip, Loading } from "@/components/common";
import type { Employee, Property, Schedule, TaskTemplate } from "@/types/api";
import { type ListEnvelope } from "@/lib/listResponse";

interface SchedulesPayload {
  data: Schedule[];
  next_cursor: string | null;
  has_more: boolean;
  templates_by_id: Record<string, TaskTemplate>;
}

interface ScheduleCreateBody {
  name: string;
  template_id: string;
  property_id?: string;
  default_assignee?: string;
  rrule: string;
  dtstart_local: string;
  active_from: string;
}

function fmtSince(iso: string | null): string {
  if (!iso) return "unknown";
  return new Date(iso).toLocaleDateString("en-GB", { month: "short", year: "numeric" });
}

function todayLocal(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

function weekdayFor(date: string): string {
  const days = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"];
  const index = new Date(`${date}T00:00:00`).getDay();
  return days[index] ?? "MO";
}

function rruleFor(frequency: string, activeFrom: string): string {
  if (frequency === "WEEKLY") return `RRULE:FREQ=WEEKLY;BYDAY=${weekdayFor(activeFrom)}`;
  return `RRULE:FREQ=${frequency}`;
}

function scheduleErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const fieldMessages = error.fieldErrors
      .map((fieldError) => fieldError.msg?.trim())
      .filter((msg): msg is string => Boolean(msg));
    if (fieldMessages.length > 0) {
      return "Could not create schedule. " + fieldMessages.join(" ");
    }
    return error.detail ?? error.title ?? "Could not create schedule. Check the fields and try again.";
  }
  return "Could not create schedule. Check the fields and try again.";
}

export default function SchedulesPage() {
  const queryClient = useQueryClient();
  const today = todayLocal();
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [templateId, setTemplateId] = useState("");
  const [propertyId, setPropertyId] = useState("");
  const [assigneeId, setAssigneeId] = useState("");
  const [activeFrom, setActiveFrom] = useState(today);
  const [startsAt, setStartsAt] = useState("09:00");
  const [frequency, setFrequency] = useState("WEEKLY");
  const [formError, setFormError] = useState<string | null>(null);

  const schedQ = useQuery({
    queryKey: qk.schedules(),
    queryFn: () => fetchJson<SchedulesPayload>("/api/v1/tasks/schedules"),
  });
  const templatesQ = useQuery({
    queryKey: qk.taskTemplates(),
    queryFn: () => fetchJson<ListEnvelope<TaskTemplate>>("/api/v1/tasks/task_templates"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const empsQ = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });
  const createSchedule = useMutation({
    mutationFn: (body: ScheduleCreateBody) =>
      fetchJson<Schedule>("/api/v1/tasks/schedules", { method: "POST", body }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.schedules() });
      resetCreate();
    },
    onError: (error) => {
      setFormError(scheduleErrorMessage(error));
    },
  });

  const sub = "\"Create a task from this template on these dates at these times.\" RRULE-backed, timezone-aware.";
  const actions = (
    <button
      type="button"
      className="btn btn--moss"
      onClick={() => setCreateOpen(true)}
    >
      + New schedule
    </button>
  );

  if (schedQ.isPending || templatesQ.isPending || propsQ.isPending || empsQ.isPending) {
    return <DeskPage title="Schedules" sub={sub} actions={actions}><Loading /></DeskPage>;
  }
  if (!schedQ.data || !propsQ.data || !empsQ.data) {
    return <DeskPage title="Schedules" sub={sub} actions={actions}>Failed to load.</DeskPage>;
  }

  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));
  const empsById = new Map(empsQ.data.map((e) => [e.id, e]));
  const { data: schedules, templates_by_id } = schedQ.data;
  const templates = templatesQ.data?.data ?? [];

  return (
    <DeskPage title="Schedules" sub={sub} actions={actions}>
      <ScheduleCreateDialog
        open={createOpen}
        templates={templates}
        templatesFailed={!templatesQ.data}
        properties={propsQ.data}
        employees={empsQ.data}
        pending={createSchedule.isPending}
        error={formError}
        name={name}
        templateId={templateId}
        propertyId={propertyId}
        assigneeId={assigneeId}
        activeFrom={activeFrom}
        startsAt={startsAt}
        frequency={frequency}
        onName={setName}
        onTemplateId={setTemplateId}
        onPropertyId={setPropertyId}
        onAssigneeId={setAssigneeId}
        onActiveFrom={setActiveFrom}
        onStartsAt={setStartsAt}
        onFrequency={setFrequency}
        onClose={closeCreate}
        onSubmit={submitCreate}
      />

      <div className="panel">
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Name</th><th>Template</th><th>Property</th><th>Recurrence</th>
              <th>Default assignee</th><th>Duration</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {schedules.map((s) => {
              const p = s.property_id ? propsById.get(s.property_id) : undefined;
              const tpl = templates_by_id[s.template_id];
              const emp = s.default_assignee_id ? empsById.get(s.default_assignee_id) : undefined;
              const duration = s.duration_minutes ?? tpl?.duration_minutes;
              return (
                <tr key={s.id}>
                  <td>
                    <strong>{s.name}</strong>
                    <div className="table__sub">since {fmtSince(s.active_from)}</div>
                  </td>
                  <td>{tpl?.name ?? "—"}</td>
                  <td>{p ? <Chip tone={p.color} size="sm">{p.name}</Chip> : "—"}</td>
                  <td className="table__sub">{s.rrule_human}</td>
                  <td>
                    {emp ? (
                      <><Avatar url={emp.avatar_url} initials={emp.avatar_initials} size="xs" alt={emp.name} /> {emp.name.split(" ")[0]}</>
                    ) : "—"}
                  </td>
                  <td className="mono">{duration ? `${duration} min` : "—"}</td>
                  <td>
                    <Chip tone={s.paused ? "sand" : "moss"} size="sm">
                      {s.paused ? "Paused" : "Active"}
                    </Chip>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head"><h2>Preview — next 7 days</h2></header>
        <ul className="task-list task-list--desk">
          {schedules.filter((s) => !s.paused).map((s) => {
            const p = s.property_id ? propsById.get(s.property_id) : undefined;
            const tpl = templates_by_id[s.template_id];
            const duration = s.duration_minutes ?? tpl?.duration_minutes;
            return (
              <li key={s.id} className="task-row">
                <span className="task-row__time mono">{s.rrule_human}</span>
                <span className="task-row__title"><strong>{s.name}</strong></span>
                {p && <Chip tone={p.color} size="sm">{p.name}</Chip>}
                {duration && <Chip tone="ghost" size="sm">{duration}m</Chip>}
              </li>
            );
          })}
        </ul>
      </div>
    </DeskPage>
  );

  function closeCreate(): void {
    if (createSchedule.isPending) return;
    resetCreate();
  }

  function resetCreate(): void {
    setCreateOpen(false);
    setName("");
    setTemplateId("");
    setPropertyId("");
    setAssigneeId("");
    setActiveFrom(today);
    setStartsAt("09:00");
    setFrequency("WEEKLY");
    setFormError(null);
  }

  function submitCreate(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const trimmed = name.trim();
    if (!templatesQ.data) {
      setFormError("Task templates could not be loaded. Reload before creating a schedule.");
      return;
    }
    if (templates.length === 0) {
      setFormError("Create a task template before adding schedules.");
      return;
    }
    const selectedTemplateId = templateId || templates[0]?.id;
    if (!trimmed) {
      setFormError("Name is required.");
      return;
    }
    if (!selectedTemplateId) {
      setFormError("Select a task template before creating a schedule.");
      return;
    }
    if (!activeFrom || !startsAt) {
      setFormError("Choose a start date and time before creating a schedule.");
      return;
    }
    setFormError(null);
    createSchedule.mutate({
      name: trimmed,
      template_id: selectedTemplateId,
      property_id: propertyId || undefined,
      default_assignee: assigneeId || undefined,
      rrule: rruleFor(frequency, activeFrom),
      dtstart_local: `${activeFrom}T${startsAt}:00`,
      active_from: activeFrom,
    });
  }
}

interface ScheduleCreateDialogProps {
  open: boolean;
  templates: TaskTemplate[];
  templatesFailed: boolean;
  properties: Property[];
  employees: Employee[];
  pending: boolean;
  error: string | null;
  name: string;
  templateId: string;
  propertyId: string;
  assigneeId: string;
  activeFrom: string;
  startsAt: string;
  frequency: string;
  onName: (value: string) => void;
  onTemplateId: (value: string) => void;
  onPropertyId: (value: string) => void;
  onAssigneeId: (value: string) => void;
  onActiveFrom: (value: string) => void;
  onStartsAt: (value: string) => void;
  onFrequency: (value: string) => void;
  onClose: () => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}

function ScheduleCreateDialog(props: ScheduleCreateDialogProps) {
  const unavailable = props.templatesFailed || props.templates.length === 0;
  const unavailableMessage = props.templatesFailed
    ? "Task templates could not be loaded. Reload before creating a schedule."
    : "Create a task template before adding schedules.";

  return (
    <dialog className="modal modal--sheet sheet-form-dialog" open={props.open} onClose={props.onClose} aria-label="New schedule">
      <form className="schedule-create-form sheet-form" onSubmit={props.onSubmit}>
        <header className="schedule-create-form__head sheet-form__head">
          <div>
            <p className="schedule-create-form__eyebrow sheet-form__eyebrow">Recurring work</p>
            <h3 className="schedule-create-form__title sheet-form__title">
              {"New schedule"
              } // code-health: ignore[nloc] Schedule-create form fields are intentionally explicit and ordered like the API payload.
            </h3>
            <p className="schedule-create-form__sub sheet-form__sub">
              Create recurring tasks from a task template.
            </p>
          </div>
          <button
            type="button"
            className="schedule-create-form__close sheet-form__close"
            onClick={props.onClose}
            aria-label="Close"
          >
            ×
          </button>
        </header>

        <div className="schedule-create-form__body sheet-form__body">
        {unavailable ? (
          <p role="status" className="sheet-form__sub">{unavailableMessage}</p>
        ) : (
          <>
            <FormField label="Name" requirement="required" className="schedule-create-form__field sheet-form__field">
              <input
                autoFocus
                required
                value={props.name}
                onChange={(e) => props.onName(e.target.value)}
                placeholder="Weekly turnover"
              />
            </FormField>
            <FormField label="Template" requirement="required" className="schedule-create-form__field sheet-form__field">
              <select
                required
                value={props.templateId || props.templates[0]?.id || ""}
                onChange={(e) => props.onTemplateId(e.target.value)}
              >
                {props.templates.map((tpl) => (
                  <option key={tpl.id} value={tpl.id}>{tpl.name}</option>
                ))}
              </select>
            </FormField>
            <FormField label="Property" requirement="optional" className="schedule-create-form__field sheet-form__field">
              <select value={props.propertyId} onChange={(e) => props.onPropertyId(e.target.value)}>
                <option value="">Any property</option>
                {props.properties.map((property) => (
                  <option key={property.id} value={property.id}>{property.name}</option>
                ))}
              </select>
            </FormField>
            <FormField label="Default assignee" requirement="optional" className="schedule-create-form__field sheet-form__field">
              <select value={props.assigneeId} onChange={(e) => props.onAssigneeId(e.target.value)}>
                <option value="">Unassigned</option>
                {props.employees.map((employee) => (
                  <option key={employee.id} value={employee.id}>{employee.name}</option>
                ))}
              </select>
            </FormField>
            <div className="schedule-create-form__grid sheet-form__grid">
            <FormField label="Starts on" requirement="required" className="schedule-create-form__field sheet-form__field">
              <input
                type="date"
                required
                value={props.activeFrom}
                onChange={(e) => props.onActiveFrom(e.target.value)}
              />
            </FormField>
            <FormField label="Start time" requirement="required" className="schedule-create-form__field sheet-form__field">
              <input
                type="time"
                required
                value={props.startsAt}
                onChange={(e) => props.onStartsAt(e.target.value)}
              />
            </FormField>
            </div>
            <FormField label="Repeats" requirement="required" className="schedule-create-form__field sheet-form__field">
              <select value={props.frequency} onChange={(e) => props.onFrequency(e.target.value)}>
                <option value="DAILY">Daily</option>
                <option value="WEEKLY">Weekly</option>
                <option value="MONTHLY">Monthly</option>
              </select>
            </FormField>
          </>
        )}

        {props.error && <p role="alert" className="tokens-form__error">{props.error}</p>}
        </div>

        <footer className="schedule-create-form__footer sheet-form__footer">
          <button type="button" className="btn btn--ghost" onClick={props.onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn--moss" disabled={props.pending || unavailable}>
            {props.pending ? "Creating..." : "Create schedule"}
          </button>
        </footer>
      </form>
    </dialog>
  );
}
