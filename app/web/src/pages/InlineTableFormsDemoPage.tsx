import { useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, MutableRefObject, ReactNode, SetStateAction } from "react";
import { CheckCircle2, RotateCcw } from "lucide-react";
import {
  InlineCheckboxField,
  InlineDateField,
  InlineNoteField,
  InlineSelectField,
  InlineTableForm,
  type InlineTableColumn,
  type InlineTableRow,
  InlineTextField,
} from "@/components/InlineTableForm";

type Priority = "low" | "normal" | "high";

interface TaskDraft {
  title: string;
  assignee: string;
  property: string;
  due: string;
  priority: Priority;
  note: string;
}

interface ChecklistDraft {
  done: boolean;
  item: string;
  owner: string;
  phase: string;
}

interface AssignmentDraft {
  window: string;
  team: string;
  property: string;
  backup: string;
}

interface DefaultDraft {
  item: string;
  owner: string;
  due: string;
  state: string;
}

const assignees = [
  { value: "", label: "Unassigned" },
  { value: "maria", label: "Maria" },
  { value: "enzo", label: "Enzo" },
  { value: "sora", label: "Sora" },
];

const properties = [
  { value: "villa-sud", label: "Villa Sud" },
  { value: "loft-north", label: "Loft North" },
  { value: "harbor-flat", label: "Harbor Flat" },
];

const priorities = [
  { value: "low", label: "Low" },
  { value: "normal", label: "Normal" },
  { value: "high", label: "High" },
];

const phases = [
  { value: "walkthrough", label: "Walkthrough" },
  { value: "prep", label: "Prep" },
  { value: "handoff", label: "Handoff" },
];

const defaultStates = [
  { value: "planned", label: "Planned" },
  { value: "active", label: "Active" },
  { value: "blocked", label: "Blocked" },
];

let demoRowCounter = 0;

function nextDemoRowId(prefix: string) {
  demoRowCounter += 1;
  return `${prefix}-${Date.now()}-${demoRowCounter}`;
}

