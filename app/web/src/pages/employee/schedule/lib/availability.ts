// Day availability resolver for `/schedule` (§14 "Schedule view").
//
// Single source of truth for the day's availability — used by the rail
// (bar + sideways text), the pending-day classname, and the empty-day
// fallback word. Returns a range in minutes-since-midnight so the rail
// bar can span exactly the shift's duration, in addition to the text
// and tone.
//
// The priority hierarchy (approved leave → pending leave → approved
// override → pending override → weekly pattern → "Off") matches §06
// "Approval logic (hybrid model)".

import { hhmmToMin } from "./bookingHelpers";
import type { DayCell } from "./buildCells";

type AvailTone = "moss" | "sand" | "rust" | "ghost";

export interface Availability {
  text: string;
  tone: AvailTone;
  startMin: number | null;
  endMin: number | null;
}

export function availability(cell: DayCell): Availability {
  const approvedLeave = cell.leaves.find((lv) => lv.approved_at !== null);
  if (approvedLeave) return fullDay(approvedLeave.category.toUpperCase(), "rust");

  const pendingLeave = cell.leaves.find((lv) => lv.approved_at === null);
  if (pendingLeave) return fullDay(`${pendingLeave.category.toUpperCase()} · pending`, "sand");

  const approvedOverride = cell.overrides.find((o) => o.approved_at !== null);
  if (approvedOverride) {
    const result = overrideAvailability(approvedOverride, cell, "rust", "moss", "");
    if (result) return result;
  }

  const pendingOverride = cell.overrides.find((o) => o.approved_at === null);
  if (pendingOverride) {
    const result = overrideAvailability(pendingOverride, cell, "sand", "sand", " · pending");
    if (result) return result;
  }

  return patternAvailability(cell) ?? offAvailability("Off", "ghost");
}

// Legacy-compat wrapper retained for call sites (DayCellView empty-day
// word, DayDrawer header) that only need the string/tone pair.
export function hoursLabel(cell: DayCell): { text: string; tone: AvailTone } {
  const a = availability(cell);
  return { text: a.text, tone: a.tone };
}

function fullDay(text: string, tone: AvailTone): Availability {
  // code-health: ignore[ccn params] Small TS helper is mis-scored by lizard after optional-chain parsing.
  return { text, tone, startMin: 0, endMin: 24 * 60 };
}

function offAvailability(text: string, tone: AvailTone): Availability {
  return { text, tone, startMin: null, endMin: null };
}

function overrideAvailability(
  override: DayCell["overrides"][number],
  cell: DayCell,
  unavailableTone: AvailTone,
  availableTone: AvailTone,
  suffix: string,
): Availability | null {
  if (!override.available) return offAvailability("OFF" + suffix, unavailableTone);
  return availabilityRange(
    override.starts_local ?? cell.pattern?.starts_local ?? null,
    override.ends_local ?? cell.pattern?.ends_local ?? null,
    availableTone,
    suffix,
  );
}

function patternAvailability(cell: DayCell): Availability | null {
  return availabilityRange(
    cell.pattern?.starts_local ?? null,
    cell.pattern?.ends_local ?? null,
    "moss",
    "",
  );
}

function availabilityRange(
  start: string | null,
  end: string | null,
  tone: AvailTone,
  suffix: string,
): Availability | null {
  if (!start || !end) return null;
  return {
    text: `${start}–${end}${suffix}`,
    tone,
    startMin: hhmmToMin(start),
    endMin: hhmmToMin(end),
  };
}
