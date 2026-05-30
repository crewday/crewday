import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import {
  InlineDateField,
  InlineSearchableSelectField,
  InlineSelectField,
  InlineTableForm,
  InlineTextField,
  InlineTimeField,
  type InlineSelectOption,
  type InlineTableColumn,
  type InlineTableRow,
} from "@/components/InlineTableForm";
import { Avatar, Chip, EmptyState, Loading } from "@/components/common";
import {
  buildRecurrenceRrule,
  frequencyFromRecurrence,
  weekdayForDate,
  type RecurrenceFrequency,
  type RecurrenceWeekday,
} from "@/components/recurrence";
import type { Employee, Property, Schedule, TaskTemplate } from "@/types/api";
import { type ListEnvelope } from "@/lib/listResponse";

interface SchedulesPayload {
  data: Schedule[];
  next_cursor: string | null;
  has_more: boolean;
  templates_by_id: Record<string, TaskTemplate>;
}

type ScheduleFrequency = Extract<RecurrenceFrequency, "DAILY" | "WEEKLY" | "MONTHLY">;

interface ScheduleDraft {
  name: string;
  template_id: string;
  property_id: string;
  area_id: string;
  default_assignee: string;
  backup_assignee_user_ids: string[];
  active_from: string;
  starts_at: string;
  frequency: ScheduleFrequency;
  rrule: string;
  duration_minutes: number | null;
  rdate_local: string;
  exdate_local: string;
  active_until: string | null;
  paused: boolean;
}

interface ScheduleBody {
  name: string;
  template_id: string;
  property_id?: string;
  area_id?: string;
  default_assignee?: string;
  backup_assignee_user_ids: string[];
  rrule: string;
  dtstart_local: string;
  duration_minutes?: number | null;
  rdate_local: string;
  exdate_local: string;
  active_from: string;
  active_until: string | null;
}

const CREATE_ROW_ID = "__new_schedule__";

const FREQUENCY_OPTIONS: InlineSelectOption[] = [
  { value: "WEEKLY", label: "Weekly" },
  { value: "DAILY", label: "Daily" },
  { value: "MONTHLY", label: "Monthly" },
];

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

function datePart(localDateTime: string): string {
  return localDateTime.slice(0, 10);
}

function timePart(localDateTime: string): string {
  return localDateTime.slice(11, 16) || "09:00";
}

function weekdayFor(date: string): RecurrenceWeekday {
  return weekdayForDate(date);
}

function rruleFor(frequency: ScheduleFrequency, activeFrom: string): string {
  return buildRecurrenceRrule({
    frequency,
    byday: frequency === "WEEKLY" ? [weekdayFor(activeFrom)] : undefined,
  }, { includePrefix: true });
}

function frequencyFromRrule(rrule: string): ScheduleFrequency {
  const frequency = frequencyFromRecurrence(rrule);
  return frequency === "YEARLY" ? "WEEKLY" : frequency;
}

function templateOption(template: TaskTemplate) {
  return {
    value: template.id,
    label: template.name,
    secondaryText: `${template.duration_minutes} min`,
    searchText: [template.name, template.description_md].filter(Boolean).join(" "),
  };
}

function propertyOption(property: Property) {
  return {
    value: property.id,
    label: property.name,
    secondaryText: property.city || property.timezone,
    searchText: [property.name, property.city, property.timezone].filter(Boolean).join(" "),
  };
}

function employeeOption(employee: Employee) {
  return {
    value: employee.id,
    label: employee.name,
    secondaryText: employee.email || undefined,
    searchText: [employee.name, employee.email].filter(Boolean).join(" "),
  };
}

function scheduleErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const fieldMessages = error.fieldErrors
      .map((fieldError) => fieldError.msg?.trim())
      .filter((msg): msg is string => Boolean(msg));
    if (fieldMessages.length > 0) return fallback + " " + fieldMessages.join(" ");
    return error.detail ?? error.title ?? fallback;
  }
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

function emptyScheduleDraft(activeFrom: string): ScheduleDraft {
  return {
    name: "",
    template_id: "",
    property_id: "",
    area_id: "",
    default_assignee: "",
    backup_assignee_user_ids: [],
    active_from: activeFrom,
    starts_at: "09:00",
    frequency: "WEEKLY",
    rrule: rruleFor("WEEKLY", activeFrom),
    duration_minutes: null,
    rdate_local: "",
    exdate_local: "",
    active_until: null,
    paused: false,
  };
}

