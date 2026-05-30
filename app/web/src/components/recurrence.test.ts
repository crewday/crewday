import { describe, expect, it } from "vitest";

import {
  buildRecurrenceRrule,
  normalizeRecurrenceRrule,
  parseRecurrenceRrule,
  recurrencePreview,
  recurrenceSummary,
} from "@/components/recurrence";

describe("recurrence helpers", () => {
  it("builds common friendly RRULEs while preserving prefix preferences", () => {
    expect(buildRecurrenceRrule({ frequency: "DAILY" })).toBe("FREQ=DAILY");
    expect(buildRecurrenceRrule({ frequency: "WEEKLY", interval: 2, byday: ["MO", "WE"] })).toBe(
      "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE",
    );
    expect(buildRecurrenceRrule({ frequency: "MONTHLY", bymonthday: 1 })).toBe(
      "FREQ=MONTHLY;BYMONTHDAY=1",
    );
    expect(buildRecurrenceRrule({ frequency: "YEARLY" }, { includePrefix: true })).toBe("RRULE:FREQ=YEARLY");
  });

  it("validates raw RRULE syntax before normalization", () => {
    expect(parseRecurrenceRrule("FREQ=WEEKLY;BYDAY=MO,TH")).toMatchObject({
      valid: true,
      value: "FREQ=WEEKLY;BYDAY=MO,TH",
    });
    expect(normalizeRecurrenceRrule("RRULE:FREQ=MONTHLY;BYMONTHDAY=1")).toBe("FREQ=MONTHLY;BYMONTHDAY=1");
    expect(normalizeRecurrenceRrule("FREQ=WEEKLY;BYDAY=MO", { includePrefix: true })).toBe(
      "RRULE:FREQ=WEEKLY;BYDAY=MO",
    );
    expect(parseRecurrenceRrule("FREQ=WEEKLY;NOPE=1")).toMatchObject({
      valid: false,
      error: "RRULE key NOPE is not supported.",
    });
    expect(parseRecurrenceRrule("FREQ=HOURLY")).toMatchObject({
      valid: false,
      error: "Use FREQ=DAILY, WEEKLY, MONTHLY, or YEARLY.",
    });
    expect(parseRecurrenceRrule("FREQ=WEEKLY;BYDAY=XX")).toMatchObject({
      valid: false,
      error: "BYDAY must use MO,TU,WE,TH,FR,SA,SU.",
    });
  });

  it("summarizes and previews valid recurrence values", () => {
    expect(recurrenceSummary(null, { emptyLabel: "Every task" })).toBe("Every task");
    expect(recurrenceSummary("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE")).toBe("Every 2 weeks on Mon, Wed");
    expect(recurrencePreview("FREQ=WEEKLY;BYDAY=MO", {
      startDate: new Date(2026, 4, 30),
      count: 2,
    })).toEqual(["Mon, 1 Jun 2026", "Mon, 8 Jun 2026"]);
    expect(recurrencePreview("FREQ=DAILY;COUNT=2", {
      startDate: new Date(2026, 4, 30),
      count: 4,
    })).toEqual(["Sat, 30 May 2026", "Sun, 31 May 2026"]);
    expect(recurrencePreview("FREQ=DAILY;BYHOUR=9", {
      startDate: new Date(2026, 4, 30),
      count: 2,
    })).toEqual([]);
  });
});
