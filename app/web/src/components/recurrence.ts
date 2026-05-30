export type RecurrenceFrequency = "DAILY" | "WEEKLY" | "MONTHLY" | "YEARLY";
export type RecurrenceWeekday = "MO" | "TU" | "WE" | "TH" | "FR" | "SA" | "SU";

export interface RecurrenceParts {
  frequency: RecurrenceFrequency;
  interval: number;
  byday: RecurrenceWeekday[];
  bymonthday: number | null;
  count: number | null;
  until: Date | null;
  unsupported: string[];
}

export interface RecurrenceParseResult {
  valid: boolean;
  value: string | null;
  parts: RecurrenceParts | null;
  error: string | null;
  hasPrefix: boolean;
}

export interface FriendlyRecurrence {
  frequency: RecurrenceFrequency;
  interval?: number;
  byday?: RecurrenceWeekday[];
  bymonthday?: number | null;
}

export const RECURRENCE_WEEKDAYS: readonly { value: RecurrenceWeekday; label: string; shortLabel: string }[] = [
  { value: "MO", label: "Monday", shortLabel: "Mon" },
  { value: "TU", label: "Tuesday", shortLabel: "Tue" },
  { value: "WE", label: "Wednesday", shortLabel: "Wed" },
  { value: "TH", label: "Thursday", shortLabel: "Thu" },
  { value: "FR", label: "Friday", shortLabel: "Fri" },
  { value: "SA", label: "Saturday", shortLabel: "Sat" },
  { value: "SU", label: "Sunday", shortLabel: "Sun" },
];