function draftFromSchedule(schedule: Schedule): ScheduleDraft {
  const activeFrom = schedule.active_from ?? datePart(schedule.dtstart_local);
  return {
    name: schedule.name,
    template_id: schedule.template_id,
    property_id: schedule.property_id ?? "",
    area_id: schedule.area_id ?? "",
    default_assignee: schedule.default_assignee_id ?? "",
    backup_assignee_user_ids: schedule.backup_assignee_user_ids,
    active_from: activeFrom,
    starts_at: timePart(schedule.dtstart_local),
    frequency: frequencyFromRrule(schedule.rrule),
    rrule: schedule.rrule,
    duration_minutes: schedule.duration_minutes,
    rdate_local: schedule.rdate_local,
    exdate_local: schedule.exdate_local,
    active_until: schedule.active_until,
    paused: schedule.paused,
  };
}

function validateScheduleDraft(
  draft: ScheduleDraft,
  templatesAvailable: boolean,
  templatesFailed: boolean,
  requireTemplateCatalog: boolean,
): string | null {
  if (requireTemplateCatalog && templatesFailed) return "Task templates could not be loaded. Reload before creating a schedule.";
  if (requireTemplateCatalog && !templatesAvailable) return "Create a task template before adding schedules.";
  if (!draft.name.trim()) return "Name is required.";
  if (!draft.template_id) return "Template is required.";
  if (!draft.active_from) return "Start date is required.";
  if (!draft.starts_at) return "Start time is required.";
  return null;
}

function bodyFromDraft(
  draft: ScheduleDraft,
  template: TaskTemplate,
  committedDraft?: ScheduleDraft,
): ScheduleBody {
  const recurrenceChanged = committedDraft
    ? draft.frequency !== committedDraft.frequency || draft.active_from !== committedDraft.active_from
    : true;
  const templateChanged = committedDraft ? draft.template_id !== committedDraft.template_id : true;
  return {
    name: draft.name.trim(),
    template_id: draft.template_id,
    property_id: draft.property_id || undefined,
    area_id: draft.area_id || undefined,
    default_assignee: draft.default_assignee || undefined,
    backup_assignee_user_ids: draft.backup_assignee_user_ids,
    rrule: recurrenceChanged ? rruleFor(draft.frequency, draft.active_from) : draft.rrule,
    dtstart_local: `${draft.active_from}T${draft.starts_at}:00`,
    duration_minutes: templateChanged ? template.duration_minutes : draft.duration_minutes,
    rdate_local: draft.rdate_local,
    exdate_local: draft.exdate_local,
    active_from: draft.active_from,
    active_until: draft.active_until,
  };
}