export default function InlineTableFormsDemoPage() {
  const [tasks, setTasks] = useState<InlineTableRow<TaskDraft>[]>(initialTasks);
  const [checklist, setChecklist] = useState<InlineTableRow<ChecklistDraft>[]>(initialChecklist);
  const [assignments, setAssignments] = useState<InlineTableRow<AssignmentDraft>[]>(initialAssignments);
  const [defaultRows, setDefaultRows] = useState<InlineTableRow<DefaultDraft>[]>(initialDefaultRows);
  const assignmentSaveTimers = useRef<number[]>([]);

  useEffect(() => () => {
    assignmentSaveTimers.current.forEach((timer) => window.clearTimeout(timer));
    assignmentSaveTimers.current = [];
  }, []);

  const taskColumns = useMemo<InlineTableColumn<TaskDraft>[]>(() => [
    {
      key: "title",
      header: "Task",
      width: { flex: 1.8, min: 220 },
      renderRead: ({ row }) => <ReadText value={row.draft.title} fallback="Untitled task" />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineTextField
          value={row.draft.title}
          disabled={disabled}
          ariaLabel="Task title"
          placeholder="Add a task title"
          onChange={(title) => update({ title })}
        />
      ),
    },
    {
      key: "assignee",
      header: "Assignee",
      width: { min: 130 },
      renderRead: ({ row }) => <ReadText value={labelFor(assignees, row.draft.assignee)} fallback="Unassigned" />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineSelectField
          value={row.draft.assignee}
          options={assignees}
          disabled={disabled}
          ariaLabel="Task assignee"
          onChange={(assignee) => update({ assignee })}
        />
      ),
    },
    {
      key: "property",
      header: "Property",
      width: { min: 130 },
      renderRead: ({ row }) => <ReadText value={labelFor(properties, row.draft.property)} />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineSelectField
          value={row.draft.property}
          options={properties}
          disabled={disabled}
          ariaLabel="Task property"
          onChange={(property) => update({ property })}
        />
      ),
    },
    {
      key: "due",
      header: "Due",
      width: { flex: 0.85, min: 122 },
      renderRead: ({ row }) => <ReadText value={row.draft.due} />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineDateField
          value={row.draft.due}
          disabled={disabled}
          ariaLabel="Task due date"
          onChange={(due) => update({ due })}
        />
      ),
    },
    {
      key: "priority",
      header: "Priority",
      width: { flex: 0.75, min: 112 },
      renderRead: ({ row }) => <PriorityChip priority={row.draft.priority} />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineSelectField
          value={row.draft.priority}
          options={priorities}
          disabled={disabled}
          ariaLabel="Task priority"
          onChange={(priority) => update({ priority: priority as Priority })}
        />
      ),
    },
  ], []);

  const checklistColumns = useMemo<InlineTableColumn<ChecklistDraft>[]>(() => [
    {
      key: "done",
      header: "Done",
      mobileLabel: "Done",
      width: { px: 112 },
      align: "center",
      renderRead: ({ row }) => row.draft.done ? <CheckCircle2 size={18} aria-label="Done" /> : <span className="inline-table-form__read--muted">Open</span>,
      renderEdit: ({ row, update, disabled }) => (
        <InlineCheckboxField
          checked={row.draft.done}
          disabled={disabled}
          label="Complete"
          onChange={(done) => update({ done })}
        />
      ),
    },
    {
      key: "item",
      header: "Checklist item",
      width: { flex: 1.9, min: 240 },
      renderRead: ({ row }) => <ReadText value={row.draft.item} />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineTextField
          value={row.draft.item}
          disabled={disabled}
          ariaLabel="Checklist item"
          onChange={(item) => update({ item })}
        />
      ),
    },
    {
      key: "owner",
      header: "Owner",
      width: { min: 130 },
      renderRead: ({ row }) => <ReadText value={labelFor(assignees, row.draft.owner)} />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineSelectField
          value={row.draft.owner}
          options={assignees}
          disabled={disabled}
          ariaLabel="Checklist owner"
          onChange={(owner) => update({ owner })}
        />
      ),
    },
    {
      key: "phase",
      header: "Phase",
      width: { min: 130 },
      renderRead: ({ row }) => <ReadText value={labelFor(phases, row.draft.phase)} />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineSelectField
          value={row.draft.phase}
          options={phases}
          disabled={disabled}
          ariaLabel="Checklist phase"
          onChange={(phase) => update({ phase })}
        />
      ),
    },
  ], []);

  const assignmentColumns = useMemo<InlineTableColumn<AssignmentDraft>[]>(() => [
    {
      key: "window",
      header: "Window",
      width: { flex: 0.8, min: 130 },
      renderRead: ({ row }) => <ReadText value={row.draft.window} />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineTextField
          value={row.draft.window}
          disabled={disabled}
          ariaLabel="Assignment window"
          onChange={(window) => update({ window })}
        />
      ),
    },
    {
      key: "team",
      header: "Team",
      width: { flex: 1.2, min: 170 },
      renderRead: ({ row }) => <ReadText value={row.draft.team} />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineTextField
          value={row.draft.team}
          disabled={disabled}
          ariaLabel="Assignment team"
          onChange={(team) => update({ team })}
        />
      ),
    },
    {
      key: "property",
      header: "Property",
      width: { min: 140 },
      renderRead: ({ row }) => <ReadText value={labelFor(properties, row.draft.property)} />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineSelectField
          value={row.draft.property}
          options={properties}
          disabled={disabled}
          ariaLabel="Assignment property"
          onChange={(property) => update({ property })}
        />
      ),
    },
    {
      key: "backup",
      header: "Backup",
      width: { min: 150 },
      renderRead: ({ row }) => <ReadText value={row.draft.backup} />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineTextField
          value={row.draft.backup}
          disabled={disabled}
          ariaLabel="Assignment backup"
          onChange={(backup) => update({ backup })}
        />
      ),
    },
  ], []);

  const defaultColumns = useMemo<InlineTableColumn<DefaultDraft>[]>(() => [
    {
      key: "item",
      header: "Item",
      renderRead: ({ row }) => <ReadText value={row.draft.item} fallback="Untitled" />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineTextField
          value={row.draft.item}
          disabled={disabled}
          ariaLabel="Default item"
          placeholder="Add item"
          onChange={(item) => update({ item })}
        />
      ),
    },
    {
      key: "owner",
      header: "Owner",
      renderRead: ({ row }) => <ReadText value={labelFor(assignees, row.draft.owner)} fallback="Unassigned" />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineSelectField
          value={row.draft.owner}
          options={assignees}
          disabled={disabled}
          ariaLabel="Default owner"
          onChange={(owner) => update({ owner })}
        />
      ),
    },
    {
      key: "due",
      header: "Due",
      renderRead: ({ row }) => <ReadText value={row.draft.due} />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineDateField
          value={row.draft.due}
          disabled={disabled}
          ariaLabel="Default due date"
          onChange={(due) => update({ due })}
        />
      ),
    },
    {
      key: "state",
      header: "State",
      renderRead: ({ row }) => <ReadText value={row.draft.state} />,
      renderEdit: ({ row, update, disabled }) => (
        <InlineSelectField
          value={row.draft.state}
          options={defaultStates}
          disabled={disabled}
          ariaLabel="Default state"
          onChange={(state) => update({ state })}
        />
      ),
    },
  ], []);

  return (
    <main className="inline-table-demo">
      <header className="inline-table-demo__header">
        <span className="inline-table-demo__eyebrow">component demo</span>
        <h1 className="inline-table-demo__title">Inline table forms for operational lists.</h1>
        <p className="inline-table-demo__lede">
          Dense row editing, detail lines for notes, explicit and autosave modes, and a phone layout
          that keeps the form usable without opening a drawer.
        </p>
      </header>

      <div className="inline-table-demo__grid">
        <DemoPanel title="Task quick entry" copy="Explicit save, new rows, read/edit existing rows, validation, and note detail lines." tag="explicit">
          <InlineTableForm
            ariaLabel="Task quick-entry inline table"
            columns={taskColumns}
            rows={tasks}
            saveMode="explicit"
            createEmptyDraft={blankTaskDraft}
            createRowLabel="New task"
            validateCreate={(draft) => draft.title.trim() ? null : "Title is required before the row can be saved."}
            onCreate={(draft) => createTask(setTasks, draft)}
            onDraftChange={(id, patch) => patchRows(setTasks, id, patch)}
            onEdit={(id) => setRowsEditing(setTasks, id, true)}
            onDelete={(id) => deleteRow(setTasks, id)}
            onCancel={(id) => cancelTask(setTasks, id)}
            onSave={(id) => saveTask(setTasks, id)}
            getRowLabel={(row) => (row.label ?? row.draft.title) || "New task"}
            renderDetail={({ row, update, disabled }) => (
              row.editing ? (
                <InlineNoteField
                  value={row.draft.note}
                  disabled={disabled}
                  ariaLabel="Task note"
                  placeholder="Add a note without stretching the main row"
                  onChange={(note) => update({ note })}
                />
              ) : row.draft.note ? (
                <p className="inline-table-form__message">{row.draft.note}</p>
              ) : null
            )}
          />
        </DemoPanel>

        <DemoPanel title="Checklist editor" copy="Compact explicit rows with checkbox, select, and a disabled item that reads as locked." tag="compact">
          <InlineTableForm
            compact
            ariaLabel="Checklist inline table"
            columns={checklistColumns}
            rows={checklist}
            saveMode="explicit"
            onDraftChange={(id, patch) => patchRows(setChecklist, id, patch)}
            onEdit={(id) => setRowsEditing(setChecklist, id, true)}
            onDelete={(id) => deleteRow(setChecklist, id)}
            onCancel={(id) => cancelSimple(setChecklist, id)}
            onSave={(id) => saveSimple(setChecklist, id)}
            getRowLabel={(row) => row.draft.item}
            emptyState="No checklist items yet."
          />
        </DemoPanel>

        <DemoPanel title="Autosave assignments" copy="Blur or press Enter to save. Escape cancels dirty changes; one row simulates a blocked save." tag="autosave">
          <InlineTableForm
            ariaLabel="Autosave assignment inline table"
            columns={assignmentColumns}
            rows={assignments}
            onDraftChange={(id, patch) => patchRows(setAssignments, id, patch)}
            onEdit={(id) => setRowsEditing(setAssignments, id, true)}
            onDelete={(id) => deleteRow(setAssignments, id)}
            onCancel={(id) => resetAssignment(setAssignments, id)}
            onSave={(id) => saveAssignment(setAssignments, id, assignmentSaveTimers)}
            getRowLabel={(row) => row.draft.window}
            renderDetail={({ row }) => (
              <div className="inline-table-demo__note-line">
                <span className="chip chip--ghost chip--sm">Saves on blur</span>
                {row.id === "a-2" ? <span className="chip chip--rust chip--sm">Simulated conflict</span> : null}
              </div>
            )}
          />
        </DemoPanel>

        <DemoPanel title="Fully default table" copy="No optional presentation or interaction overrides: autosave, icon actions, single-click edit, derived labels, standard density, default widths, and factory create row." tag="defaults">
          <InlineTableForm
            ariaLabel="Fully default inline table"
            columns={defaultColumns}
            rows={defaultRows}
            createEmptyDraft={blankDefaultDraft}
            onCreate={(draft) => createDefaultRow(setDefaultRows, draft)}
            onDraftChange={(id, patch) => patchRows(setDefaultRows, id, patch)}
            onEdit={(id) => setRowsEditing(setDefaultRows, id, true)}
            onDelete={(id) => deleteRow(setDefaultRows, id)}
            onCancel={(id) => cancelSimple(setDefaultRows, id)}
            onSave={(id) => saveSimple(setDefaultRows, id)}
          />
        </DemoPanel>
      </div>
    </main>
  );
}

