// Re-export shim so import paths line up with `app/web/src/types/task.ts`.
// The mocks tree keeps every type in `api.ts`; the production tree splits
// them into one file per bounded context. Re-exporting here lets the
// verbatim-port property hold (the page file is byte-identical between
// trees) without forcing a full reshuffle of `mocks/web/src/types/`.
export type {
  AreaScope,
  AvailabilityOverride,
  AvailabilityOverrideCategory,
  ChecklistItem,
  ChecklistTemplateItem,
  Instruction,
  Issue,
  MySchedulePayload,
  PhotoEvidence,
  PropertyScope,
  Schedule,
  ScheduleAssignment,
  ScheduleRuleset,
  ScheduleRulesetSlot,
  SchedulerCalendarPayload,
  SchedulerTaskView,
  SchedulerUserView,
  SelfWeeklyAvailabilitySlot,
  Task,
  TaskComment,
  TaskPriority,
  TaskStatus,
  TaskTemplate,
} from "./api";
