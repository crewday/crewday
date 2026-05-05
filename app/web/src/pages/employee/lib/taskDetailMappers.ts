import type { AgentMessage, Instruction, PhotoEvidence, Property, TaskPriority } from "@/types/api";

export interface ResolvedInventoryEffect {
  item_ref: string;
  kind: "consume" | "produce";
  qty: number;
  item_id: string | null;
  item_name: string;
  unit: string;
  on_hand: number | null;
}

export type ApiTaskState =
  | "scheduled"
  | "pending"
  | "in_progress"
  | "completed"
  | "skipped"
  | "cancelled"
  | "overdue";

export type RenderTaskStatus =
  | "completed"
  | "in_progress"
  | "pending"
  | "scheduled"
  | "skipped"
  | "cancelled"
  | "overdue";

export interface ApiChecklistItem {
  id?: string;
  label?: string;
  text?: string;
  done?: boolean;
  checked?: boolean;
  required?: boolean;
  requires_photo?: boolean;
  guest_visible?: boolean;
  completed_at?: string | null;
  checked_at?: string | null;
  completed_by_user_id?: string | null;
}

export interface ChecklistItemView {
  id: string | null;
  label: string;
  done: boolean;
  required: boolean;
}

export interface ApiTask {
  id: string;
  workspace_id?: string;
  title: string;
  property_id?: string | null;
  area?: string | null;
  area_id?: string | null;
  priority: TaskPriority;
  state?: ApiTaskState;
  status?: RenderTaskStatus;
  scheduled_for_utc?: string;
  scheduled_for_local?: string;
  scheduled_start?: string;
  duration_minutes?: number | null;
  estimated_minutes?: number;
  photo_evidence: PhotoEvidence;
  linked_instruction_ids?: string[];
  inventory_consumption_json?: Record<string, number>;
  is_personal?: boolean;
  created_at?: string;
  checklist?: ApiChecklistItem[];
}

export interface TaskDetailResponse {
  task: ApiTask;
  property?: Property | null;
  instructions?: Instruction[];
  checklist?: ApiChecklistItem[];
  inventory_effects?: ResolvedInventoryEffect[];
}

export interface NormalizedTask {
  id: string;
  title: string;
  property_id: string | null;
  area: string;
  scheduled_start: string;
  estimated_minutes: number;
  priority: TaskPriority;
  status: RenderTaskStatus;
  checklist: ChecklistItemView[];
  photo_evidence: PhotoEvidence;
  is_personal: boolean;
  inventory_consumption_json: Record<string, number>;
}

export interface NormalizedTaskDetail {
  task: NormalizedTask;
  property: Property | null;
  instructions: Instruction[];
  inventory_effects: ResolvedInventoryEffect[];
}

export interface CommentPayload {
  id: string;
  occurrence_id: string;
  kind: "user" | "agent" | "system";
  author_user_id: string | null;
  body_md: string;
  created_at: string;
  deleted_at: string | null;
}

export function normalizeTaskDetail(payload: ApiTask | TaskDetailResponse): NormalizedTaskDetail {
  const response = isTaskDetailResponse(payload) ? payload : { task: payload };
  const rawTask = {
    ...response.task,
    checklist: response.checklist ?? response.task.checklist,
  };
  return {
    task: normalizeTask(rawTask),
    property: response.property ?? null,
    instructions: response.instructions ?? [],
    inventory_effects: response.inventory_effects ?? effectsFromConsumption(rawTask),
  };
}

export function isTaskDetailResponse(payload: ApiTask | TaskDetailResponse): payload is TaskDetailResponse {
  return "task" in payload;
}

export function normalizeTask(task: ApiTask): NormalizedTask {
  // code-health: ignore[ccn] Boundary mapper intentionally enumerates nullable API fallbacks field-by-field.
  const state = task.state ?? task.status ?? "pending";
  return {
    id: task.id,
    title: task.title,
    property_id: task.property_id ?? null,
    area: task.area ?? task.area_id ?? "",
    scheduled_start: scheduledStart(task),
    estimated_minutes: task.duration_minutes ?? task.estimated_minutes ?? 30,
    priority: task.priority,
    status: state,
    checklist: normalizeChecklist(task.checklist),
    photo_evidence: task.photo_evidence,
    is_personal: task.is_personal ?? false,
    inventory_consumption_json: task.inventory_consumption_json ?? {},
  };
}

export function updateChecklistItem(
  payload: ApiTask | TaskDetailResponse | undefined,
  itemId: string,
  patch: Partial<ApiChecklistItem>,
): ApiTask | TaskDetailResponse | undefined {
  if (!payload || !itemId) return payload;
  const updateRows = (rows: ApiChecklistItem[] | undefined): ApiChecklistItem[] | undefined =>
    rows?.map((row) => (row.id === itemId ? { ...row, ...patch } : row));
  if (isTaskDetailResponse(payload)) {
    return {
      ...payload,
      checklist: updateRows(payload.checklist),
      task: {
        ...payload.task,
        checklist: updateRows(payload.task.checklist),
      },
    };
  }
  return {
    ...payload,
    checklist: updateRows(payload.checklist),
  };
}

export function commentToMessage(comment: CommentPayload): AgentMessage {
  // code-health: ignore[ccn] Tiny ternary mapper is over-counted by the TS parser but should stay inline.
  return {
    at: comment.created_at,
    kind: comment.kind === "user" ? "user" : "agent",
    body: comment.body_md,
  };
}

function scheduledStart(task: ApiTask): string {
  return task.scheduled_for_utc ?? task.scheduled_start ?? task.scheduled_for_local ?? task.created_at ?? "";
}

function normalizeChecklist(items: ApiChecklistItem[] | undefined): ChecklistItemView[] {
  return (items ?? []).map(normalizeChecklistItem).filter((item) => item.label);
}

function normalizeChecklistItem(item: ApiChecklistItem): ChecklistItemView {
  return {
    id: item.id ?? null,
    label: item.label ?? item.text ?? "",
    done: item.done ?? item.checked ?? false,
    required: item.required ?? item.requires_photo ?? false,
  };
}

function effectsFromConsumption(task: ApiTask): ResolvedInventoryEffect[] {
  return Object.entries(task.inventory_consumption_json ?? {}).map(([itemRef, qty]) => ({
    item_ref: itemRef,
    kind: "consume",
    qty,
    item_id: null,
    item_name: itemRef,
    unit: "each",
    on_hand: null,
  }));
}