const WEEKDAY_SET = new Set(RECURRENCE_WEEKDAYS.map((day) => day.value));
const FREQUENCY_SET = new Set<RecurrenceFrequency>(["DAILY", "WEEKLY", "MONTHLY", "YEARLY"]);
const PREVIEW_KEYS = new Set(["FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "COUNT", "UNTIL"]);
const RRULE_KEYS = new Set([
  "FREQ",
  "UNTIL",
  "COUNT",
  "INTERVAL",
  "BYSECOND",
  "BYMINUTE",
  "BYHOUR",
  "BYDAY",
  "BYMONTHDAY",
  "BYYEARDAY",
  "BYWEEKNO",
  "BYMONTH",
  "BYSETPOS",
  "WKST",
]);
const WEEKDAY_INDEX: Record<RecurrenceWeekday, number> = {
  SU: 0,
  MO: 1,
  TU: 2,
  WE: 3,
  TH: 4,
  FR: 5,
  SA: 6,
};

export function weekdayForDate(date: string): RecurrenceWeekday {
  const days: RecurrenceWeekday[] = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"];
  const index = new Date(`${date}T00:00:00`).getDay();
  return days[index] ?? "MO";
}

export function parseRecurrenceRrule(value: string | null | undefined): RecurrenceParseResult {
  const raw = value?.trim() ?? "";
  if (!raw) return { valid: true, value: null, parts: null, error: null, hasPrefix: false };

  const hasPrefix = raw.toUpperCase().startsWith("RRULE:");
  const body = hasPrefix ? raw.slice(6).trim() : raw;
  if (!body) return invalid(raw, "Enter an RRULE after RRULE:.", hasPrefix);

  const entries = new Map<string, string>();
  const unsupported: string[] = [];
  for (const token of body.split(";")) {
    const trimmed = token.trim();
    if (!trimmed) return invalid(raw, "Remove empty RRULE segments.", hasPrefix);
    const equalsIndex = trimmed.indexOf("=");
    if (equalsIndex <= 0 || equalsIndex === trimmed.length - 1) {
      return invalid(raw, "Use KEY=VALUE segments separated by semicolons.", hasPrefix);
    }
    const key = trimmed.slice(0, equalsIndex).trim().toUpperCase();
    const fieldValue = trimmed.slice(equalsIndex + 1).trim().toUpperCase();
    if (!/^[A-Z]+$/.test(key)) return invalid(raw, "RRULE keys must use letters only.", hasPrefix);
    if (entries.has(key)) return invalid(raw, `RRULE repeats ${key}.`, hasPrefix);
    if (!RRULE_KEYS.has(key)) return invalid(raw, `RRULE key ${key} is not supported.`, hasPrefix);
    entries.set(key, fieldValue);
    if (!PREVIEW_KEYS.has(key)) unsupported.push(key);
  }

  const frequency = entries.get("FREQ");
  if (!frequency) return invalid(raw, "RRULE must include FREQ.", hasPrefix);
  if (!FREQUENCY_SET.has(frequency as RecurrenceFrequency)) {
    return invalid(raw, "Use FREQ=DAILY, WEEKLY, MONTHLY, or YEARLY.", hasPrefix);
  }

  const intervalValue = entries.get("INTERVAL");
  const interval = parsePositiveWholeNumber(intervalValue, 1);
  if (interval === null) {
    return invalid(raw, "INTERVAL must be a positive whole number.", hasPrefix);
  }

  const bydayValue = entries.get("BYDAY");
  const byday = bydayValue ? bydayValue.split(",") : [];
  if (byday.some((day) => !isValidBydayToken(day))) {
    return invalid(raw, "BYDAY must use MO,TU,WE,TH,FR,SA,SU.", hasPrefix);
  }
  const simpleByday = byday.filter((day): day is RecurrenceWeekday => WEEKDAY_SET.has(day as RecurrenceWeekday));
  if (simpleByday.length !== byday.length) unsupported.push("BYDAY");

  const bymonthdayValue = entries.get("BYMONTHDAY");
  const bymonthdays = bymonthdayValue ? bymonthdayValue.split(",") : [];
  if (bymonthdays.some((day) => !isValidMonthDay(day))) {
    return invalid(raw, "BYMONTHDAY must be a day number from 1 to 31.", hasPrefix);
  }
  const simpleMonthDays = bymonthdays.filter((day) => !day.startsWith("-"));
  if (simpleMonthDays.length !== bymonthdays.length || bymonthdays.length > 1) unsupported.push("BYMONTHDAY");
  const bymonthday = bymonthdays.length === 1 && simpleMonthDays.length === 1
    ? Number.parseInt(bymonthdays[0] ?? "", 10)
    : null;

  const countValue = entries.get("COUNT");
  const count = countValue ? parsePositiveWholeNumber(countValue) : null;
  if (countValue && count === null) return invalid(raw, "COUNT must be a positive whole number.", hasPrefix);

  const untilValue = entries.get("UNTIL");
  const until = untilValue ? parseUntilDate(untilValue) : null;
  if (untilValue && !until) return invalid(raw, "UNTIL must use YYYYMMDD or YYYYMMDDTHHMMSSZ.", hasPrefix);

  const wkst = entries.get("WKST");
  if (wkst && !WEEKDAY_SET.has(wkst as RecurrenceWeekday)) {
    return invalid(raw, "WKST must use MO,TU,WE,TH,FR,SA,SU.", hasPrefix);
  }

  const numericListError = validateNumericLists(entries);
  if (numericListError) return invalid(raw, numericListError, hasPrefix);

  return {
    valid: true,
    value: body,
    parts: {
      frequency: frequency as RecurrenceFrequency,
      interval,
      byday: simpleByday,
      bymonthday,
      count,
      until,
      unsupported,
    },
    error: null,
    hasPrefix,
  };
}

export function buildRecurrenceRrule(recurrence: FriendlyRecurrence, options: { includePrefix?: boolean } = {}): string {
  const interval = recurrence.interval && recurrence.interval > 1 ? recurrence.interval : 1;
  const segments = [`FREQ=${recurrence.frequency}`];
  if (interval > 1) segments.push(`INTERVAL=${interval}`);
  if (recurrence.frequency === "WEEKLY" && recurrence.byday?.length) {
    segments.push(`BYDAY=${recurrence.byday.join(",")}`);
  }
  if (recurrence.frequency === "MONTHLY" && recurrence.bymonthday) {
    segments.push(`BYMONTHDAY=${recurrence.bymonthday}`);
  }
  const rrule = segments.join(";");
  return options.includePrefix ? `RRULE:${rrule}` : rrule;
}

export function normalizeRecurrenceRrule(value: string | null, options: { includePrefix?: boolean } = {}): string | null {
  const parsed = parseRecurrenceRrule(value);
  if (!parsed.valid || !parsed.value) return null;
  return options.includePrefix ? `RRULE:${parsed.value}` : parsed.value;
}

export function frequencyFromRecurrence(value: string | null | undefined): RecurrenceFrequency {
  const parsed = parseRecurrenceRrule(value);
  return parsed.parts?.frequency ?? "WEEKLY";
}

export function recurrenceSummary(value: string | null | undefined, options: { emptyLabel?: string } = {}): string {
  const parsed = parseRecurrenceRrule(value);
  if (!parsed.value) return options.emptyLabel ?? "No recurrence";
  if (!parsed.valid || !parsed.parts) return "Advanced recurrence";

  const { frequency, interval, byday, bymonthday, unsupported } = parsed.parts;
  if (unsupported.length > 0) return "Advanced recurrence";
  if (frequency === "DAILY") return interval > 1 ? `Every ${interval} days` : "Daily";
  if (frequency === "WEEKLY") {
    const cadence = interval > 1 ? `Every ${interval} weeks` : "Weekly";
    const days = byday.length > 0 ? ` on ${weekdayLabels(byday).join(", ")}` : "";
    return `${cadence}${days}`;
  }
  if (frequency === "MONTHLY") {
    return bymonthday ? `Monthly on day ${bymonthday}` : "Monthly";
  }
  return interval > 1 ? `Every ${interval} years` : "Yearly";
}

export function recurrencePreview(
  value: string | null | undefined,
  options: { count?: number; startDate?: Date } = {},
): string[] {
  const parsed = parseRecurrenceRrule(value);
  if (!parsed.valid || !parsed.parts) return [];
  if (parsed.parts.unsupported.length > 0) return [];
  const count = options.count ?? 4;
  const startDate = stripTime(options.startDate ?? new Date());
  const dates: Date[] = [];
  let cursor = new Date(startDate);
  let guard = 0;
  while (dates.length < count && guard < 1500) {
    if (parsed.parts.until && cursor > stripTime(parsed.parts.until)) break;
    if (matchesRecurrence(cursor, startDate, parsed.parts)) dates.push(new Date(cursor));
    if (parsed.parts.count && dates.length >= parsed.parts.count) break;
    cursor.setDate(cursor.getDate() + 1);
    guard += 1;
  }
  return dates.map(formatPreviewDate);
}

function invalid(value: string, error: string, hasPrefix: boolean): RecurrenceParseResult {
  return { valid: false, value, parts: null, error, hasPrefix };
}

function weekdayLabels(days: readonly RecurrenceWeekday[]): string[] {
  return days.map((day) => RECURRENCE_WEEKDAYS.find((candidate) => candidate.value === day)?.shortLabel ?? day);
}

function parsePositiveWholeNumber(raw: string | undefined, defaultValue: number | null = null): number | null {
  if (raw === undefined) return defaultValue;
  if (!/^[1-9]\d*$/.test(raw)) return null;
  return Number.parseInt(raw, 10);
}

function isValidBydayToken(token: string): boolean {
  const match = /^([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)$/.exec(token);
  if (!match) return false;
  const ordinal = match[1];
  if (!ordinal) return true;
  const value = Number.parseInt(ordinal, 10);
  return value !== 0 && value >= -53 && value <= 53;
}

function isValidMonthDay(raw: string): boolean {
  return /^-?([1-9]|[12]\d|3[01])$/.test(raw);
}

function parseUntilDate(raw: string): Date | null {
  const match = /^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})Z?)?$/.exec(raw);
  if (!match) return null;
  const [, yearText, monthText, dayText, hourText = "00", minuteText = "00", secondText = "00"] = match;
  const year = Number.parseInt(yearText ?? "", 10);
  const month = Number.parseInt(monthText ?? "", 10);
  const day = Number.parseInt(dayText ?? "", 10);
  const hour = Number.parseInt(hourText, 10);
  const minute = Number.parseInt(minuteText, 10);
  const second = Number.parseInt(secondText, 10);
  const date = new Date(year, month - 1, day, hour, minute, second);
  if (
    date.getFullYear() !== year
    || date.getMonth() !== month - 1
    || date.getDate() !== day
    || date.getHours() !== hour
    || date.getMinutes() !== minute
    || date.getSeconds() !== second
  ) {
    return null;
  }
  return date;
}

