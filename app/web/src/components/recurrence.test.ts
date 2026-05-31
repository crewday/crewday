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
    expect(buildRecurrenceRrule({
      frequency: "MONTHLY",
      monthlyOrdinalWeekday: { ordinal: 1, weekday: "MO" },
    })).toBe("FREQ=MONTHLY;BYDAY=1MO");
    expect(buildRecurrenceRrule({
      frequency: "YEARLY",
      bymonth: 1,
      bymonthday: 15,
    }, { includePrefix: true })).toBe("RRULE:FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=15");
    expect(buildRecurrenceRrule({
      frequency: "YEARLY",
      bymonth: 9,
      monthlyOrdinalWeekday: { ordinal: 1, weekday: "MO" },
    })).toBe("FREQ=YEARLY;BYMONTH=9;BYDAY=1MO");
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

  it("parses supported monthly ordinal weekday RRULE shapes", () => {
    expect(parseRecurrenceRrule("FREQ=MONTHLY;BYDAY=1MO")).toMatchObject({
      valid: true,
      parts: {
        monthlyOrdinalWeekday: { ordinal: 1, weekday: "MO" },
        unsupported: [],
      },
    });
    expect(parseRecurrenceRrule("FREQ=MONTHLY;BYDAY=FR;BYSETPOS=-1")).toMatchObject({
      valid: true,
      parts: {
        byday: ["FR"],
        monthlyOrdinalWeekday: { ordinal: -1, weekday: "FR" },
        unsupported: [],
      },
    });
    expect(parseRecurrenceRrule("FREQ=MONTHLY;BYDAY=MO;BYSETPOS=1")).toMatchObject({
      valid: true,
      parts: {
        byday: ["MO"],
        monthlyOrdinalWeekday: { ordinal: 1, weekday: "MO" },
        unsupported: [],
      },
    });
    expect(parseRecurrenceRrule("FREQ=MONTHLY;BYDAY=5MO")).toMatchObject({
      valid: true,
      parts: {
        monthlyOrdinalWeekday: null,
        unsupported: ["BYDAY"],
      },
    });
    expect(parseRecurrenceRrule("FREQ=MONTHLY;BYDAY=MO")).toMatchObject({
      valid: true,
      parts: {
        monthlyOrdinalWeekday: null,
        unsupported: ["BYDAY"],
      },
    });
  });

  it("parses supported yearly explicit date and ordinal weekday RRULE shapes", () => {
    expect(parseRecurrenceRrule("FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=15")).toMatchObject({
      valid: true,
      parts: {
        bymonth: 1,
        bymonthday: 15,
        monthlyOrdinalWeekday: null,
        unsupported: [],
      },
    });
    expect(parseRecurrenceRrule("FREQ=YEARLY;BYMONTH=9;BYDAY=1MO")).toMatchObject({
      valid: true,
      parts: {
        bymonth: 9,
        monthlyOrdinalWeekday: { ordinal: 1, weekday: "MO" },
        unsupported: [],
      },
    });
    expect(parseRecurrenceRrule("FREQ=YEARLY;BYMONTH=9;BYDAY=MO;BYSETPOS=-1")).toMatchObject({
      valid: true,
      parts: {
        bymonth: 9,
        byday: ["MO"],
        monthlyOrdinalWeekday: { ordinal: -1, weekday: "MO" },
        unsupported: [],
      },
    });
    expect(parseRecurrenceRrule("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=31")).toMatchObject({
      valid: true,
      parts: {
        bymonth: 2,
        bymonthday: 31,
        unsupported: ["YEARLY"],
      },
    });
    expect(parseRecurrenceRrule("FREQ=YEARLY")).toMatchObject({
      valid: true,
      parts: {
        unsupported: ["YEARLY"],
      },
    });
    expect(parseRecurrenceRrule("FREQ=YEARLY;BYMONTH=9;BYDAY=MO")).toMatchObject({
      valid: true,
      parts: {
        monthlyOrdinalWeekday: null,
        unsupported: ["BYDAY", "YEARLY"],
      },
    });
  });

  it("summarizes and previews valid recurrence values", () => {
    expect(recurrenceSummary(null, { emptyLabel: "Every task" })).toBe("Every task");
    expect(recurrenceSummary("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE")).toBe("Every 2 weeks on Mon, Wed");
    expect(recurrenceSummary("FREQ=MONTHLY;BYDAY=1MO")).toBe("Monthly on the first Monday");
    expect(recurrenceSummary("FREQ=MONTHLY;BYDAY=FR;BYSETPOS=-1")).toBe("Monthly on the last Friday");
    expect(recurrenceSummary("FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=15")).toBe("Yearly on January 15");
    expect(recurrenceSummary("FREQ=YEARLY;BYMONTH=9;BYDAY=1MO")).toBe("Yearly on the first Monday in September");
    expect(recurrenceSummary("FREQ=YEARLY;BYMONTH=9;BYDAY=MO;BYSETPOS=-1")).toBe(
      "Yearly on the last Monday in September",
    );
    expect(recurrenceSummary("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=31")).toBe("Advanced recurrence");
    expect(recurrencePreview("FREQ=WEEKLY;BYDAY=MO", {
      startDate: new Date(2026, 4, 30),
      count: 2,
    })).toEqual(["Mon, 1 Jun 2026", "Mon, 8 Jun 2026"]);
    expect(recurrencePreview("FREQ=DAILY;COUNT=2", {
      startDate: new Date(2026, 4, 30),
      count: 4,
    })).toEqual(["Sat, 30 May 2026", "Sun, 31 May 2026"]);
    expect(recurrencePreview("FREQ=MONTHLY;BYDAY=1MO", {
      startDate: new Date(2026, 4, 30),
      count: 2,
    })).toEqual(["Mon, 1 Jun 2026", "Mon, 6 Jul 2026"]);
    expect(recurrencePreview("FREQ=MONTHLY;BYDAY=FR;BYSETPOS=-1", {
      startDate: new Date(2026, 4, 30),
      count: 2,
    })).toEqual(["Fri, 26 Jun 2026", "Fri, 31 Jul 2026"]);
    expect(recurrencePreview("FREQ=MONTHLY;BYDAY=4FR", {
      startDate: new Date(2026, 0, 1),
      count: 3,
    })).toEqual(["Fri, 23 Jan 2026", "Fri, 27 Feb 2026", "Fri, 27 Mar 2026"]);
    expect(recurrencePreview("FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=15", {
      startDate: new Date(2026, 0, 1),
      count: 2,
    })).toEqual(["Thu, 15 Jan 2026", "Fri, 15 Jan 2027"]);
    expect(recurrencePreview("FREQ=YEARLY;BYMONTH=9;BYDAY=1MO", {
      startDate: new Date(2026, 0, 1),
      count: 2,
    })).toEqual(["Mon, 7 Sept 2026", "Mon, 6 Sept 2027"]);
    expect(recurrencePreview("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29", {
      startDate: new Date(2026, 0, 1),
      count: 3,
    })).toEqual(["Tue, 29 Feb 2028", "Sun, 29 Feb 2032", "Fri, 29 Feb 2036"]);
    expect(recurrencePreview("FREQ=YEARLY;BYMONTH=9;BYDAY=MO;BYSETPOS=-1", {
      startDate: new Date(2026, 0, 1),
      count: 2,
    })).toEqual(["Mon, 28 Sept 2026", "Mon, 27 Sept 2027"]);
    expect(recurrencePreview("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=31", {
      startDate: new Date(2026, 0, 1),
      count: 2,
    })).toEqual([]);
    expect(recurrencePreview("FREQ=DAILY;BYHOUR=9", {
      startDate: new Date(2026, 4, 30),
      count: 2,
    })).toEqual([]);
    expect(recurrencePreview("FREQ=YEARLY", {
      startDate: new Date(2026, 4, 30),
      count: 2,
    })).toEqual([]);
  });
});
