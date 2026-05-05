import type { Me, PhotoEvidence, Task, TaskPriority, TaskStatus } from "@/types/api";

export type ApiTaskState =
  | "scheduled"
  | "pending"
  | "in_progress"
  | "completed"
  | "skipped"
  | "cancelled"
  | "overdue";

export interface ApiChecklistItem {
  label?: string;
  text?: string;
  done?: boolean;
  checked?: boolean;
  guest_visible?: boolean;
  key?: string;
  required?: boolean;
}

export interface ApiTask {
  id: string;
  title: string;
  workspace_id?: string;
  template_id?: string | null;
  schedule_id?: string | null;
  property_id?: string | null;
  area?: string | null;
  area_id?: string | null;
  priority: TaskPriority;
  state?: ApiTaskState;
  status?: TaskStatus;
  scheduled_for_utc?: string;
  scheduled_for_local?: string;
  scheduled_start?: string;
  duration_minutes?: number | null;
  estimated_minutes?: number;
  photo_evidence: PhotoEvidence;
  evidence_policy?: Task["evidence_policy"];
  linked_instruction_ids?: string[];
  instructions_ids?: string[];
  assigned_user_id?: string | null;
  assignee_id?: string | null;
  created_by?: string | null;
  is_personal?: boolean;
  asset_id?: string | null;
  settings_override?: Record<string, unknown>;
  checklist?: ApiChecklistItem[];
}

export interface TaskListResponse {
  data: ApiTask[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface TodayPayload {
  now_task: Task | null;
  upcoming: Task[];
  completed: Task[];
  nowIso: string;
}

export function todayQueryParams(me: Me): URLSearchParams {
  const window = todayUtcWindow(me.today);
  const params = new URLSearchParams({
    scheduled_for_utc_gte: window.gte,
    scheduled_for_utc_lt: window.lt,
    limit: "100",
  });
  if (me.user_id) params.set("assignee_user_id", me.user_id);
  return params;
}

export function groupToday(tasks: Task[], nowIso: string): TodayPayload {
  const sorted = [...tasks].sort((a, b) => a.scheduled_start.localeCompare(b.scheduled_start));
  const completed = sorted.filter((task) => task.status === "completed");
  const active = sorted.filter((task) => !isTerminalStatus(task.status));
  const nowMs = new Date(nowIso).getTime();
  const nowTask = active.find((task) => new Date(task.scheduled_start).getTime() <= nowMs) ?? null;
  const upcoming = active.filter((task) => task.id !== nowTask?.id);
  return { now_task: nowTask, upcoming, completed, nowIso };
}

export function markCompleted(today: TodayPayload, taskId: string): TodayPayload {
  const all = [today.now_task, ...today.upcoming, ...today.completed].filter(
    (task): task is Task => task !== null,
  );
  const updated = all.map((task) =>
    task.id === taskId ? { ...task, status: "completed" as const } : task,
  );
  return groupToday(updated, today.nowIso);
}

export function normalizeTodayPayload(
  page: TaskListResponse,
  nowIso: string,
  fallbackIso = new Date().toISOString(),
): TodayPayload {
  return groupToday(page.data.map((task) => normalizeTask(task, fallbackIso)), nowIso);
}

export function normalizeTask(task: ApiTask, fallbackIso = new Date().toISOString()): Task {
  const state = task.state ?? statusToState(task.status) ?? "pending";
  return {
    id: task.id,
    title: task.title,
    property_id: task.property_id ?? "",
    area: task.area ?? task.area_id ?? "",
    assignee_id: task.assignee_id ?? task.assigned_user_id ?? "",
    scheduled_start: scheduledStart(task, fallbackIso),
    estimated_minutes: task.duration_minutes ?? task.estimated_minutes ?? 30,
    priority: task.priority,
    status: stateToStatus(state),
    checklist: normalizeChecklist(task.checklist),
    photo_evidence: task.photo_evidence,
    evidence_policy: task.evidence_policy ?? evidencePolicyFromPhoto(task.photo_evidence),
    instructions_ids: task.instructions_ids ?? task.linked_instruction_ids ?? [],
    template_id: task.template_id ?? null,
    schedule_id: task.schedule_id ?? null,
    turnover_bundle_id: null,
    asset_id: task.asset_id ?? null,
    settings_override: task.settings_override ?? {},
    assigned_user_id: task.assigned_user_id ?? task.assignee_id ?? "",
    workspace_id: task.workspace_id ?? "",
    created_by: task.created_by ?? "",
    is_personal: task.is_personal ?? false,
  };
}

function normalizeChecklist(items: ApiChecklistItem[] | undefined): Task["checklist"] {
  return (items ?? [])
    .map((item) => ({
      label: item.label ?? item.text ?? "",
      done: item.done ?? item.checked ?? false,
      guest_visible: item.guest_visible,
      key: item.key,
      required: item.required,
    }))
    .filter((item) => item.label);
}

function scheduledStart(task: ApiTask, fallbackIso: string): string {
  return task.scheduled_for_utc ?? task.scheduled_start ?? task.scheduled_for_local ?? fallbackIso;
}

function todayUtcWindow(today: string): { gte: string; lt: string } {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(today);
  const start = match
    ? new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])))
    : new Date(today);
  const end = new Date(start);
  end.setDate(end.getDate() + 1);
  return { gte: start.toISOString(), lt: end.toISOString() };
}

function statusToState(status: TaskStatus | undefined): ApiTaskState | null {
  return status ?? null;
}

function stateToStatus(state: ApiTaskState): TaskStatus {
  return state;
}

function isTerminalStatus(status: TaskStatus): boolean {
  return status === "completed" || status === "skipped" || status === "cancelled";
}

function evidencePolicyFromPhoto(photo: PhotoEvidence): Task["evidence_policy"] {
  if (photo === "required") return "require";
  if (photo === "optional") return "optional";
  return "forbid";
}
