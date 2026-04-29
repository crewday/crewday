export function fmtDayMon(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

export function fmtDayMonTime(iso: string): string {
  const d = new Date(iso);
  const date = d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return date + " · " + time;
}

export function formatValue(value: unknown): string {
  if (value === true) return "yes";
  if (value === false) return "no";
  if (value === null || value === undefined) return "--";
  return String(value);
}