function DemoPanel({
  title,
  copy,
  tag,
  children,
}: {
  title: string;
  copy: string;
  tag: string;
  children: ReactNode;
}) {
  return (
    <section className="inline-table-demo__panel">
      <header className="inline-table-demo__panel-head">
        <div>
          <h2 className="inline-table-demo__panel-title">{title}</h2>
          <p className="inline-table-demo__panel-copy">{copy}</p>
        </div>
        <span className="inline-table-demo__tag">{tag}</span>
      </header>
      {children}
    </section>
  );
}

function ReadText({ value, fallback }: { value: string; fallback?: string }) {
  return (
    <span className={value ? "inline-table-form__read" : "inline-table-form__read inline-table-form__read--muted"}>
      {value || fallback || "None"}
    </span>
  );
}

function PriorityChip({ priority }: { priority: Priority }) {
  const tone = priority === "high" ? "rust" : priority === "low" ? "ghost" : "moss";
  return <span className={`chip chip--${tone} chip--sm`}>{labelFor(priorities, priority)}</span>;
}

function labelFor(options: readonly { value: string; label: string }[], value: string) {
  return options.find((option) => option.value === value)?.label ?? value;
}

function patchRows<TDraft>(
  setRows: Dispatch<SetStateAction<InlineTableRow<TDraft>[]>>,
  id: string,
  patch: Partial<TDraft>,
) {
  setRows((rows) => rows.map((row) => (
    row.id === id
      ? {
        ...row,
        committedDraft: row.committedDraft ?? row.draft,
        draft: { ...row.draft, ...patch },
        dirty: true,
        error: undefined,
        validation: undefined,
      }
      : row
  )));
}

