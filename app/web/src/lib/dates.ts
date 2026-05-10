export type DateTimeValue = Date | string | number | null | undefined;

export interface DateTimeFormatOptions {
  locale?: string;
  timeZone?: string;
}

export interface DateTimeDisplayOptions extends DateTimeFormatOptions {
  now?: DateTimeValue;
  showTime?: boolean;
  relativeWithinDays?: number;
}

export interface DateTimeDisplay {
  date: Date;
  dateTime: string;
  title: string;
  label: string;
  isRelative: boolean;
}

const DEFAULT_LOCALE = "en-US";
const LEGACY_LOCALE = "en-GB";
const DEFAULT_RELATIVE_WITHIN_DAYS = 7;
const MINUTE_MS = 60 * 1000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

export function parseDateTime(value: DateTimeValue): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const date = value instanceof Date ? new Date(value.getTime()) : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatExactDateTime(
  value: DateTimeValue,
  { locale = DEFAULT_LOCALE, timeZone }: DateTimeFormatOptions = {},
): string | null {
  const date = parseDateTime(value);
  if (date === null) return null;
  return dateTimeFormat(locale, {
    dateStyle: "long",
    timeStyle: "short",
    timeZone,
  }).format(date);
}

export function formatAbsoluteDateTime(
  value: DateTimeValue,
  {
    locale = DEFAULT_LOCALE,
    timeZone,
    showTime = false,
  }: DateTimeFormatOptions & { showTime?: boolean } = {},
): string | null {
  const date = parseDateTime(value);
  if (date === null) return null;

  if (!locale.toLowerCase().startsWith("en")) {
    return dateTimeFormat(locale, {
      dateStyle: "long",
      timeStyle: showTime ? "short" : undefined,
      timeZone,
    }).format(date);
  }

  const parts = dateTimeFormat(locale, {
    month: "long",
    day: "numeric",
    year: "numeric",
    timeZone,
  }).formatToParts(date);
  const month = partValue(parts, "month");
  const day = partValue(parts, "day");
  const year = partValue(parts, "year");
  if (!month || !day || !year) {
    return dateTimeFormat(locale, {
      dateStyle: "long",
      timeStyle: showTime ? "short" : undefined,
      timeZone,
    }).format(date);
  }

  const dateLabel = `${month} ${ordinal(Number(day))}, ${year}`;
  if (!showTime) return dateLabel;
  return `${dateLabel}, ${formatTime(date, locale, timeZone)}`;
}

export function formatRelativeDateTime(
  value: DateTimeValue,
  {
    locale = DEFAULT_LOCALE,
    now = new Date(),
    relativeWithinDays = DEFAULT_RELATIVE_WITHIN_DAYS,
  }: Pick<DateTimeDisplayOptions, "locale" | "now" | "relativeWithinDays"> = {},
): string | null {
  const date = parseDateTime(value);
  const nowDate = parseDateTime(now);
  if (date === null || nowDate === null) return null;

  const diffMs = date.getTime() - nowDate.getTime();
  const absMs = Math.abs(diffMs);
  if (absMs >= relativeWithinDays * DAY_MS) return null;
  if (absMs < MINUTE_MS) return "just now";

  const formatter = relativeTimeFormat(locale);
  const minutes = relativeValue(diffMs, MINUTE_MS);
  if (Math.abs(minutes) < 60) {
    return formatter.format(minutes, "minute");
  }
  const hours = relativeValue(diffMs, HOUR_MS);
  if (Math.abs(hours) < 24) {
    return formatter.format(hours, "hour");
  }
  return formatter.format(relativeValue(diffMs, DAY_MS), "day");
}

export function formatDateTimeDisplay(
  value: DateTimeValue,
  options: DateTimeDisplayOptions = {},
): DateTimeDisplay | null {
  const date = parseDateTime(value);
  if (date === null) return null;

  const { locale = DEFAULT_LOCALE, timeZone, showTime = false } = options;
  const title = formatExactDateTime(date, { locale, timeZone });
  if (title === null) return null;

  const relative = formatRelativeDateTime(date, options);
  const absolute = formatAbsoluteDateTime(date, { locale, timeZone, showTime });
  const label = relative ?? absolute;
  if (label === null) return null;

  return {
    date,
    dateTime: date.toISOString(),
    title,
    label,
    isRelative: relative !== null,
  };
}

export function shouldRefreshRelativeDateTime(
  value: DateTimeValue,
  now: DateTimeValue = new Date(),
  relativeWithinDays = DEFAULT_RELATIVE_WITHIN_DAYS,
): boolean {
  const date = parseDateTime(value);
  const nowDate = parseDateTime(now);
  if (date === null || nowDate === null) return false;
  return Math.abs(date.getTime() - nowDate.getTime()) < relativeWithinDays * DAY_MS;
}

export function fmtDate(
  iso: string,
  locale = LEGACY_LOCALE,
  opts?: Intl.DateTimeFormatOptions,
): string {
  return new Date(iso).toLocaleDateString(
    locale,
    opts ?? { day: "2-digit", month: "short" },
  );
}

function formatTime(date: Date, locale: string, timeZone?: string): string {
  return dateTimeFormat(locale, {
    hour: "2-digit",
    minute: "2-digit",
    timeZone,
  }).format(date);
}

function dateTimeFormat(
  locale: string,
  options: Intl.DateTimeFormatOptions,
): Intl.DateTimeFormat {
  try {
    return new Intl.DateTimeFormat(locale, options);
  } catch (error) {
    if (!(error instanceof RangeError)) throw error;
  }

  try {
    return new Intl.DateTimeFormat(DEFAULT_LOCALE, options);
  } catch (error) {
    if (!(error instanceof RangeError)) throw error;
  }

  return new Intl.DateTimeFormat(DEFAULT_LOCALE, { ...options, timeZone: undefined });
}

function relativeTimeFormat(locale: string): Intl.RelativeTimeFormat {
  try {
    return new Intl.RelativeTimeFormat(locale, { numeric: "always" });
  } catch (error) {
    if (!(error instanceof RangeError)) throw error;
  }
  return new Intl.RelativeTimeFormat(DEFAULT_LOCALE, { numeric: "always" });
}

function partValue(parts: Intl.DateTimeFormatPart[], type: Intl.DateTimeFormatPartTypes): string {
  return parts.find((part) => part.type === type)?.value ?? "";
}

function ordinal(day: number): string {
  const tens = day % 100;
  if (tens >= 11 && tens <= 13) return `${day}th`;
  switch (day % 10) {
    case 1:
      return `${day}st`;
    case 2:
      return `${day}nd`;
    case 3:
      return `${day}rd`;
    default:
      return `${day}th`;
  }
}

function relativeValue(diffMs: number, unitMs: number): number {
  const rounded = Math.round(Math.abs(diffMs) / unitMs);
  return diffMs < 0 ? -Math.max(1, rounded) : Math.max(1, rounded);
}