function scheduleActiveFrom(schedules: readonly Schedule[], row: InlineTableRow<ScheduleDraft>): string | null {
  const schedule = schedules.find((candidate) => candidate.id === row.id);
  if (schedule) return schedule.active_from;
  return row.draft.active_from || null;
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

export default function SchedulesPage() {
  const queryClient = useQueryClient();
  const tableShellRef = useRef<HTMLDivElement | null>(null);
  const today = todayLocal();
  const [createRow, setCreateRow] = useState<InlineTableRow<ScheduleDraft>>(() => makeCreateRow(today));
  const [rowEdits, setRowEdits] = useState<ReadonlyMap<string, InlineTableRow<ScheduleDraft>>>(() => new Map());

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

  const templates = templatesQ.data?.data ?? [];
  const templatesById = useMemo(() => {
    const byId = new Map(templates.map((template) => [template.id, template]));
    for (const template of Object.values(schedQ.data?.templates_by_id ?? {})) {
      if (!byId.has(template.id)) byId.set(template.id, template);
    }
    return byId;
  }, [schedQ.data?.templates_by_id, templates]);
  const editableTemplates = useMemo(() => Array.from(templatesById.values()), [templatesById]);

  const saveSchedule = useMutation({
    mutationFn: ({ rowId, draft }: { rowId: string; draft: ScheduleDraft }) => {
      const template = templatesById.get(draft.template_id);
      if (!template) throw new Error("Template is required.");
      const row = rowId === CREATE_ROW_ID ? createRow : rowEdits.get(rowId);
      const body = bodyFromDraft(draft, template, row?.committedDraft);
      if (rowId === CREATE_ROW_ID) {
        return fetchJson<Schedule>("/api/v1/tasks/schedules", { method: "POST", body });
      }
      return fetchJson<Schedule>(`/api/v1/tasks/schedules/${rowId}`, { method: "PATCH", body });
    },
    onSuccess: async (_saved, variables) => {
      if (variables.rowId === CREATE_ROW_ID) {
        setCreateRow(makeCreateRow(today));
      } else {
        setRowEdits((current) => clearMapValue(current, variables.rowId));
      }
      await queryClient.invalidateQueries({ queryKey: qk.schedules() });
    },
    onError: (error, variables) => {
      const message = scheduleErrorMessage(error, "Could not save schedule.");
      if (variables.rowId === CREATE_ROW_ID) {
        setCreateRow((current) => ({ ...current, error: message, saving: false }));
        return;
      }
      setRowEdits((current) => {
        const existing = current.get(variables.rowId);
        if (!existing) return current;
        return setMapValue(current, variables.rowId, { ...existing, error: message, saving: false });
      });
    },
  });

  const toggleSchedule = useMutation({
    mutationFn: ({ rowId, paused }: { rowId: string; paused: boolean }) => (
      fetchJson<Schedule>(`/api/v1/tasks/schedules/${rowId}/${paused ? "resume" : "pause"}`, { method: "POST" })
    ),
    onSuccess: async (_saved, variables) => {
      setRowEdits((current) => clearMapValue(current, variables.rowId));
      await queryClient.invalidateQueries({ queryKey: qk.schedules() });
    },
    onError: (error, variables) => {
      const message = scheduleErrorMessage(error, "Could not update schedule status.");
      setRowEdits((current) => {
        const existing = current.get(variables.rowId);
        if (existing) return setMapValue(current, variables.rowId, { ...existing, error: message });
        const schedule = schedQ.data?.data.find((candidate) => candidate.id === variables.rowId);
        if (!schedule) return current;
        const draft = draftFromSchedule(schedule);
        return setMapValue(current, variables.rowId, {
          id: variables.rowId,
          draft,
          committedDraft: draft,
          error: message,
        });
      });
    },
  });

  const sub = "\"Create a task from this template on these dates at these times.\" RRULE-backed, timezone-aware.";
  const actions = (
    <button
      type="button"
      className="btn btn--moss"
      onClick={focusCreateRow}
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
  const createTemplateOptions = templates.map(templateOption);
  const editTemplateOptions = editableTemplates.map(templateOption);
  const propertyOptions = propsQ.data.map(propertyOption);
  const assigneeOptions = empsQ.data.map(employeeOption);
  const templatesFailed = !templatesQ.data;
  const templatesAvailable = templates.length > 0;

  const columns: InlineTableColumn<ScheduleDraft>[] = [
    {
      key: "name",
      header: "Name",
      width: { flex: 1.35, min: 190 },
      renderRead: ({ row }) => (
        <>
          <strong>{row.draft.name}</strong>
          <div className="table__sub">
            since {fmtSince(scheduleActiveFrom(schedules, row) || null)}
          </div>
        </>
      ),
      renderEdit: ({ row, update, disabled }) => (
        <InlineTextField
          value={row.draft.name}
          placeholder="Weekly turnover"
          disabled={disabled}
          ariaLabel="Name"
          onChange={(name) => update({ name })}
        />
      ),
    },
    {
      key: "template",
      header: "Template",
      width: { flex: 1.1, min: 170 },
      renderRead: ({ row }) => templates_by_id[row.draft.template_id]?.name ?? templatesById.get(row.draft.template_id)?.name ?? "-",
      renderEdit: ({ row, update, disabled }) => (
        <InlineSearchableSelectField
          value={row.draft.template_id}
          options={row.isNew ? createTemplateOptions : editTemplateOptions}
          disabled={disabled || (row.isNew ? !templatesAvailable : editTemplateOptions.length === 0)}
          label="Template"
          placeholder="Select template"
          noResultsLabel="No templates"
          onChange={(template_id) => update({ template_id })}
        />
      ),
    },
    {
      key: "property",
      header: "Property",
      width: { flex: 1, min: 160 },
      renderRead: ({ row }) => {
        const property = row.draft.property_id ? propsById.get(row.draft.property_id) : undefined;
        return property ? <Chip tone={property.color} size="sm">{property.name}</Chip> : "-";
      },
      renderEdit: ({ row, update, disabled }) => (
        <InlineSearchableSelectField
          value={row.draft.property_id}
          options={propertyOptions}
          blankOption={{ label: "Any property" }}
          disabled={disabled}
          label="Property"
          onChange={(property_id) => update({ property_id })}
        />
      ),
    },
    {
      key: "assignee",
      header: "Assignee",
      width: { flex: 1, min: 160 },
      renderRead: ({ row }) => {
        const employee = row.draft.default_assignee ? empsById.get(row.draft.default_assignee) : undefined;
        return employee ? (
          <>
            <Avatar url={employee.avatar_url} initials={employee.avatar_initials} size="xs" alt={employee.name} />{" "}
            {employee.name.split(" ")[0]}
          </>
        ) : "-";
      },
      renderEdit: ({ row, update, disabled }) => (
        <InlineSearchableSelectField
          value={row.draft.default_assignee}
          options={assigneeOptions}
          blankOption={{ label: "Unassigned" }}
          disabled={disabled}
          label="Default assignee"
          onChange={(default_assignee) => update({ default_assignee })}
        />
      ),
    },
    {
      key: "active_from",
      header: "Starts",
      width: { px: 132 },
      renderRead: ({ row }) => <span className="mono">{row.draft.active_from || "-"}</span>,
      renderEdit: ({ row, update, disabled }) => (
        <InlineDateField
          value={row.draft.active_from}
          disabled={disabled}
          ariaLabel="Starts on"
          onChange={(active_from) => update({ active_from })}
        />
      ),
    },
    {
      key: "starts_at",
      header: "Time",
      width: { px: 108 },
      renderRead: ({ row }) => <span className="mono">{row.draft.starts_at || "-"}</span>,
      renderEdit: ({ row, update, disabled }) => (
        <InlineTimeField
          value={row.draft.starts_at}
          disabled={disabled}
          ariaLabel="Start time"
          onChange={(starts_at) => update({ starts_at })}
        />
      ),
    },
    {
      key: "frequency",
      header: "Repeats",
      width: { px: 128 },
      renderRead: ({ row }) => schedules.find((schedule) => schedule.id === row.id)?.rrule_human ?? row.draft.frequency.toLowerCase(),
      // Schedules split recurrence across start date, start time, and frequency,
      // and the API owns the human summary. Reuse shared RRULE helpers here;
      // the full picker fits checklist-row RRULE editing, not this schedule row.
      renderEdit: ({ row, update, disabled }) => (
        <InlineSelectField
          value={row.draft.frequency}
          options={FREQUENCY_OPTIONS}
          disabled={disabled}
          ariaLabel="Repeats"
          onChange={(frequency) => update({ frequency: frequency as ScheduleFrequency })}
        />
      ),
    },
    {
      key: "duration",
      header: "Duration",
      width: { px: 104 },
      className: "mono",
      renderRead: ({ row }) => {
        const schedule = schedules.find((candidate) => candidate.id === row.id);
        const template = templates_by_id[row.draft.template_id] ?? templatesById.get(row.draft.template_id);
        const duration = schedule?.duration_minutes ?? template?.duration_minutes;
        return duration ? `${duration} min` : "-";
      },
      renderEdit: ({ row }) => {
        const template = templates_by_id[row.draft.template_id] ?? templatesById.get(row.draft.template_id);
        return template ? `${template.duration_minutes} min` : "-";
      },
    },
    {
      key: "status",
      header: "Status",
      width: { px: 128 },
      renderRead: ({ row }) => (
        <ScheduleStatusCell
          row={row}
          disabled={toggleSchedule.isPending && toggleSchedule.variables?.rowId === row.id}
          onToggle={() => toggleSchedule.mutate({ rowId: row.id, paused: row.draft.paused })}
        />
      ),
      renderEdit: ({ row }) => (
        <Chip tone={row.draft.paused ? "sand" : "moss"} size="sm">
          {row.draft.paused ? "Paused" : "Active"}
        </Chip>
      ),
    },
  ];

  const rows: InlineTableRow<ScheduleDraft>[] = schedules.map((schedule) => {
    const baseDraft = draftFromSchedule(schedule);
    const localRow = rowEdits.get(schedule.id);
    return {
      id: schedule.id,
      label: schedule.name,
      draft: localRow?.draft ?? baseDraft,
      committedDraft: baseDraft,
      editing: localRow?.editing ?? false,
      dirty: localRow?.dirty ?? false,
      saving: saveSchedule.isPending && saveSchedule.variables?.rowId === schedule.id,
      error: localRow?.error,
      validation: localRow?.validation,
    };
  });

  const activeCreateRow: InlineTableRow<ScheduleDraft> = {
    ...createRow,
    disabled: templatesFailed || !templatesAvailable,
    saving: saveSchedule.isPending && saveSchedule.variables?.rowId === CREATE_ROW_ID,
    validation: createRow.validation ?? (
      templatesFailed || !templatesAvailable
        ? validateScheduleDraft(createRow.draft, templatesAvailable, templatesFailed, true)
        : undefined
    ),
    meta: schedules.length === 0 && createRow.dirty === false && templatesAvailable ? (
      <EmptyState
        title="No schedules yet"
        copy="Use the trailing row to add recurring work."
        variant="compact"
      />
    ) : createRow.meta,
  };

  return (
    <DeskPage title="Schedules" sub={sub} actions={actions}>
      <div className="panel" ref={tableShellRef}>
        <InlineTableForm
          ariaLabel="Schedules"
          columns={columns}
          rows={rows}
          trailingCreateRow={activeCreateRow}
          saveMode="explicit"
          onDraftChange={patchRow}
          onEdit={editRow}
          onCancel={cancelRow}
          onSave={saveRow}
          getRowLabel={(row) => (row.label ?? row.draft.name) || "New schedule"}
        />
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

  function makeCreateRow(activeFrom: string): InlineTableRow<ScheduleDraft> {
    return {
      id: CREATE_ROW_ID,
      label: "New schedule",
      draft: emptyScheduleDraft(activeFrom),
      committedDraft: emptyScheduleDraft(activeFrom),
      editing: true,
      isNew: true,
    };
  }

  function focusCreateRow(): void {
    setCreateRow((row) => ({
      ...row,
      dirty: true,
      validation: undefined,
      error: undefined,
      meta: undefined,
    }));
    const row = tableShellRef.current?.querySelector<HTMLElement>(
      `[data-inline-table-row-group="${CREATE_ROW_ID}"]`,
    );
    row?.scrollIntoView?.({ block: "nearest" });
    row?.querySelector<HTMLElement>("input, select, button")?.focus();
  }

  function patchRow(rowId: string, patch: Partial<ScheduleDraft>): void {
    if (rowId === CREATE_ROW_ID) {
      setCreateRow((row) => ({
        ...row,
        draft: { ...row.draft, ...patch },
        dirty: true,
        validation: undefined,
        error: undefined,
        meta: undefined,
      }));
      return;
    }

    const schedule = schedules.find((candidate) => candidate.id === rowId);
    if (!schedule) return;
    setRowEdits((current) => {
      const existing = current.get(rowId);
      const committedDraft = existing?.committedDraft ?? draftFromSchedule(schedule);
      const nextRow: InlineTableRow<ScheduleDraft> = {
        id: rowId,
        label: schedule.name,
        draft: { ...(existing?.draft ?? committedDraft), ...patch },
        committedDraft,
        editing: true,
        dirty: true,
        validation: undefined,
        error: undefined,
      };
      return setMapValue(current, rowId, nextRow);
    });
  }

  function editRow(rowId: string): void {
    const schedule = schedules.find((candidate) => candidate.id === rowId);
    if (!schedule) return;
    setRowEdits((current) => {
      const existing = current.get(rowId);
      const committedDraft = draftFromSchedule(schedule);
      return setMapValue(current, rowId, {
        id: rowId,
        label: schedule.name,
        draft: existing?.draft ?? committedDraft,
        committedDraft,
        editing: true,
        dirty: existing?.dirty ?? false,
        validation: existing?.validation,
        error: existing?.error,
      });
    });
  }

  function cancelRow(rowId: string): void {
    if (rowId === CREATE_ROW_ID) {
      setCreateRow(makeCreateRow(today));
      return;
    }
    setRowEdits((current) => clearMapValue(current, rowId));
  }

  function saveRow(rowId: string): void {
    const row = rowId === CREATE_ROW_ID ? createRow : rowEdits.get(rowId);
    if (!row) return;
    const validation = validateScheduleDraft(row.draft, templatesAvailable, templatesFailed, rowId === CREATE_ROW_ID);
    if (validation) {
      if (rowId === CREATE_ROW_ID) {
        setCreateRow((current) => ({ ...current, dirty: true, validation }));
        return;
      }
      setRowEdits((current) => {
        const existing = current.get(rowId);
        if (!existing) return current;
        return setMapValue(current, rowId, { ...existing, validation });
      });
      return;
    }
    saveSchedule.mutate({ rowId, draft: row.draft });
  }
}

function ScheduleStatusCell({
  row,
  disabled,
  onToggle,
}: {
  row: InlineTableRow<ScheduleDraft>;
  disabled: boolean;
  onToggle: () => void;
}) {
  if (row.isNew) {
    return <Chip tone="ghost" size="sm">Draft</Chip>;
  }
  return (
    <button
      type="button"
      className="btn btn--ghost btn--sm"
      disabled={disabled}
      onClick={(event) => {
        event.stopPropagation();
        onToggle();
      }}
    >
      {row.draft.paused ? "Resume" : "Pause"}
    </button>
  );
}