function setRowsEditing<TDraft>(
  setRows: Dispatch<SetStateAction<InlineTableRow<TDraft>[]>>,
  id: string,
  editing: boolean,
) {
  setRows((rows) => rows.map((row) => row.id === id ? { ...row, editing } : row));
}

function saveSimple<TDraft>(
  setRows: Dispatch<SetStateAction<InlineTableRow<TDraft>[]>>,
  id: string,
) {
  setRows((rows) => rows.map((row) => (
    row.id === id
      ? { ...row, committedDraft: row.draft, dirty: false, editing: false, saving: false, error: undefined }
      : row
  )));
}

function cancelSimple<TDraft>(
  setRows: Dispatch<SetStateAction<InlineTableRow<TDraft>[]>>,
  id: string,
) {
  setRows((rows) => rows.flatMap((row) => {
    if (row.id !== id) return [row];
    if (row.isNew) return [];
    return [{
      ...row,
      draft: row.committedDraft ?? row.draft,
      dirty: false,
      editing: false,
      validation: undefined,
      error: undefined,
    }];
  }));
}

function deleteRow<TDraft>(
  setRows: Dispatch<SetStateAction<InlineTableRow<TDraft>[]>>,
  id: string,
) {
  setRows((rows) => rows.filter((row) => row.id !== id));
}

function saveTask(
  setRows: Dispatch<SetStateAction<InlineTableRow<TaskDraft>[]>>,
  id: string,
) {
  setRows((rows) => rows.map((row) => {
    if (row.id !== id) return row;
    if (!row.draft.title.trim()) {
      return { ...row, validation: "Title is required before the row can be saved." };
    }
    return {
      ...row,
      committedDraft: row.draft,
      dirty: false,
      editing: false,
      isNew: false,
      validation: undefined,
      error: undefined,
    };
  }));
}

function createTask(
  setRows: Dispatch<SetStateAction<InlineTableRow<TaskDraft>[]>>,
  draft: TaskDraft,
) {
  const savedId = nextDemoRowId("t");
  setRows((rows) => [
    ...rows,
    {
      id: savedId,
      editing: false,
      draft,
      committedDraft: draft,
    },
  ]);
}

function createDefaultRow(
  setRows: Dispatch<SetStateAction<InlineTableRow<DefaultDraft>[]>>,
  draft: DefaultDraft,
) {
  const savedId = nextDemoRowId("d");
  setRows((rows) => [
    ...rows,
    {
      id: savedId,
      draft,
      committedDraft: draft,
    },
  ]);
}

function cancelTask(
  setRows: Dispatch<SetStateAction<InlineTableRow<TaskDraft>[]>>,
  id: string,
) {
  cancelSimple(setRows, id);
}

