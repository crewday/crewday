// `DayCell` model + page-merging glue for `/schedule` (§14 "Schedule
// view"). The infinite query streams 7-day pages of `MySchedulePayload`;
// `buildCells` flattens them into one row per date — keyed by local-ISO
// date string — and `mergeSchedulePages` concatenates adjacent pages
// for downstream readers (the drawer needs every row across the loaded
// window).

import type {
  AvailabilityOverride,
  Booking,
  Leave,
  MySchedulePayload,
  ScheduleRulesetSlot,
  SchedulerTaskView,
  SelfWeeklyAvailabilitySlot,
} from "@/types/api";
import { addDays, isoDate, isoWeekday } from "./dateHelpers";

export interface DayCell {
  date: Date;
  iso: string;
  rota: { slot: ScheduleRulesetSlot; property_id: string }[];
  tasks: SchedulerTaskView[];
  leaves: Leave[];
  overrides: AvailabilityOverride[];
  bookings: Booking[];
  pattern: SelfWeeklyAvailabilitySlot | null;
}

export function buildCells(
  from: Date,
  days: number,
  data: MySchedulePayload,
): DayCell[] {
  const cells: DayCell[] = [];
  // Defensive defaults — even with the API contract pinned, a future
  // regression on /me/schedule that drops a key shouldn't crash the
  // page. Each `?? []` collapses an undefined source into an empty
  // bag so the day-cell still renders (degraded > broken).
  const assignments = data.assignments ?? [];
  const slotsAll = data.slots ?? [];
  const weeklyAvailability = data.weekly_availability ?? [];
  const tasksAll = data.tasks ?? [];
  const leavesAll = data.leaves ?? [];
  const overridesAll = data.overrides ?? [];
  const bookingsAll = data.bookings ?? [];
  const assignmentProperty = new Map<string, string>();
  assignments.forEach((a) => {
    if (a.schedule_ruleset_id) assignmentProperty.set(a.schedule_ruleset_id, a.property_id);
  });
  const weeklyByDay = new Map<number, SelfWeeklyAvailabilitySlot>(
    weeklyAvailability.map((w) => [w.weekday, w]),
  );
  for (let i = 0; i < days; i++) {
    const d = addDays(from, i);
    const iso = isoDate(d);
    const wd = isoWeekday(d);
    const rota = slotsAll
      .filter((s) => s.weekday === wd)
      .map((s) => ({
        slot: s,
        property_id: assignmentProperty.get(s.schedule_ruleset_id) ?? "",
      }))
      .filter((r) => r.property_id);
    const tasks = tasksAll
      .filter((t) => t.scheduled_start.slice(0, 10) === iso)
      .sort((a, b) => a.scheduled_start.localeCompare(b.scheduled_start));
    const leaves = leavesAll.filter(
      (lv) => lv.starts_on <= iso && lv.ends_on >= iso,
    );
    const overrides = overridesAll.filter((ao) => ao.date === iso);
    const bookings = bookingsAll
      .filter((b) => b.scheduled_start.slice(0, 10) === iso)
      .sort((a, b) => a.scheduled_start.localeCompare(b.scheduled_start));
    cells.push({
      date: d,
      iso,
      rota,
      tasks,
      leaves,
      overrides,
      bookings,
      pattern: weeklyByDay.get(wd) ?? null,
    });
  }
  return cells;
}

// Concatenate `useInfiniteQuery` pages into the same shape one /me/
// schedule call would return. Per-page collections (tasks, bookings,
// leaves, overrides) get id-deduped — the API filters by date so
// duplicates are unlikely, but a refetch overlap shouldn't crash the
// drawer. Workspace-stable rows (properties, rulesets, assignments,
// slots, weekly_availability) come from the first page.
export function mergeSchedulePages(pages: MySchedulePayload[]): MySchedulePayload | null {
  if (pages.length === 0) return null;
  const first = pages[0]!;
  if (pages.length === 1) return first;
  const last = pages[pages.length - 1]!;
  const dedup = <T,>(items: T[], key: (t: T) => string): T[] => {
    const seen = new Set<string>();
    const out: T[] = [];
    for (const it of items) {
      const k = key(it);
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(it);
    }
    return out;
  };
  return {
    window: { from: first.window.from, to: last.window.to },
    user_id: first.user_id,
    weekly_availability: first.weekly_availability ?? [],
    rulesets: dedup(pages.flatMap((p) => p.rulesets ?? []), (r) => r.id),
    slots: dedup(pages.flatMap((p) => p.slots ?? []), (s) => s.id),
    assignments: dedup(pages.flatMap((p) => p.assignments ?? []), (a) => a.id),
    tasks: dedup(pages.flatMap((p) => p.tasks ?? []), (t) => t.id),
    properties: dedup(pages.flatMap((p) => p.properties ?? []), (p) => p.id),
    leaves: dedup(pages.flatMap((p) => p.leaves ?? []), (lv) => lv.id),
    overrides: dedup(pages.flatMap((p) => p.overrides ?? []), (o) => o.id),
    bookings: dedup(pages.flatMap((p) => p.bookings ?? []), (b) => b.id),
  };
}