function validateNumericLists(entries: ReadonlyMap<string, string>): string | null {
  const validators: [string, number, number][] = [
    ["BYSECOND", 0, 60],
    ["BYMINUTE", 0, 59],
    ["BYHOUR", 0, 23],
    ["BYYEARDAY", -366, 366],
    ["BYWEEKNO", -53, 53],
    ["BYMONTH", 1, 12],
    ["BYSETPOS", -366, 366],
  ];
  for (const [key, min, max] of validators) {
    const value = entries.get(key);
    if (!value) continue;
    if (value.split(",").some((item) => !isIntegerInRange(item, min, max) || item === "0" && min < 0)) {
      return `${key} contains an invalid number.`;
    }
  }
  return null;
}

function isIntegerInRange(raw: string, min: number, max: number): boolean {
  if (!/^[+-]?\d+$/.test(raw)) return false;
  const value = Number.parseInt(raw, 10);
  return Number.isInteger(value) && value >= min && value <= max;
}

function matchesRecurrence(date: Date, startDate: Date, parts: RecurrenceParts): boolean {
  if (parts.frequency === "DAILY") {
    return daysBetween(startDate, date) % parts.interval === 0;
  }
  if (parts.frequency === "WEEKLY") {
    const allowedDays = parts.byday.length > 0
      ? parts.byday.map((day) => WEEKDAY_INDEX[day])
      : [startDate.getDay()];
    if (!allowedDays.includes(date.getDay())) return false;
    return Math.floor(daysBetween(startOfWeek(startDate), date) / 7) % parts.interval === 0;
  }
  if (parts.frequency === "MONTHLY") {
    const targetDay = parts.bymonthday ?? startDate.getDate();
    if (date.getDate() !== targetDay) return false;
    return monthsBetween(startDate, date) % parts.interval === 0;
  }
  if (date.getMonth() !== startDate.getMonth() || date.getDate() !== startDate.getDate()) return false;
  return (date.getFullYear() - startDate.getFullYear()) % parts.interval === 0;
}

function stripTime(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function startOfWeek(date: Date): Date {
  const copy = stripTime(date);
  copy.setDate(copy.getDate() - copy.getDay());
  return copy;
}

function daysBetween(start: Date, end: Date): number {
  return Math.floor((stripTime(end).getTime() - stripTime(start).getTime()) / 86_400_000);
}

function monthsBetween(start: Date, end: Date): number {
  return (end.getFullYear() - start.getFullYear()) * 12 + end.getMonth() - start.getMonth();
}

function formatPreviewDate(date: Date): string {
  return new Intl.DateTimeFormat("en-GB", {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(date);
}