function resetAssignment(
  setRows: Dispatch<SetStateAction<InlineTableRow<AssignmentDraft>[]>>,
  id: string,
) {
  setRows((rows) => rows.map((row) => (
    row.id === id
      ? {
        ...row,
        draft: row.committedDraft ?? initialAssignments.find((item) => item.id === id)?.draft ?? row.draft,
        dirty: false,
        editing: false,
        error: undefined,
      }
      : row
  )));
}

function saveAssignment(
  setRows: Dispatch<SetStateAction<InlineTableRow<AssignmentDraft>[]>>,
  id: string,
  timers: MutableRefObject<number[]>,
) {
  setRows((rows) => rows.map((row) => row.id === id ? { ...row, saving: true, error: undefined } : row));
  const timer = window.setTimeout(() => {
    timers.current = timers.current.filter((candidate) => candidate !== timer);
    setRows((rows) => rows.map((row) => {
      if (row.id !== id) return row;
      if (id === "a-2") {
        return {
          ...row,
          saving: false,
          dirty: true,
          error: "Crew overlap detected. Choose another backup or cancel the edit.",
        };
      }
      return {
        ...row,
        committedDraft: row.draft,
        saving: false,
        dirty: false,
        editing: false,
        error: undefined,
      };
    }));
  }, 450);
  timers.current = [...timers.current, timer];
}

const initialTasks: InlineTableRow<TaskDraft>[] = [
  {
    id: "t-1",
    editing: false,
    draft: {
      title: "Confirm linen delivery",
      assignee: "maria",
      property: "villa-sud",
      due: "2026-05-18",
      priority: "high",
      note: "Supplier promised arrival before the 10:30 turnover window.",
    },
  },
  {
    id: "t-2",
    editing: false,
    draft: {
      title: "Restock coffee capsules",
      assignee: "enzo",
      property: "loft-north",
      due: "2026-05-19",
      priority: "normal",
      note: "",
    },
  },
  {
    id: "t-3",
    label: "Validation example",
    isNew: true,
    editing: true,
    dirty: true,
    validation: "Title is required before the row can be saved.",
    draft: {
      title: "",
      assignee: "",
      property: "harbor-flat",
      due: "2026-05-20",
      priority: "low",
      note: "New-row validation and cancel behavior.",
    },
  },
];

function blankTaskDraft(): TaskDraft {
  return {
    title: "",
    assignee: "",
    property: "villa-sud",
    due: "2026-05-18",
    priority: "normal",
    note: "",
  };
}

function blankDefaultDraft(): DefaultDraft {
  return {
    item: "",
    owner: "",
    due: "2026-05-21",
    state: "planned",
  };
}

const initialChecklist: InlineTableRow<ChecklistDraft>[] = [
  {
    id: "c-1",
    editing: true,
    dirty: true,
    draft: { done: false, item: "Photo balcony chairs after staging", owner: "sora", phase: "walkthrough" },
  },
  {
    id: "c-2",
    editing: false,
    draft: { done: true, item: "Replace entry code in guest guide", owner: "maria", phase: "handoff" },
  },
  {
    id: "c-3",
    editing: false,
    disabled: true,
    meta: <span><RotateCcw size={13} aria-hidden="true" /> Synced from template</span>,
    draft: { done: false, item: "Do not edit - template controlled", owner: "enzo", phase: "prep" },
  },
];

const initialAssignments: InlineTableRow<AssignmentDraft>[] = [
  {
    id: "a-1",
    dirty: false,
    draft: { window: "09:00-11:00", team: "Housekeeping A", property: "villa-sud", backup: "Sora" },
  },
  {
    id: "a-2",
    dirty: true,
    error: "Crew overlap detected. Choose another backup or cancel the edit.",
    draft: { window: "11:00-13:00", team: "Turnover pair", property: "harbor-flat", backup: "Maria" },
  },
  {
    id: "a-3",
    saving: true,
    draft: { window: "14:00-16:00", team: "Maintenance", property: "loft-north", backup: "Enzo" },
  },
];

const initialDefaultRows: InlineTableRow<DefaultDraft>[] = [
  {
    id: "d-1",
    draft: { item: "Check guest guide links", owner: "maria", due: "2026-05-21", state: "planned" },
    committedDraft: { item: "Check guest guide links", owner: "maria", due: "2026-05-21", state: "planned" },
  },
  {
    id: "d-2",
    draft: { item: "Assign weekend backup", owner: "enzo", due: "2026-05-22", state: "active" },
    committedDraft: { item: "Assign weekend backup", owner: "enzo", due: "2026-05-22", state: "active" },
  },
];
