// Booking + timeline helpers for `/schedule` (§14 "Schedule view").
//
// `bookingMinutes` and `fmtDuration` drive the day drawer's
// per-booking duration label; `bookingNeedsAttention` and
// `BOOKING_STATUS_LABEL` drive the pending banner + chip text.
// `hhmmToMin`, `isoToMinOfDay`, `computeWindow`, `TASK_CHIP_*` build
// the per-week timeline geometry — a single window per ISO week so
// every cell in the desktop grid shares top/bottom hours and stays
// at the same height.

import type { Booking, BookingStatus } from "@/types/api";

interface DayCellLite {
  rota: { slot: { starts_local: string; ends_local: string } }[];
  tasks: { scheduled_start: string; estimated_minutes: number; property_id: string }[];
  bookings: Pick<Booking, "scheduled_start" | "scheduled_end" | "property_id">[];
  pattern: { starts_local: string | null; ends_local: string | null } | null;
}

export const BOOKING_STATUS_LABEL: Record<BookingStatus, string> = {
  pending_approval: "Pending approval",
  scheduled: "Scheduled",
  completed: "Completed",
  cancelled_by_client: "Cancelled (client)",
  cancelled_by_agency: "Cancelled (agency)",
  no_show_worker: "No-show",
  adjusted: "Completed (edited)",
};

export function bookingMinutes(b: Booking): number {
  if (b.actual_minutes_paid != null) return b.actual_minutes_paid;
  if (b.actual_minutes != null) return b.actual_minutes;
  const ms = new Date(b.scheduled_end).getTime() - new Date(b.scheduled_start).getTime();
  return Math.max(0, Math.round(ms / 60_000) - Math.round(b.break_seconds / 60));
}

export function fmtDuration(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${h}h ${String(m).padStart(2, "0")}m`;
}

export function fmtHM(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// "Needs attention" = a pending_approval row (ad-hoc proposal or a
// declined-and-unassigned one) OR a non-null pending self-amend the
// manager hasn't ruled on yet. Drives both the top banner count and
// the day-cell sand-edge modifier.
export function bookingNeedsAttention(b: Booking): boolean {
  return b.status === "pending_approval" || b.pending_amend_minutes != null;
}

export function hhmmToMin(s: string): number {
  const [h, m] = s.split(":").map((n) => Number(n));
  return (h ?? 0) * 60 + (m ?? 0);
}

export function isoToMinOfDay(iso: string): number {
  const d = new Date(iso);
  return d.getHours() * 60 + d.getMinutes();
}

// ── Timeline geometry ─────────────────────────────────────────────────
//
// The day-cell timeline is a vertical axis of minutes. Each loaded ISO
// week computes one `TimeWindow` covering every event in the week, so
// all seven desktop cells share the same top/bottom hours and render at
// identical height (required for a clean 7-col grid). Phone cards in
// the scrolling agenda use the same per-week window so switching weeks
// feels continuous.
//
// Scale is bounded: ~0.5 px/min with a 220–480px total clamp. Clamping
// keeps a quiet day readable without a 9h shift ballooning the agenda;
// an unusually long event simply compresses the scale rather than
// inflating the whole week.

export interface TimeWindow {
  startMin: number;
  endMin: number;
  pxPerMin: number;
  totalPx: number;
}

// Minimum pixel height a task chip needs to stay readable (time +
// one-line title). Used both to clamp chip size at render time and
// to compute how dense a booking's tasks are when picking the row
// scale.
export const TASK_CHIP_MIN_PX = 20;
export const TASK_CHIP_GAP_PX = 2;

export function computeWindow(cells: DayCellLite[]): TimeWindow {
  const bounds = eventBounds(cells);
  const [startMin, endMin] = expandedBounds(bounds.startMin, bounds.endMin);
  const pxPerMin = weekScale(cells);
  const totalPx = (endMin - startMin) * pxPerMin;
  return { startMin, endMin, pxPerMin, totalPx };
}

function eventBounds(cells: DayCellLite[]): { startMin: number; endMin: number } {
  let minStart = Infinity;
  let maxEnd = -Infinity;
  for (const cell of cells) {
    const bounds = cellBounds(cell);
    minStart = Math.min(minStart, bounds.startMin);
    maxEnd = Math.max(maxEnd, bounds.endMin);
  }
  return { startMin: minStart, endMin: maxEnd };
}

function cellBounds(cell: DayCellLite): { startMin: number; endMin: number } {
  const ranges: [number, number][] = [
    ...cell.rota.map(
      (r): [number, number] => [hhmmToMin(r.slot.starts_local), hhmmToMin(r.slot.ends_local)],
    ),
    ...cell.bookings.map(
      (b): [number, number] => [isoToMinOfDay(b.scheduled_start), isoToMinOfDay(b.scheduled_end)],
    ),
    ...cell.tasks.map((t) => {
      const start = isoToMinOfDay(t.scheduled_start);
      return [start, start + (t.estimated_minutes || 30)] as [number, number];
    }),
    ...patternRange(cell),
  ];
  return {
    startMin: Math.min(...ranges.map(([start]) => start)),
    endMin: Math.max(...ranges.map(([, end]) => end)),
  };
}

function patternRange(cell: DayCellLite): [number, number][] {
  if (!cell.pattern?.starts_local || !cell.pattern.ends_local) return [];
  return [[hhmmToMin(cell.pattern.starts_local), hhmmToMin(cell.pattern.ends_local)]];
}

function expandedBounds(rawStart: number, rawEnd: number): [number, number] {
  let minStart = rawStart;
  let maxEnd = rawEnd;
  if (!isFinite(minStart) || !isFinite(maxEnd)) {
    minStart = 9 * 60;
    maxEnd = 17 * 60;
  }
  minStart = Math.max(0, Math.floor((minStart - 30) / 60) * 60);
  maxEnd = Math.min(24 * 60, Math.ceil((maxEnd + 30) / 60) * 60);
  if (maxEnd - minStart < 360) {
    const mid = (minStart + maxEnd) / 2;
    minStart = Math.max(0, Math.floor((mid - 180) / 60) * 60);
    maxEnd = Math.min(24 * 60, Math.ceil((mid + 180) / 60) * 60);
  }
  return [minStart, maxEnd];
}

function weekScale(cells: DayCellLite[]): number {
  let pxPerMin = 0.5;
  for (const cell of cells) {
    for (const b of cell.bookings) {
      pxPerMin = Math.max(pxPerMin, bookingScale(b, cell.tasks));
    }
  }
  return Math.min(pxPerMin, 1.5);
}

function bookingScale(
  booking: DayCellLite["bookings"][number],
  tasks: DayCellLite["tasks"],
): number {
  const bookingStart = isoToMinOfDay(booking.scheduled_start);
  const bookingEnd = isoToMinOfDay(booking.scheduled_end);
  const starts = tasks
    .filter((task) => task.property_id === booking.property_id)
    .map((task) => isoToMinOfDay(task.scheduled_start))
    .filter((start) => start >= bookingStart && start < bookingEnd)
    .sort((a, b) => a - b);
  if (starts.length === 0) return 0.5;
  return (TASK_CHIP_MIN_PX + TASK_CHIP_GAP_PX) / smallestGap(starts, bookingStart);
}

function smallestGap(starts: number[], bookingStart: number): number {
  let minGap = Math.max(1, starts[0]! - bookingStart);
  for (let i = 1; i < starts.length; i++) {
    minGap = Math.min(minGap, starts[i]! - starts[i - 1]!);
  }
  return minGap;
}

export function posTop(minutes: number, window: TimeWindow): number {
  return (minutes - window.startMin) * window.pxPerMin;
}
