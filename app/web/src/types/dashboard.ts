// crewday — JSON API types: aggregated payloads for the workspace
// dashboard and history pages.

import type { Employee } from "./employee";
import type { Task, Issue } from "./task";
import type { ApprovalRequest } from "./approval";
import type { Expense } from "./expense";
import type { Leave } from "./employee";
import type { Stay, Property } from "./property";
import type { ListEnvelope } from "../lib/listResponse";

export type HistoryTab = "tasks" | "chats" | "expenses" | "leaves";

export interface HistoryChat {
  id: string;
  title: string;
  last_at: string;
  summary: string;
}

export interface HistoryRowsByTab {
  tasks: Task;
  chats: HistoryChat;
  expenses: Expense;
  leaves: Leave;
}

export type HistoryPagePayload<T extends HistoryTab> = ListEnvelope<HistoryRowsByTab[T]>;

export interface DashboardPayload {
  on_booking: Employee[];
  by_status: { completed: Task[]; in_progress: Task[]; pending: Task[] };
  pending_approvals: ApprovalRequest[];
  pending_expenses: Expense[];
  pending_leaves: Leave[];
  open_issues: Issue[];
  stays_today: Stay[];
  properties: Property[];
  employees: Employee[];
}
