const DAY_MS = 86_400_000;

function utcDateFromIsoDate(iso: string): Date {
  const [year, month, day] = iso.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day));
}

export function fmtDayMonYear(iso: string): string {
  return utcDateFromIsoDate(iso).toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

export function inclusiveDays(startIso: string, endIso: string): number {
  const ms = utcDateFromIsoDate(endIso).getTime() - utcDateFromIsoDate(startIso).getTime();
  return Math.floor(ms / DAY_MS) + 1;
}
