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
  // code-health: ignore[ccn] Pure day-cell mapper keeps the per-day schedule contract in one readable loop.
  const cells: DayCell[] = [];
  const bags = scheduleBags(data);
  const assignmentProperty = assignmentPropertyMap(bags.assignments);
  const weeklyByDay = weeklyAvailabilityMap(bags.weeklyAvailability);
  for (let i = 0; i < days; i++) {
    const d = addDays(from, i);
    const iso = isoDate(d);
    const wd = isoWeekday(d);
    cells.push({
      date: d,
      iso,
      rota: rotaForDay(bags.slots, assignmentProperty, wd),
      tasks: byScheduledIso(bags.tasks, iso),
      leaves: bags.leaves.filter((lv) => lv.starts_on <= iso && lv.ends_on >= iso),
      overrides: bags.overrides.filter((ao) => ao.date === iso),
      bookings: byScheduledIso(bags.bookings, iso),
      pattern: weeklyByDay.get(wd) ?? null,
    });
  }
  return cells;
}

function scheduleBags(data: MySchedulePayload): {
  assignments: MySchedulePayload["assignments"];
  slots: MySchedulePayload["slots"];
  weeklyAvailability: SelfWeeklyAvailabilitySlot[];
  tasks: SchedulerTaskView[];
  leaves: Leave[];
  overrides: AvailabilityOverride[];
  bookings: Booking[];
} {
  return {
    assignments: data.assignments ?? [],
    slots: data.slots ?? [],
    weeklyAvailability: data.weekly_availability ?? [],
    tasks: data.tasks ?? [],
    leaves: data.leaves ?? [],
    overrides: data.overrides ?? [],
    bookings: data.bookings ?? [],
  };
}

function assignmentPropertyMap(assignments: MySchedulePayload["assignments"]): Map<string, string> {
  const map = new Map<string, string>();
  assignments.forEach((a) => {
    if (a.schedule_ruleset_id) map.set(a.schedule_ruleset_id, a.property_id);
  });
  return map;
}

function weeklyAvailabilityMap(
  weeklyAvailability: SelfWeeklyAvailabilitySlot[],
): Map<number, SelfWeeklyAvailabilitySlot> {
  return new Map(weeklyAvailability.map((w) => [w.weekday, w]));
}

function rotaForDay(
  slots: ScheduleRulesetSlot[],
  assignmentProperty: Map<string, string>,
  weekday: number,
): DayCell["rota"] {
  return slots
    .filter((s) => s.weekday === weekday)
    .map((s) => ({
      slot: s,
      property_id: assignmentProperty.get(s.schedule_ruleset_id) ?? "",
    }))
    .filter((r) => r.property_id);
}

function byScheduledIso<T extends { scheduled_start: string }>(items: T[], iso: string): T[] {
  return items
    .filter((item) => item.scheduled_start.slice(0, 10) === iso)
    .sort((a, b) => a.scheduled_start.localeCompare(b.scheduled_start));
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
